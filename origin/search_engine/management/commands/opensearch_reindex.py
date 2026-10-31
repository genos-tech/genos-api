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

Two lanes of write, reported separately as `chunks_changed` and
`chunks_metadata_only`. Only the former re-embeds. A chunk whose text
is untouched but whose metadata moved (status, priority, assignee,
milestone/sprint link, ACL, `updated_at`) is refreshed with a partial
doc update that keeps the stored vector, so correcting drifted metadata
across the whole corpus costs bulk updates and no embedding spend.

That makes a one-off full run the repair tool for metadata that went
stale before this lane existed — those chunks have an empty
`RagChunk.meta_hash`, so the first pass over each rewrites its
overlays:

    python manage.py opensearch_reindex        # heals; re-embeds nothing

Membership changes: chunk ACLs are copied from project and channel
membership, which an incremental pass can't see change. `acl_dirty`
queues each project or channel whose members changed; an incremental
pass also re-emits everything in those scopes (a metadata-only update,
so no re-embedding) and then clears the marks it consumed. A pass with
errors, a dry run, or an `--entity-types` subset that skips an
ACL-bearing type keeps them for the next pass.

Schema versions: when the live index was built under a different
`INDEX_SCHEMA_VERSION` than the code expects, the command prints a
warning. The fix is to recreate the index:

    python manage.py opensearch_setup --recreate
    python manage.py opensearch_reindex
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from django.core.management.base import CommandError
from opensearchpy.exceptions import NotFoundError

from origin.management.cron_command import CronCommand
from origin.search_engine import acl_dirty
from origin.search_engine.index_config import INDEX_SCHEMA_VERSION
from origin.search_engine.ingestion import ingest_all
from origin.search_engine.opensearch_client import get_client, get_index_alias
from origin.search_engine.purge import sweep_orphans

log = logging.getLogger(__name__)


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
            "--catch-up",
            action="store_true",
            help=(
                "With --since-minutes and --heartbeat: start the window at "
                "the heartbeat's last successful run (minus the same "
                "overlap) when that is earlier, so a skipped or failed run "
                "is covered by the next one. Set by railway-reindexer.toml."
            ),
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
            overlap = timedelta(minutes=options["since_minutes"])
            since = datetime.now(timezone.utc) - overlap
            if options.get("catch_up"):
                since = _catch_up_since(since, options.get("heartbeat") or "", overlap)
        elif options.get("catch_up"):
            raise CommandError("--catch-up needs --since-minutes.")

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

        # Claimed before the chunkers run, so a mark written mid-pass is
        # left for the next one. A full pass re-reads every ACL anyway,
        # but still claims, so it can clear the queue.
        dirty = acl_dirty.claim()
        stats = ingest_all(
            since=since,
            entity_types=options.get("entity_types"),
            dry_run=dry_run,
            dirty=dirty,
        )
        acl_marks_released = 0
        if _may_release(dirty, stats, options):
            acl_marks_released = acl_dirty.release(dirty)

        # Deleted entities never re-enter the chunkers, so ingestion alone
        # can't clean them up — the sweep is the delete path's backstop.
        # It runs on every pass (hooks in the delete views give immediacy;
        # this bounds worst-case staleness at the cron cadence).
        sweep_stats = None
        if not options.get("no_purge_orphans"):
            sweep_stats = sweep_orphans(dry_run=dry_run)

        self.stdout.write(self.style.SUCCESS("Reindex complete."))
        payload = {
            "ingestion": stats.as_dict(),
            "acl_marks": {"claimed": len(dirty.rows), "released": acl_marks_released},
        }
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


# Every entity type whose ACL comes from project or channel membership.
# Todos, conversations and spotlight answers are owner-scoped.
_ACL_ENTITY_TYPES = frozenset(
    {"chat", "task", "milestone", "note", "thread_summary", "note_summary"}
)


def _catch_up_since(since: datetime, heartbeat: str, overlap: timedelta) -> datetime:
    """Widen an incremental window back to the cron's last success.

    A fixed `--since-minutes` window only covers what changed since the
    previous tick. When a tick is skipped (a redeploy, a Railway hiccup) or
    fails, whatever was written in its window is never re-emitted, and it
    stays missing from search until someone runs a full reindex by hand.

    `last_succeeded_at` only moves on a run that finished without an ERROR,
    so a failed run widens the next window too. The overlap is subtracted
    from it because that run read Postgres before it finished.
    """
    if not heartbeat:
        raise CommandError("--catch-up needs --heartbeat to know the last successful run.")
    from origin.models.common.ops_models import CronHeartbeat  # noqa: PLC0415

    try:
        last_ok = (
            CronHeartbeat.objects.filter(name=heartbeat)
            .values_list("last_succeeded_at", flat=True)
            .first()
        )
    except Exception:  # noqa: BLE001 — never block the reindex on the lookup
        log.warning("Catch-up: heartbeat lookup failed; using the fixed window", exc_info=True)
        return since
    if last_ok is None:
        return since
    return min(since, last_ok - overlap)


def _may_release(dirty, stats, options) -> bool:
    """Whether this pass refreshed every claimed scope. Anything less
    keeps the marks: a missed refresh leaves a removed member's hits."""
    if not dirty.rows or options.get("dry_run") or stats.errors:
        return False
    entity_types = options.get("entity_types")
    return not entity_types or _ACL_ENTITY_TYPES <= set(entity_types)
