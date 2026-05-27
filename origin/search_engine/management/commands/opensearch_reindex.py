"""Management command that runs the OpenSearch ingestion pipeline.

Usage:

    # Full reindex (re-evaluates every entity; only re-embeds chunks
    # whose text actually changed).
    python manage.py opensearch_reindex

    # Incremental reindex — only entities updated in the last N minutes.
    # Suitable for a crontab entry.
    python manage.py opensearch_reindex --since-minutes 10

    # Restrict to specific entity types.
    python manage.py opensearch_reindex --entity-types chat task

The command is idempotent: running it twice in a row when nothing has
changed produces zero embedding calls and zero OpenSearch writes (it
still scans Postgres + the RagChunk table).
"""

import json
from datetime import datetime, timedelta, timezone

from django.core.management.base import BaseCommand

from origin.search_engine.ingestion import ingest_all


class Command(BaseCommand):
    help = (
        "Re-index chats, tasks, and notes into OpenSearch. By default "
        "runs a full reindex; pass --since-minutes for an incremental "
        "pass (suitable for crontab)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--since-minutes",
            type=int,
            default=None,
            help=(
                "Only re-process entities updated within the last N "
                "minutes. Default: full reindex."
            ),
        )
        parser.add_argument(
            "--since",
            type=str,
            default=None,
            help=("Explicit ISO 8601 timestamp lower-bound. Overrides " "--since-minutes."),
        )
        parser.add_argument(
            "--entity-types",
            nargs="+",
            default=None,
            choices=["chat", "task", "note", "thread_summary", "note_summary"],
            help="Subset of entity types to ingest. Default: all.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Run the chunkers and compute new/changed/stale counts "
                "without calling the embedding API or writing to "
                "OpenSearch / the tracking table."
            ),
        )

    def handle(self, *args, **options):
        since = None
        if options.get("since"):
            since = datetime.fromisoformat(options["since"])
        elif options.get("since_minutes") is not None:
            since = datetime.now(timezone.utc) - timedelta(minutes=options["since_minutes"])

        if since is not None:
            self.stdout.write(f"Incremental reindex since {since.isoformat()}...")
        else:
            self.stdout.write("Full reindex starting...")
        if options.get("dry_run"):
            self.stdout.write("(dry-run: no embeddings, no writes)")

        stats = ingest_all(
            since=since,
            entity_types=options.get("entity_types"),
            dry_run=options.get("dry_run", False),
        )
        self.stdout.write(self.style.SUCCESS("Reindex complete."))
        self.stdout.write(json.dumps(stats.as_dict(), indent=2))
