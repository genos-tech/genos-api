"""`python manage.py prune_old_tombstones` - remove empty tombstones of deleted messages older than
the 60-day sync window (MIP-1937, see `services/tombstone_prune`).

    python manage.py prune_old_tombstones            # DRY RUN: counts only, writes nothing
    python manage.py prune_old_tombstones --apply    # delete them (at most --limit per run)

Output is COUNTS ONLY. Safe to re-run. Not scheduled: run it by hand (or add it to a cron) once the
count in the dry run looks right.
"""

from __future__ import annotations

import logging

from origin.management.cron_command import CronCommand
from origin.services import tombstone_prune as service

log = logging.getLogger("origin.mcp")


class Command(CronCommand):
    help = (
        "Find (dry run, the default) or remove (--apply) empty tombstone rows of deleted messages "
        "older than the sync window. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Delete them. Default: dry run.")
        parser.add_argument("--limit", type=int, default=service.DEFAULT_LIMIT)

    def handle(self, *args, **opts):
        apply = bool(opts["apply"])
        self.stdout.write("prune_old_tombstones: " + ("APPLY" if apply else "DRY RUN"))
        result = service.prune(apply=apply, limit=int(opts["limit"]))
        kept = ", ".join(f"{k}={v}" for k, v in result.kept.items())
        if apply:
            self.stdout.write(f"removed {result.changed} tombstones; kept: {kept}")
            log.info("prune_old_tombstones: removed=%d", result.changed)
        else:
            self.stdout.write(f"would remove {result.matched} tombstones; kept: {kept}")
            if result.matched:
                self.stdout.write("re-run with --apply to remove them")
