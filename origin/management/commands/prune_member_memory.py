"""`python manage.py prune_member_memory` - the daily agent-memory sweep (MIP-2251).

Deletes `MemberEvent` rows older than 180 days and `MemberItemDay` rows older than 365 days for
every member (the write path only did it for the member who wrote). DRY RUN BY DEFAULT: counts
only; `--apply` deletes. Output is counts only. See `origin/services/member_memory_sweep.py`.

Run daily by the `member-memory-sweep` service in `.railway/railway.ts`.
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.services import member_memory_sweep

log = logging.getLogger(__name__)


class Command(CronCommand):
    help = "Count (default) or delete agent-memory rows past their window, for every member."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Delete. Without it: counts only.")

    def handle(self, *args, **opts):
        apply = opts["apply"]
        result = member_memory_sweep.sweep(apply=apply)
        verb = "deleted" if apply else "would delete"
        prefix = "" if apply else "[dry-run] "
        self.stdout.write(f"{prefix}member_events: {verb} {result.events}")
        self.stdout.write(f"{prefix}member_item_days: {verb} {result.item_days}")
        if apply:
            log.info("prune_member_memory: events=%d item_days=%d", result.events, result.item_days)
