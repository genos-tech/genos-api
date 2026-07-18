"""Daily OpenSearch maintenance: reclaim deleted docs, report index health.

Usage:

    # Normal daily pass (what the cron runs): logs doc/segment/disk
    # stats and runs `_forcemerge?only_expunge_deletes=true` when the
    # whole-index deleted-doc share is >= 5%.
    python manage.py opensearch_maintain

    # Stats only — see the deleted ratio without merging.
    python manage.py opensearch_maintain --dry-run

    # Merge regardless of the threshold.
    python manage.py opensearch_maintain --force

    # One-off full compaction to N segments. ONLY for right after a
    # full `opensearch_setup --recreate` + reindex, while writes are
    # quiet — a maxed-out segment stops merging naturally, so never
    # put this on a cron.
    python manage.py opensearch_maintain --max-num-segments 1

Why deletes accumulate at all: see `origin.search_engine.maintenance`.
"""

import json

from django.core.management.base import CommandError

from origin.management.cron_command import CronCommand
from origin.search_engine.maintenance import DEFAULT_MIN_DELETED_RATIO, maintain_index
from origin.search_engine.opensearch_client import get_client


class Command(CronCommand):
    help = (
        "Report chunk-index health and expunge deleted documents when "
        "their share exceeds the threshold. Intended as a daily cron."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--min-deleted-ratio",
            type=float,
            default=DEFAULT_MIN_DELETED_RATIO,
            help=(
                "Whole-index deleted-doc share (0..1) below which the "
                f"merge is skipped. Default: {DEFAULT_MIN_DELETED_RATIO}."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run the expunge merge even below the threshold.",
        )
        parser.add_argument(
            "--max-num-segments",
            type=int,
            default=None,
            help=(
                "Full compaction to N segments instead of the expunge "
                "merge. Manual use only (after a full recreate+reindex); "
                "never on a cron."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Collect and print stats without merging.",
        )

    def handle(self, *args, **options):
        # Fail fast (and loud — CronCommand marks the run red) when
        # OpenSearch is down: an unreachable cluster is a real incident,
        # unlike a merely missing index, which maintain_index treats as
        # a warning because the reindexer owns recovery.
        if not get_client().ping():
            raise CommandError("OpenSearch is unreachable (ping failed); aborting maintenance.")

        report = maintain_index(
            min_deleted_ratio=options["min_deleted_ratio"],
            force=options["force"],
            max_num_segments=options["max_num_segments"],
            dry_run=options["dry_run"],
        )
        self.stdout.write(self.style.SUCCESS(f"Maintenance: {report['action']}"))
        self.stdout.write(json.dumps(report, indent=2))
