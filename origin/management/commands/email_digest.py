"""The email digest — a plain "what you missed" summary, all tiers.

NOT the tier-gated AI agent digest (`agent_digest`, in-app + push,
pro/max only, LLM-generated). This one is a daily unread summary built
from two DB queries, costs nothing per user, and exists for retention:
it is the mail that brings people back. Same hourly local-time-tick
skeleton as `agent_digest`:

  * HOURLY cron (railway-email-digest.toml); each pass selects users
    whose LOCAL clock (CustomUser.timezone) is at --at-hour (default 8).
  * Idempotent via `email_digest_last_sent_at` + a 20h MIN_GAP —
    deliberately its OWN stamp, never the agent digest's
    (`digest_last_sent_at`): sharing would let one channel's send
    suppress the other and one opt-out kill both.
  * Content = unread Activity + unread InboxItems since the last stamp,
    MINUS anything a coalesced notification email already delivered
    (join against the outbox's `sent` rows — the outbox doubles as the
    dedupe ledger between the two email surfaces).
  * Empty digest -> stamp, send nothing (the agent_digest rule: a mail
    that says nothing useful is worse than no mail).
  * Gates: `email_digest_enabled` (its own opt-out, flipped by the
    "digest"-scope unsubscribe), the `email_enabled`/`master_enabled`
    prefs masters, and the bounce/complaint suppression list.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.models.chat.unified_models import Activity
from origin.models.common.inbox_models import InboxItems
from origin.models.common.notification_models import (
    EmailNotificationEvent,
    NotificationPreference,
)
from origin.models.common.user_models import CustomUser
from origin.services.email import send_templated_email
from origin.services.email_notify_send import _absolute, resolve_email_locale
from origin.services.email_suppression import is_suppressed
from origin.services.email_unsubscribe import unsubscribe_headers, unsubscribe_url
from origin.services.user_time import resolve_zone

log = logging.getLogger(__name__)

_MIN_GAP = timedelta(hours=20)  # DST-tolerant "once a day"
_FIRST_WINDOW = timedelta(days=7)  # first-ever digest looks back a week
_MAX_ENTRIES = 10


def _digest_template(locale: str) -> str:
    if locale != "en":
        candidate = f"{locale}/digest"
        try:
            get_template(f"emails/{candidate}.txt")
            get_template(f"emails/{candidate}.html")
            return candidate
        except TemplateDoesNotExist:
            log.warning("[email] missing %s digest templates; falling back to en", locale)
    return "digest"


def _subject(total: int, locale: str) -> str:
    if locale == "ja":
        return f"Genosダイジェスト — 未読{total}件"
    return f"Your Genos digest — {total} update{'s' if total != 1 else ''}"


class Command(CronCommand):
    help = (
        "Hourly email-digest tick: send an unread summary to users whose "
        "local time matches, minus what coalesced emails already covered."
    )

    def add_arguments(self, parser):
        parser.add_argument("--at-hour", type=int, default=8)
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--user-id",
            default="",
            help="Restrict to one user id and SKIP the local-hour check.",
        )

    def handle(self, *args, **options):
        if not settings.EMAIL_NOTIFICATIONS_ENABLED:
            self.stdout.write("EMAIL_NOTIFICATIONS_ENABLED is off; nothing to do.")
            return
        at_hour = options["at_hour"] % 24
        limit = max(1, int(options["limit"]))
        dry_run = bool(options["dry_run"])
        only_user = (options["user_id"] or "").strip()
        now = timezone.now()

        users = CustomUser.objects.filter(email_digest_enabled=True, is_deleted=False)
        if only_user:
            users = users.filter(id=only_user)
        users = list(
            users.only(
                "id", "email", "username", "timezone", "language", "email_digest_last_sent_at"
            )
        )

        sent = skipped = failed = 0
        for user in users:
            if sent >= limit:
                break
            uid = str(user.id)
            if not user.email:
                continue
            if not only_user:
                local = now.astimezone(resolve_zone(user.timezone))
                if local.hour != at_hour:
                    continue
            last = user.email_digest_last_sent_at
            if last is not None and now - last < _MIN_GAP:
                skipped += 1
                continue
            prefs = NotificationPreference.objects.filter(user_id=uid).first()
            if prefs is not None and (not prefs.email_enabled or not prefs.master_enabled):
                skipped += 1
                continue
            if is_suppressed(user.email):
                skipped += 1
                continue

            if dry_run:
                self.stdout.write(f"[dry-run] would send digest to {uid}")
                sent += 1
                continue

            try:
                total, entries = self._collect(uid, last, now)
                if total == 0:
                    # Truthful "nothing new" stamps, or every later tick
                    # today re-asks the same question.
                    user.email_digest_last_sent_at = now
                    user.save(update_fields=["email_digest_last_sent_at"])
                    skipped += 1
                    continue
                self._send(user, total, entries)
            except Exception:  # noqa: BLE001 — one user never kills the pass
                log.warning("digest send failed for user=%s", uid, exc_info=True)
                failed += 1
                continue
            user.email_digest_last_sent_at = now
            user.save(update_fields=["email_digest_last_sent_at"])
            sent += 1

        self.stdout.write(
            f"email digest @{at_hour}:00 local — sent={sent} skipped={skipped} "
            f"failed={failed} (of {len(users)} enabled users)"
        )

    def _collect(self, uid: str, last, now):
        """(total unread, top entries) since the last stamp, minus what a
        coalesced notification email already delivered."""
        # Lazy import: email_enqueue imports webpush_dispatch; keep this
        # command importable without dragging the push stack at load time.
        from origin.services.email_enqueue import _email_spec

        window_start = last or (now - _FIRST_WINDOW)

        acts = list(
            Activity.objects.filter(
                recipient_id=uid, is_read=False, ts_created_at__gte=window_start
            )
            .select_related("actor", "channel", "message")
            .order_by("-ts_created_at")[:100]
        )
        items = list(
            InboxItems.objects.filter(
                receiver_id=uid,
                is_read=False,
                is_deleted=False,
                ts_created_at__gte=window_start,
            )
            .select_related("sender")
            .order_by("-ts_created_at")[:50]
        )
        emailed_acts = set(
            EmailNotificationEvent.objects.filter(
                user_id=uid,
                status=EmailNotificationEvent.STATUS_SENT,
                activity_id__in=[a.id for a in acts],
            ).values_list("activity_id", flat=True)
        )
        emailed_items = set(
            EmailNotificationEvent.objects.filter(
                user_id=uid,
                status=EmailNotificationEvent.STATUS_SENT,
                inbox_item_id__in=[i.item_id for i in items],
            ).values_list("inbox_item_id", flat=True)
        )
        acts = [a for a in acts if a.id not in emailed_acts]
        items = [i for i in items if i.item_id not in emailed_items]

        entries = []
        for act in acts:
            spec = _email_spec(act)
            if spec is None:
                continue
            entries.append({"title": spec["title"], "url": _absolute(spec["url"])})
        for item in items:
            from origin.services.webpush_dispatch import _inbox_title

            sender_name = getattr(item.sender, "username", None) or "Someone"
            entries.append(
                {"title": _inbox_title(item, sender_name), "url": _absolute("/workspace/inbox")}
            )
        total = len(entries)
        return total, entries[:_MAX_ENTRIES]

    def _send(self, user, total: int, entries) -> None:
        locale = resolve_email_locale(user)
        send_templated_email(
            to=user.email,
            subject=_subject(total, locale),
            template_base=_digest_template(locale),
            context={
                "user_name": user.username or "",
                "total": total,
                "entries": entries,
                "more": max(0, total - len(entries)),
                "app_url": settings.FRONTEND_BASE_URL,
                # Footer opts out of the DIGEST only; the header pair
                # stays all-scope as everywhere else.
                "unsubscribe_url": unsubscribe_url(user.id, "digest") or "",
            },
            headers=unsubscribe_headers(user.id),
        )
