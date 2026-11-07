"""`python manage.py schedule_tick` — run the members' schedules that have come due (MIP-1147).

Fires every five minutes (the `schedule-tick` service in `.railway/railway.ts`). Each pass
takes the enabled schedules whose `next_run_at` has passed, oldest first, and runs each one's
job as its member (`services.schedules.run_due`). The rules (claim, lateness, failures) live
there.

Failures here are per member and log at WARNING, so a member's broken job never turns the
cron red. Only a fault in the tick itself (the database) logs at ERROR and fails the run.
A tick that stops running shows up through its heartbeat (`CRON_SCHEDULES["schedule-tick"]`).

Usage:
    python manage.py schedule_tick
    python manage.py schedule_tick --dry-run
"""

from __future__ import annotations

import logging

from django.utils import timezone

from origin.management.cron_command import CronCommand
from origin.services import schedules

log = logging.getLogger("origin.schedules")

# Under the cron interval: a tick that overran would make Railway skip the next one.
DEFAULT_BUDGET_SECONDS = 240
DEFAULT_LIMIT = 200
# A due schedule still waiting this long means the cron was down or a pass wedged.
LATE_MINUTES = 15


class Command(CronCommand):
    help = "Run the member schedules whose time has come."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
        parser.add_argument("--budget-seconds", type=int, default=DEFAULT_BUDGET_SECONDS)
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run nothing and write nothing; just report what is due.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        if options["dry_run"]:
            rows = schedules.due_schedules(now=now, limit=options["limit"])
            self.stdout.write(f"[dry-run] {len(rows)} schedule(s) due")
            for row in rows:
                self.stdout.write(
                    f"[dry-run] {row.pk} job={row.job} due={row.next_run_at.isoformat()}"
                )
            return

        summary = schedules.run_due(
            now=now, budget_seconds=options["budget_seconds"], limit=options["limit"]
        )
        if summary.max_late_seconds > LATE_MINUTES * 60:
            log.warning(
                "schedule_tick: oldest due schedule was %d minutes late "
                "(cron down, or a pass wedged?)",
                summary.max_late_seconds // 60,
            )
        self.stdout.write(
            f"schedule_tick: {summary.ran} ran, {summary.failed} failed, "
            f"{summary.skipped_late} skipped late, {summary.skipped_access} skipped access, "
            f"{summary.paused} paused, {summary.claimed_elsewhere} claimed elsewhere, "
            f"{summary.deferred} deferred (of {summary.due} due, "
            f"oldest {summary.max_late_seconds}s late)"
        )
