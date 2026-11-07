"""`python manage.py prune_schedule_reports` - enforce the retention of scheduled reports (MIP-1159).

A member's schedule files a report in their Inbox on every run (item_type 10). Those rows are
generated and recreatable, and nothing else removes them, so without this the Inbox and the
account export only grow. The rule (30 days, newest 60 per schedule, an unread report within
the window is kept) lives in `origin/services/schedule_report_retention.py`; no other item
type is touched.

Run daily via the `schedule-report-prune` service in `.railway/railway.ts`.

Usage:
    python manage.py prune_schedule_reports
    python manage.py prune_schedule_reports --dry-run
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.services import schedule_report_retention as retention

log = logging.getLogger("origin.schedules")


class Command(CronCommand):
    help = "Delete scheduled reports past their retention (age, and the newest N per schedule)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many reports would be deleted without deleting them.",
        )

    def handle(self, *args, **opts):
        result = retention.prune(dry_run=opts["dry_run"])
        detail = (
            f"{result.expired} older than {retention.REPORT_RETENTION_DAYS} days, "
            f"{result.over_cap} beyond the newest {retention.MAX_REPORTS_PER_SCHEDULE} "
            "of their schedule"
        )
        if opts["dry_run"]:
            self.stdout.write(
                f"[dry-run] would delete {result.matched} scheduled reports ({detail})."
            )
            return
        log.info(
            "prune_schedule_reports: deleted=%d expired=%d over_cap=%d",
            result.deleted,
            result.expired,
            result.over_cap,
        )
        self.stdout.write(f"Deleted {result.deleted} scheduled reports ({detail}).")
