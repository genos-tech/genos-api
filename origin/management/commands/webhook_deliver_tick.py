"""Drain the webhook outbox.

Runs every minute. Each pass claims due deliveries atomically, POSTs
them, and either marks them sent or schedules a backed-off retry.

Modelled on `email_notify_tick`, which is the proven outbox in this
repo, with the one addition that path lacks: `next_attempt_at`. Email
retries flat every 5 minutes because its destination is one provider we
have a contract with. A customer's endpoint may be down for hours, and
retrying it every tick is both rude and useless.

⚠️ **A failing customer endpoint logs at WARNING, never ERROR.**
`CronCommand` fails the whole run on any ERROR (that is its entire
purpose), so logging a 500 from someone else's server at ERROR would
turn every flaky integration into a red cron and train everyone to
ignore it. Same precedent as `email_notify_tick`'s transient-mail
branch. ERROR is reserved for *our* failures.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.models.common.webhook_models import (
    MAX_CONSECUTIVE_FAILURES,
    WebhookDelivery,
    WebhookEndpoint,
)
from origin.services.webhook_delivery import backoff_for, post_delivery

log = logging.getLogger("origin.webhooks")

MAX_ATTEMPTS = 5
# A row still SENDING after this long belonged to a worker that died.
STALE_CLAIM_MINUTES = 15
DEFAULT_LIMIT = 200


class Command(CronCommand):
    help = "Deliver pending outbound webhooks."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_LIMIT,
            help="Max deliveries per pass. A brake, not a scheduler.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Claim nothing and send nothing; just report what is due.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        limit = options["limit"]

        # Stale sweep first, so a row orphaned by a crashed pass rejoins
        # this one rather than waiting for a human.
        revived = WebhookDelivery.objects.filter(
            status=WebhookDelivery.STATUS_SENDING,
            claimed_at__lt=now - timezone.timedelta(minutes=STALE_CLAIM_MINUTES),
        ).update(status=WebhookDelivery.STATUS_PENDING, claimed_at=None)
        if revived:
            log.warning("webhook_deliver: revived %s stale delivery row(s)", revived)

        due = WebhookDelivery.objects.filter(
            status=WebhookDelivery.STATUS_PENDING,
            attempts__lt=MAX_ATTEMPTS,
        ).filter(next_attempt_at__isnull=True) | WebhookDelivery.objects.filter(
            status=WebhookDelivery.STATUS_PENDING,
            attempts__lt=MAX_ATTEMPTS,
            next_attempt_at__lte=now,
        )
        due_ids = list(due.values_list("id", flat=True)[:limit])

        if options["dry_run"]:
            self.stdout.write(f"{len(due_ids)} delivery(ies) due")
            return

        if not due_ids:
            return

        # The filter IS the lock: only rows still PENDING are taken, so
        # two overlapping passes cannot claim the same row.
        claimed = WebhookDelivery.objects.filter(
            id__in=due_ids, status=WebhookDelivery.STATUS_PENDING
        ).update(status=WebhookDelivery.STATUS_SENDING, claimed_at=now)
        if not claimed:
            return

        rows = WebhookDelivery.objects.filter(
            id__in=due_ids, status=WebhookDelivery.STATUS_SENDING
        ).select_related("endpoint")

        sent = failed = 0
        for row in rows:
            endpoint = row.endpoint
            if endpoint is None or not endpoint.is_active:
                # Disabled between enqueue and delivery — drop it rather
                # than retrying against an endpoint somebody turned off.
                row.status = WebhookDelivery.STATUS_FAILED
                row.last_error = "endpoint inactive"
                row.save(update_fields=["status", "last_error"])
                continue

            status_code, error = post_delivery(
                url=endpoint.url,
                secret=endpoint.secret,
                event=row.event,
                delivery_id=str(row.id),
                payload=row.payload,
            )
            row.attempts += 1
            row.last_status_code = status_code
            row.last_error = error[:300]

            if not error:
                row.status = WebhookDelivery.STATUS_SENT
                row.sent_at = timezone.now()
                row.claimed_at = None
                sent += 1
                if endpoint.consecutive_failures:
                    WebhookEndpoint.objects.filter(pk=endpoint.pk).update(consecutive_failures=0)
            else:
                failed += 1
                if row.attempts >= MAX_ATTEMPTS:
                    row.status = WebhookDelivery.STATUS_FAILED
                else:
                    row.status = WebhookDelivery.STATUS_PENDING
                    row.next_attempt_at = timezone.now() + backoff_for(row.attempts)
                row.claimed_at = None
                self._record_endpoint_failure(endpoint)

            row.save(
                update_fields=[
                    "status",
                    "attempts",
                    "last_status_code",
                    "last_error",
                    "next_attempt_at",
                    "claimed_at",
                    "sent_at",
                ]
            )

        # WARNING, not ERROR — see the module docstring.
        if failed:
            log.warning("webhook_deliver: %s sent, %s failed", sent, failed)
        self.stdout.write(f"webhook_deliver: {sent} sent, {failed} failed")

    @staticmethod
    def _record_endpoint_failure(endpoint: WebhookEndpoint) -> None:
        count = endpoint.consecutive_failures + 1
        fields = {"consecutive_failures": count}
        if count >= MAX_CONSECUTIVE_FAILURES:
            # A URL that has failed this many times in a row is not
            # coming back on its own, and leaving it enabled means it
            # consumes cron budget forever.
            fields["is_active"] = False
            fields["disabled_at"] = timezone.now()
            log.warning(
                "webhook_deliver: disabling endpoint %s after %s consecutive failures",
                endpoint.id,
                count,
            )
        WebhookEndpoint.objects.filter(pk=endpoint.pk).update(**fields)
