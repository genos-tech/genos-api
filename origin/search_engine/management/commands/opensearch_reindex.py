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

Schema versions: when the live index was built under a different
`INDEX_SCHEMA_VERSION` than the code expects, the command prints a
warning. The fix is to recreate the index:

    python manage.py opensearch_setup --recreate
    python manage.py opensearch_reindex
"""

import json
from datetime import datetime, timedelta, timezone

from django.core.management.base import CommandError
from opensearchpy.exceptions import NotFoundError

from origin.management.cron_command import CronCommand
from origin.search_engine.index_config import INDEX_SCHEMA_VERSION
from origin.search_engine.ingestion import ingest_all
from origin.search_engine.opensearch_client import get_client, get_index_alias
from origin.search_engine.purge import sweep_orphans


class Command(CronCommand):
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
            choices=[
                "chat",
                "task",
                "milestone",
                "note",
                "thread_summary",
                "note_summary",
                "todo",
                "conversation",
                "spotlight_answer",
            ],
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
        parser.add_argument(
            "--no-purge-orphans",
            action="store_true",
            help=(
                "Skip the post-ingestion orphan sweep (chunks whose "
                "backing entity was deleted). The sweep is the only "
                "cleanup path for deleted entities — chunkers never "
                "revisit them — so skip it only for debugging."
            ),
        )
        parser.add_argument(
            "--purge-orphans-only",
            action="store_true",
            help=(
                "Run ONLY the orphan sweep, without ingesting. Useful "
                "for a one-off cleanup of the deleted-entity backlog "
                "without paying for a full reindex scan."
            ),
        )

    def handle(self, *args, **options):
        # Schema-version sanity check. Best-effort: a mismatched index
        # is still indexable (writes succeed; just the new fields are
        # absent from the live mappings), but the operator should know
        # to recreate so the new keyword fields / subfields work.
        self._warn_on_schema_mismatch()

        # Preflight: fail fast (and before spending any embedding budget)
        # if OpenSearch is unreachable. Otherwise the run would embed the
        # whole corpus, fail every bulk write, and rely on the CronCommand
        # tripwire to catch it only after the wasted work.
        if not options.get("dry_run") and not get_client().ping():
            raise CommandError("OpenSearch is unreachable (ping failed); aborting reindex.")

        since = None
        if options.get("since"):
            since = datetime.fromisoformat(options["since"])
        elif options.get("since_minutes") is not None:
            since = datetime.now(timezone.utc) - timedelta(minutes=options["since_minutes"])

        dry_run = options.get("dry_run", False)

        if options.get("purge_orphans_only"):
            self.stdout.write("Orphan sweep only (no ingestion)...")
            sweep_stats = sweep_orphans(dry_run=dry_run)
            self.stdout.write(self.style.SUCCESS("Orphan sweep complete."))
            self.stdout.write(json.dumps({"orphan_sweep": sweep_stats}, indent=2))
            return

        if since is not None:
            self.stdout.write(f"Incremental reindex since {since.isoformat()}...")
        else:
            self.stdout.write("Full reindex starting...")
        if dry_run:
            self.stdout.write("(dry-run: no embeddings, no writes)")

        stats = ingest_all(
            since=since,
            entity_types=options.get("entity_types"),
            dry_run=dry_run,
        )

        # Deleted entities never re-enter the chunkers, so ingestion alone
        # can't clean them up — the sweep is the delete path's backstop.
        # It runs on every pass (hooks in the delete views give immediacy;
        # this bounds worst-case staleness at the cron cadence).
        sweep_stats = None
        if not options.get("no_purge_orphans"):
            sweep_stats = sweep_orphans(dry_run=dry_run)

        self.stdout.write(self.style.SUCCESS("Reindex complete."))
        payload = {"ingestion": stats.as_dict()}
        if sweep_stats is not None:
            payload["orphan_sweep"] = sweep_stats
        self.stdout.write(json.dumps(payload, indent=2))

    def _warn_on_schema_mismatch(self):
        """Sample one chunk's `index_schema_version` and compare to the
        code's `INDEX_SCHEMA_VERSION`. A mismatch means the live index
        was built before the current schema; new keyword fields and
        text subfields won't exist on the live mapping, so the new
        chunkers will write fields that get silently dropped.

        Non-fatal. Recovery: `manage.py opensearch_setup --recreate`.
        """
        try:
            client = get_client()
            alias = get_index_alias()
            resp = client.search(
                index=alias,
                body={
                    "size": 1,
                    "_source": ["index_schema_version"],
                    "query": {"match_all": {}},
                },
            )
            hits = resp.get("hits", {}).get("hits", [])
            if not hits:
                return  # empty index — first reindex, no mismatch to warn about
            live_version = (hits[0].get("_source") or {}).get("index_schema_version")
            if live_version and live_version != INDEX_SCHEMA_VERSION:
                self.stdout.write(
                    self.style.WARNING(
                        f"Index schema mismatch: live index is "
                        f"{live_version!r} but code expects "
                        f"{INDEX_SCHEMA_VERSION!r}. New v2 fields "
                        "(author_id, task_status, .prefix subfield, "
                        "etc.) won't be searchable until the index is "
                        "recreated. Run:\n"
                        "  manage.py opensearch_setup --recreate\n"
                        "  manage.py opensearch_reindex"
                    )
                )
        except (NotFoundError, Exception):  # noqa: BLE001 — never block reindex on a probe failure
            return
