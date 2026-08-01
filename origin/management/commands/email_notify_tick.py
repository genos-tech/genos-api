"""The email coalescer — drains the notification outbox every ~5 minutes.

One batched email per user per pass, and only when it's actually wanted:

  * AWAY-GATED: a user is emailed only when their OLDEST pending row is
    older than --away-minutes. Someone active in the app gets the in-app
    feed and push; email is for the person who left (Slack's "we emailed
    you because you were away" semantic).
  * RETURN-CANCELLED: a visible tab at send time marks the user's
    pending rows skipped — they're back, the feed has everything.
  * READ-SUPPRESSED: rows whose source Activity / InboxItems became
    read in-app are dropped at send time.
  * COOLDOWN: at most one email per user per --cooldown-minutes,
    derived from the outbox's own `sent` rows (no extra schema).
  * NO-DUPLICATE: rows are claimed by an atomic
    `filter(status=PENDING).update(status=SENDING)` — two overlapping
    passes cannot both claim a row. `sent_at` doubles as the CLAIM
    stamp while a row is SENDING (overwritten with the real send time
    on success, cleared on failure), which is what the stale sweep
    keys on: SENDING rows claimed >15 min ago belong to a crashed pass
    and go back to pending.
  * RETRY: a transport failure re-pends the row with attempts+1
    (failed for good after 5); one user's failure never kills the pass.

Prefs are re-checked at send time (`should_email`) — the enqueue-side
check was the volume valve, this one is the correctness gate. The
suppression hook is a placeholder until PR A5 wires the bounce/complaint
list into it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Min
from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.models.common.notification_models import EmailNotificationEvent
from origin.models.common.user_models import CustomUser
from origin.services import presence
from origin.services.email_gating import should_email
from origin.services.email_notify_send import send_notification_batch

log = logging.getLogger(__name__)

_MAX_ATTEMPTS = 5
_STALE_SENDING = timedelta(minutes=15)


def _suppressed(user) -> bool:
    """Bounce/complaint suppression (EmailSuppression rows, written by
    the Anymail tracking webhook)."""
    from origin.services.email_suppression import is_suppressed

    return is_suppressed(user.email)


class Command(CronCommand):
    help = (
        "Coalesce pending email-notification outbox rows into at most one "
        "batched email per user, honoring away-gating, read-state, "
        "cooldown, and preferences."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--away-minutes",
            type=int,
            default=None,
            help="Only email a user whose oldest pending row is at least "
            "this old (default: settings.EMAIL_NOTIFY_AWAY_MINUTES).",
        )
        parser.add_argument(
            "--cooldown-minutes",
            type=int,
            default=None,
            help="Minimum minutes between two emails to the same user "
            "(default: settings.EMAIL_NOTIFY_COOLDOWN_MINUTES).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=200,
            help="Max users emailed per pass — a brake, not a scheduler.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Select and report, but claim/send/mutate nothing.",
        )
        parser.add_argument(
            "--user-id",
            default="",
            help="Restrict to one user id and SKIP the away threshold (manual testing / support).",
        )

    def handle(self, *args, **options):
        if not settings.EMAIL_NOTIFICATIONS_ENABLED:
            self.stdout.write("EMAIL_NOTIFICATIONS_ENABLED is off; nothing to do.")
            return
        away_minutes = options["away_minutes"]
        if away_minutes is None:
            away_minutes = settings.EMAIL_NOTIFY_AWAY_MINUTES
        cooldown_minutes = options["cooldown_minutes"]
        if cooldown_minutes is None:
            cooldown_minutes = settings.EMAIL_NOTIFY_COOLDOWN_MINUTES
        limit = max(1, int(options["limit"]))
        dry_run = bool(options["dry_run"])
        only_user = (options["user_id"] or "").strip()
        now = timezone.now()

        # Recover rows claimed by a pass that died mid-send. `sent_at`
        # is the claim stamp for SENDING rows (see module docstring).
        if not dry_run:
            swept = EmailNotificationEvent.objects.filter(
                status=EmailNotificationEvent.STATUS_SENDING,
                sent_at__lt=now - _STALE_SENDING,
            ).update(status=EmailNotificationEvent.STATUS_PENDING, sent_at=None)
            if swept:
                log.warning("[email] swept %d stale SENDING rows back to pending", swept)

        pending = (
            EmailNotificationEvent.objects.filter(
                status=EmailNotificationEvent.STATUS_PENDING,
                attempts__lt=_MAX_ATTEMPTS,
            )
            .values("user_id")
            .annotate(oldest=Min("ts_created_at"), n=Count("id"))
        )
        if only_user:
            pending = pending.filter(user_id=only_user)
        candidates = {
            str(row["user_id"]): row
            for row in pending
            if only_user or row["oldest"] <= now - timedelta(minutes=away_minutes)
        }
        if not candidates:
            self.stdout.write("email tick — nobody due.")
            return

        # Cooldown, derived from the outbox itself.
        cooled = {
            str(uid)
            for uid in EmailNotificationEvent.objects.filter(
                status=EmailNotificationEvent.STATUS_SENT,
                sent_at__gte=now - timedelta(minutes=cooldown_minutes),
                user_id__in=list(candidates),
            ).values_list("user_id", flat=True)
        }

        users = {
            str(u.id): u
            for u in CustomUser.objects.filter(id__in=list(candidates), is_deleted=False)
        }

        sent = skipped_presence = skipped_cooldown = failed = 0
        for uid, meta in candidates.items():
            if sent >= limit:
                break
            user = users.get(uid)
            if user is None or not user.email:
                continue
            if uid in cooled:
                skipped_cooldown += 1
                continue
            if dry_run:
                self.stdout.write(f"[dry-run] would email {uid} ({meta['n']} events)")
                sent += 1
                continue
            if presence.has_visible_tab(uid):
                # They came back — the in-app feed shows everything.
                EmailNotificationEvent.objects.filter(
                    user_id=uid, status=EmailNotificationEvent.STATUS_PENDING
                ).update(status=EmailNotificationEvent.STATUS_SKIPPED)
                skipped_presence += 1
                continue

            # Atomic claim: the filter is the lock.
            EmailNotificationEvent.objects.filter(
                user_id=uid,
                status=EmailNotificationEvent.STATUS_PENDING,
                attempts__lt=_MAX_ATTEMPTS,
            ).update(status=EmailNotificationEvent.STATUS_SENDING, sent_at=now)
            claimed = list(
                EmailNotificationEvent.objects.filter(
                    user_id=uid, status=EmailNotificationEvent.STATUS_SENDING
                )
                .select_related("activity", "inbox_item")
                .order_by("ts_created_at")
            )

            to_send, to_skip = [], []
            for row in claimed:
                source_read = (row.activity is not None and row.activity.is_read) or (
                    row.inbox_item is not None and row.inbox_item.is_read
                )
                if source_read or not should_email(uid, row.category):
                    to_skip.append(row.id)
                else:
                    to_send.append(row)
            if _suppressed(user):
                to_skip += [row.id for row in to_send]
                to_send = []
            if to_skip:
                EmailNotificationEvent.objects.filter(id__in=to_skip).update(
                    status=EmailNotificationEvent.STATUS_SKIPPED, sent_at=None
                )
            if not to_send:
                continue

            try:
                send_notification_batch(user, to_send)
            except Exception as exc:  # noqa: BLE001 — one user never kills the pass
                # WARNING, not ERROR: a transient mail outage retries on
                # the next tick and must not red the cron (the
                # ai_cost_report precedent). Persistent failure surfaces
                # as rows hitting FAILED + the Resend dashboard.
                log.warning("[email] send failed for user=%s: %s", uid, exc)
                for row in to_send:
                    row.attempts += 1
                    row.status = (
                        EmailNotificationEvent.STATUS_FAILED
                        if row.attempts >= _MAX_ATTEMPTS
                        else EmailNotificationEvent.STATUS_PENDING
                    )
                    row.sent_at = None
                EmailNotificationEvent.objects.bulk_update(
                    to_send, ["attempts", "status", "sent_at"]
                )
                failed += 1
                continue
            EmailNotificationEvent.objects.filter(id__in=[r.id for r in to_send]).update(
                status=EmailNotificationEvent.STATUS_SENT, sent_at=timezone.now()
            )
            sent += 1

        self.stdout.write(
            f"email tick — sent={sent} skipped_presence={skipped_presence} "
            f"skipped_cooldown={skipped_cooldown} failed={failed} "
            f"(of {len(candidates)} due users)"
        )
