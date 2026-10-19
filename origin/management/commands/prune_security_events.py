"""`python manage.py prune_security_events` — enforce the audit log's retention.

Deletes `SecurityEvent` rows older than `SECURITY_EVENT_RETENTION_DAYS`
(365 by default, never under `SECURITY_EVENT_RETENTION_MIN_DAYS`). This
is the only code path that removes audit rows; see
`SecurityEvent.objects.prune`.

Run daily via `railway-security-event-prune.toml`.

Usage:
    python manage.py prune_security_events
    python manage.py prune_security_events --dry-run
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.models.common.security_event_models import SecurityEvent
from origin.services.security_events import prune_before, retention_days

log = logging.getLogger(__name__)


class Command(CronCommand):
    help = "Delete security audit events older than the retention period."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows would be deleted without deleting them.",
        )

    def handle(self, *args, **opts):
        before = prune_before()
        if opts["dry_run"]:
            count = SecurityEvent.objects.filter(ts_created_at__lt=before).count()
            self.stdout.write(
                f"[dry-run] would delete {count} security events older than "
                f"{retention_days()} days (before {before:%Y-%m-%d})."
            )
            return
        deleted = SecurityEvent.objects.prune(before)
        log.info(
            "prune_security_events: deleted=%d retention_days=%d before=%s",
            deleted,
            retention_days(),
            before.isoformat(),
        )
        self.stdout.write(f"Deleted {deleted} security events older than {retention_days()} days.")
