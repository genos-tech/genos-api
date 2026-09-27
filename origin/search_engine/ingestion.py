"""Ingestion orchestrator.

Walks `EntityChunks` produced by each chunker, and for every entity:

  1. Hashes the chunk text, and separately fingerprints its metadata
     (the status / priority / assignee / milestone / sprint overlays,
     the ACL list, `updated_at` — everything but `search_text`).
  2. Compares against the `RagChunk` tracking table to split chunks
     into `new` / `changed` / `metadata_only` / `unchanged` and
     identifies stale chunk ids that exist in the table but no longer
     in the regeneration.
  3. Embeds only new + changed chunks (skipping unchanged saves
     OpenAI cost and time).
  4. Bulk-indexes the new + changed docs into OpenSearch, and applies
     `metadata_only` chunks as partial doc updates that leave the
     stored embedding — and so the embedding bill — untouched.
  5. Deletes stale chunks from OpenSearch.
  6. Mirrors all of that into `RagChunk` (upsert / delete) so the
     next run can repeat steps 1-2.

Why step 4 has two lanes: the metadata a chunk carries is NOT part of
the text it was embedded from, so a status flip (Open → Closed), a
reassignment, or an ACL change produces a byte-identical
`search_text`. Hashing only the text classified those edits as
"unchanged" and never wrote them, leaving OpenSearch to serve stale
overlays forever — visible as a Spotlight status chip that disagreed
with the task and that neither a page refresh nor a full reindex could
correct.

This is the entry point both the `opensearch_reindex` management
command and (later) any Kafka-style indexer worker should call.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Iterable, Optional

from django.conf import settings
from django.db import transaction
from opensearchpy import helpers as os_helpers

from origin.search_engine.chunkers.base import Chunk, EntityChunks
from origin.search_engine.chunkers.chat_chunker import iter_all_chat_chunks
from origin.search_engine.chunkers.conversation_chunker import (
    conversation_chunks_for_run,
    iter_conversation_chunks,
)
from origin.search_engine.chunkers.milestone_chunker import iter_milestone_chunks
from origin.search_engine.chunkers.note_chunker import iter_all_note_chunks
from origin.search_engine.chunkers.note_summary_chunker import iter_note_summary_chunks
from origin.search_engine.chunkers.spotlight_answer_chunker import iter_spotlight_answer_chunks
from origin.search_engine.chunkers.task_chunker import iter_task_chunks
from origin.search_engine.chunkers.thread_summary_chunker import iter_thread_summary_chunks
from origin.search_engine.chunkers.todo_chunker import iter_todo_chunks
from origin.search_engine.embeddings import (
    embed_texts,
    get_active_embedding_model_name,
    hash_text,
)
from origin.search_engine.index_config import INDEX_SCHEMA_VERSION
from origin.search_engine.llm import spend
from origin.search_engine.models import RagChunk
from origin.search_engine.opensearch_client import get_client, get_index_alias

log = logging.getLogger(__name__)


class IngestionStats:
    def __init__(self):
        self.entities_processed = 0
        self.chunks_total = 0
        self.chunks_new = 0
        self.chunks_changed = 0
        # Text identical, metadata not: refreshed in place, never re-embedded.
        self.chunks_metadata_only = 0
        self.chunks_unchanged = 0
        self.chunks_deleted = 0
        self.errors: list[str] = []

    def as_dict(self) -> dict:
        return {
            "entities_processed": self.entities_processed,
            "chunks_total": self.chunks_total,
            "chunks_new": self.chunks_new,
            "chunks_changed": self.chunks_changed,
            "chunks_metadata_only": self.chunks_metadata_only,
            "chunks_unchanged": self.chunks_unchanged,
            "chunks_deleted": self.chunks_deleted,
            "errors": self.errors,
        }


def ingest_all(
    since: Optional[datetime] = None,
    entity_types: Optional[list[str]] = None,
    dry_run: bool = False,
) -> IngestionStats:
    """Run a full or incremental ingestion across chats/tasks/notes.

    Args:
        since: Only re-process entities updated after this timestamp.
            None means full reindex.
        entity_types: Restrict to a subset, e.g. ["chat"]. Default
            processes all three.
        dry_run: Skip embeddings, OpenSearch writes, and tracking-table
            writes. The chunkers still run and stats are populated, so
            you can verify which chunks *would* be touched without
            consuming OpenAI quota or mutating state.
    """
    stats = IngestionStats()
    entity_types = entity_types or [
        "chat",
        "task",
        "milestone",
        "note",
        "thread_summary",
        "note_summary",
        "todo",
        "conversation",
        "spotlight_answer",
    ]

    # Cost meter — embeddings are priced spend now, and a reindex pass
    # makes a lot of them. Attribute the pass to its own `index` surface
    # instead of letting every cron embed land in `unattributed` (which
    # is reserved for MISSING binds, not known infra work). Re-entrant:
    # a caller that already bound a request keeps its own attribution.
    with spend.spend_context(surface="index"), spend.spend_purpose("index"):
        return _ingest_all_bound(stats, since=since, entity_types=entity_types, dry_run=dry_run)


def _ingest_all_bound(
    stats: IngestionStats,
    *,
    since: Optional[datetime],
    entity_types: list[str],
    dry_run: bool,
) -> IngestionStats:
    if "chat" in entity_types:
        log.info("Ingesting chats (since=%s, dry_run=%s)...", since, dry_run)
        _ingest_stream(iter_all_chat_chunks(since=since), stats, dry_run=dry_run)
    if "task" in entity_types:
        log.info("Ingesting tasks (since=%s, dry_run=%s)...", since, dry_run)
        _ingest_stream(iter_task_chunks(since=since), stats, dry_run=dry_run)
    if "milestone" in entity_types:
        log.info("Ingesting milestones (since=%s, dry_run=%s)...", since, dry_run)
        _ingest_stream(iter_milestone_chunks(since=since), stats, dry_run=dry_run)
    if "note" in entity_types:
        log.info("Ingesting notes (since=%s, dry_run=%s)...", since, dry_run)
        _ingest_stream(iter_all_note_chunks(since=since), stats, dry_run=dry_run)
    if "thread_summary" in entity_types:
        log.info("Ingesting thread summaries (since=%s, dry_run=%s)...", since, dry_run)
        _ingest_stream(iter_thread_summary_chunks(since=since), stats, dry_run=dry_run)
    if "note_summary" in entity_types:
        log.info("Ingesting note summaries (since=%s, dry_run=%s)...", since, dry_run)
        _ingest_stream(iter_note_summary_chunks(since=since), stats, dry_run=dry_run)
    if "todo" in entity_types:
        log.info("Ingesting todos (since=%s, dry_run=%s)...", since, dry_run)
        _ingest_stream(iter_todo_chunks(since=since), stats, dry_run=dry_run)
    if "conversation" in entity_types:
        log.info("Ingesting conversations (since=%s, dry_run=%s)...", since, dry_run)
        _ingest_stream(iter_conversation_chunks(since=since), stats, dry_run=dry_run)
    if "spotlight_answer" in entity_types:
        log.info("Ingesting spotlight answers (since=%s, dry_run=%s)...", since, dry_run)
        _ingest_stream(iter_spotlight_answer_chunks(since=since), stats, dry_run=dry_run)

    # Refresh-deferred bulk: with `RAG_BULK_REFRESH=false` (the default),
    # individual `_bulk()` calls skip server-side refresh, leaving the
    # written segments invisible to search until we call refresh here.
    # One refresh at the end of the run beats ~N refreshes at one per
    # entity batch — both for throughput AND for total wall-clock.
    # Skipped in dry_run since nothing was written.
    if not dry_run and not settings.SEARCH_ENGINE.get("RAG_BULK_REFRESH", False):
        try:
            client = get_client()
            client.indices.refresh(index=get_index_alias())
            log.info("Refresh complete (deferred-refresh mode).")
        except Exception:  # noqa: BLE001 — refresh failure is non-fatal
            log.exception(
                "Final refresh failed; documents will become searchable on next refresh."
            )

    log.info("Ingestion done: %s", stats.as_dict())
    return stats


def ingest_conversation_run(run) -> bool:
    """Index ONE completed `AgentRun` into the conversation lane,
    immediately searchable (C1 near-real-time hook, §4.7).

    Called by `agent_views._stream_ndjson` right after a run is saved
    with `status="done"` — off the user-visible stream (the last byte
    has already been sent), so the ~1 embed call + bulk write + refresh
    it costs never delays an answer. Returns True when a chunk was
    indexed, False when the run isn't durable memory (wrong status,
    empty answer, missing scope).

    Idempotent with the 10-minute incremental reindexer that remains
    the backstop: `_ingest_entity` hash-diffs against `RagChunk`, so
    when the cron re-sees this run the chunk is `unchanged` and no
    re-embed happens. The explicit refresh matters because the default
    deferred-refresh mode (`RAG_BULK_REFRESH=false`) leaves bulk writes
    invisible to search until *some* refresh runs — without it the
    "recall what we discussed a minute ago" case would silently wait
    for the next cron pass.
    """
    entity = conversation_chunks_for_run(run)
    if entity is None:
        return False
    stats = IngestionStats()
    # The ask path rebinds its own request around this call, so there the
    # inner bind is a no-op and the embed is grouped — and priced — as
    # part of what that ask cost. Any other caller (the cron backstop,
    # a backfill) attributes to `index` rather than `unattributed`.
    with spend.spend_context(surface="index"), spend.spend_purpose("index"):
        _ingest_entity(entity, stats, dry_run=False)
    if not settings.SEARCH_ENGINE.get("RAG_BULK_REFRESH", False):
        try:
            get_client().indices.refresh(index=get_index_alias())
        except Exception:  # noqa: BLE001 — refresh failure is non-fatal
            log.exception(
                "Post-conversation refresh failed; the chunk becomes searchable on next refresh."
            )
    return True


def _ingest_stream(
    stream: Iterable[EntityChunks], stats: IngestionStats, *, dry_run: bool
) -> None:
    for entity in stream:
        try:
            _ingest_entity(entity, stats, dry_run=dry_run)
            stats.entities_processed += 1
        except Exception as e:  # noqa: BLE001 — keep one bad entity from killing run
            log.exception("Failed to ingest entity %s/%s", entity.entity_type, entity.entity_id)
            stats.errors.append(f"{entity.entity_type}/{entity.entity_id}: {e}")


def _ingest_entity(entity: EntityChunks, stats: IngestionStats, *, dry_run: bool) -> None:
    if not entity.chunks:
        # Nothing to index, but we may still need to delete stale rows
        # that existed under this entity previously.
        if not dry_run:
            _delete_stale(entity.entity_type, entity.entity_id, keep_chunk_ids=set(), stats=stats)
        return

    model = get_active_embedding_model_name()

    # Compute hashes. Text and metadata are fingerprinted separately so
    # the two can be acted on independently — see `_meta_fingerprint`.
    incoming: dict[str, Chunk] = {}
    incoming_hashes: dict[str, str] = {}
    incoming_meta_hashes: dict[str, str] = {}
    for c in entity.chunks:
        incoming[c.chunk_id] = c
        incoming_hashes[c.chunk_id] = hash_text(c.search_text or "")
        incoming_meta_hashes[c.chunk_id] = _meta_fingerprint(c)

    # Look up existing tracking rows for this entity.
    existing_rows = {
        row.chunk_id: row
        for row in RagChunk.objects.filter(
            entity_type=entity.entity_type, entity_id=entity.entity_id
        )
    }

    new_ids: list[str] = []
    changed_ids: list[str] = []
    meta_only_ids: list[str] = []
    unchanged_ids: list[str] = []

    for cid, c in incoming.items():
        row = existing_rows.get(cid)
        if row is None:
            new_ids.append(cid)
        elif row.text_hash != incoming_hashes[cid] or row.embedding_model != model:
            changed_ids.append(cid)
        elif row.meta_hash != incoming_meta_hashes[cid]:
            # Same text, different overlay — refresh the doc, keep the
            # vector. This ordering matters: the text/model check above
            # already covers a re-embed, so reaching here guarantees the
            # stored embedding is still valid for this chunk.
            meta_only_ids.append(cid)
        else:
            unchanged_ids.append(cid)

    to_embed_ids = new_ids + changed_ids
    stats.chunks_total += len(incoming)
    stats.chunks_new += len(new_ids)
    stats.chunks_changed += len(changed_ids)
    stats.chunks_metadata_only += len(meta_only_ids)
    stats.chunks_unchanged += len(unchanged_ids)

    if dry_run:
        # Count stale rows for stats, but don't touch OpenSearch or
        # the tracking table.
        keep_ids = set(incoming.keys())
        stale_count = (
            RagChunk.objects.filter(entity_type=entity.entity_type, entity_id=entity.entity_id)
            .exclude(chunk_id__in=keep_ids)
            .count()
        )
        stats.chunks_deleted += stale_count
        return

    # Embed new + changed.
    embeddings_by_id: dict[str, list[float]] = {}
    if to_embed_ids:
        texts = [incoming[cid].search_text for cid in to_embed_ids]
        vectors = embed_texts(texts)
        for cid, vec in zip(to_embed_ids, vectors):
            embeddings_by_id[cid] = vec

    # Bulk index new + changed.
    if to_embed_ids:
        _bulk_index(
            [_to_os_doc(incoming[cid], embeddings_by_id[cid], model) for cid in to_embed_ids]
        )

    # Refresh metadata-only chunks in place. Separate bulk call from the
    # one above because these are `update` ops, not full `index` ops —
    # that is what preserves the existing embedding.
    if meta_only_ids:
        _bulk_index([_to_os_metadata_update(incoming[cid]) for cid in meta_only_ids])

    # Stale = previously tracked under this entity but not in current set.
    keep_ids = set(incoming.keys())
    _delete_stale(entity.entity_type, entity.entity_id, keep_chunk_ids=keep_ids, stats=stats)

    # Mirror to RagChunk.
    _upsert_tracking(
        entity_type=entity.entity_type,
        entity_id=entity.entity_id,
        chunks=incoming,
        hashes=incoming_hashes,
        meta_hashes=incoming_meta_hashes,
        model=model,
        affected_ids=set(to_embed_ids) | set(meta_only_ids),
    )


def _meta_fingerprint(chunk: Chunk) -> str:
    """Hash of everything the chunk carries EXCEPT `search_text`.

    Complement of `text_hash`, so between them the two cover the whole
    document: the overlays (`task_status`, `task_priority`,
    `task_assignee_id`, `task_milestone_id`, `task_sprint_id`,
    `note_owner_id`, ...), `acl_user_ids`, `title`, `related_entity_ids`
    and `updated_at`.

    Deliberately a whole-doc-minus-text hash rather than an enumerated
    field list: a chunker that starts emitting a new overlay is then
    covered automatically, whereas a hand-maintained list silently
    re-introduces this bug for whatever field someone forgets to add.

    `updated_at` being included means any touched entity gets one cheap
    doc update per pass. That is wanted, not incidental — the search
    layer re-ranks on document freshness and returns `updated_at` to the
    client, both of which were reading a stale value for the same reason
    the status chip was.
    """
    doc = chunk.to_dict()
    doc.pop("search_text", None)
    return hash_text(json.dumps(doc, sort_keys=True, ensure_ascii=False, default=str))


# Doc fields a metadata-only update must leave alone. The embedding pair
# is the entire point of this path — the stored vector stays valid
# because the text that produced it is unchanged — and `search_text` is
# by definition identical here (its hash is what routed the chunk down
# this path) as well as being the largest field in the document.
_META_UPDATE_SKIP_FIELDS = frozenset({"embedding", "embedding_model", "search_text"})


def _to_os_metadata_update(chunk: Chunk) -> dict:
    """Bulk action that refreshes a chunk's metadata in place.

    A partial `update` rather than the full `index` op `_to_os_doc`
    builds, so the existing embedding survives untouched: a status flip
    costs one cheap doc update and zero embedding calls.

    Nulls are written EXPLICITLY — built from `asdict`, not
    `Chunk.to_dict()`, which strips them. A partial update leaves keys
    it doesn't mention alone, so dropping a `None` would make a CLEARED
    field (task unassigned, milestone detached, ACL member removed) keep
    its old value forever, which is the very failure this path exists to
    fix.
    """
    doc = {k: v for k, v in asdict(chunk).items() if k not in _META_UPDATE_SKIP_FIELDS}
    doc["index_schema_version"] = INDEX_SCHEMA_VERSION
    return {
        "_op_type": "update",
        "_index": get_index_alias(),
        "_id": chunk.chunk_id,
        "doc": doc,
    }


def _to_os_doc(chunk: Chunk, embedding: list[float], model: str) -> dict:
    """Build the OpenSearch document for a chunk.

    The chunk_id is also used as the OpenSearch `_id` so that upsert
    semantics work naturally.
    """
    doc = chunk.to_dict()
    doc["embedding"] = embedding
    doc["embedding_model"] = model
    doc["index_schema_version"] = INDEX_SCHEMA_VERSION
    doc["text_hash"] = hash_text(chunk.search_text or "")
    return {
        "_index": get_index_alias(),
        "_id": chunk.chunk_id,
        "_source": doc,
    }


def _bulk_error_info(err) -> tuple[Optional[int], Optional[str]]:
    """Pull (status, error type) out of one `helpers.bulk` error entry.

    Each entry is `{<op>: {"status": ..., "error": {"type": ...}}}`, where
    `<op>` is the op type OpenSearch echoes back. Written defensively (and
    without naming the op) because this is an error path: a transport-level
    failure can hand back a shape that doesn't match, and a crash here
    would replace a logged warning with a traceback that aborts the run.
    """
    if not isinstance(err, dict):
        return None, None
    action = next(iter(err.values()), None)
    if not isinstance(action, dict):
        return None, None
    etype = action.get("error")
    etype = etype.get("type") if isinstance(etype, dict) else None
    return action.get("status"), etype


def _bulk_index(actions: list[dict]) -> None:
    if not actions:
        return
    batch_size = settings.SEARCH_ENGINE["BULK_BATCH_SIZE"]
    client = get_client()
    # `raise_on_error=False` so a single bad doc doesn't abort the
    # whole batch; we log instead.
    #
    # `refresh` strategy: default `RAG_BULK_REFRESH=false` means each
    # batch ships fire-and-forget (no `?refresh`), and `ingest_all`
    # issues one explicit `indices.refresh()` at the end of the full
    # run. Switching to `true` triggers per-batch refresh — useful for
    # one-off writes that need to be visible immediately (e.g. an
    # ad-hoc `manage.py shell` upsert).
    refresh_per_batch = settings.SEARCH_ENGINE.get("RAG_BULK_REFRESH", False)
    success, errors = os_helpers.bulk(
        client,
        actions,
        chunk_size=batch_size,
        raise_on_error=False,
        raise_on_exception=False,
        refresh=bool(refresh_per_batch),
    )
    # A 409 `version_conflict_engine_exception` is not a failure: it means
    # another writer updated this exact document between our read and our
    # write, so the doc already holds a value at least as fresh as ours.
    # Nothing is lost and there is nothing to retry — re-sending would just
    # rewrite identical metadata.
    #
    # It has to be filtered because concurrency is normal here, not
    # exceptional: the Railway cron overlaps itself by a minute by design,
    # `kick_off_demo_reindex` fires a full reindex per demo signin / team
    # join / starter seed, and `ingest_conversation_run` writes one chunk
    # per completed ask. Left at ERROR, the `CronCommand` tripwire reds a
    # run whose work in fact all landed — which trains the operator to
    # ignore a red reindex, the one signal that must stay trustworthy.
    #
    # Same shape and same argument as `purge.py`, which discounts a 404 on
    # delete ("the doc was already gone — that's the outcome we wanted").
    # Real errors still log at ERROR and still fail the run.
    benign, real_errors = [], []
    for err in errors:
        status, etype = _bulk_error_info(err)
        if status == 409 and etype == "version_conflict_engine_exception":
            benign.append(err)
        else:
            real_errors.append(err)

    if benign:
        # WARNING, so it stays visible (a spike means unexpected write
        # contention) without failing the run.
        log.warning(
            "Bulk index: %d doc(s) already updated by a concurrent writer "
            "(409, treated as success)",
            len(benign),
        )
    if real_errors:
        # ERROR (not WARNING) so a CronCommand-based reindex fails the run —
        # a bulk that wrote nothing must not leave the cron green.
        log.error("Bulk index reported %d errors (success=%d)", len(real_errors), success)
        for err in real_errors[:5]:
            log.warning("  %s", err)


def _delete_stale(
    entity_type: str,
    entity_id: str,
    keep_chunk_ids: set[str],
    stats: IngestionStats,
) -> None:
    """Delete chunks tracked for this entity but missing from the
    regeneration. Both OpenSearch and RagChunk are cleaned."""
    stale_ids = list(
        RagChunk.objects.filter(entity_type=entity_type, entity_id=entity_id)
        .exclude(chunk_id__in=keep_chunk_ids)
        .values_list("chunk_id", flat=True)
    )
    if not stale_ids:
        return

    actions = [
        {"_op_type": "delete", "_index": get_index_alias(), "_id": cid} for cid in stale_ids
    ]
    client = get_client()
    # Same deferred-refresh policy as `_bulk_index` — defers to the
    # end-of-run refresh in `ingest_all` unless RAG_BULK_REFRESH=true.
    refresh_per_batch = settings.SEARCH_ENGINE.get("RAG_BULK_REFRESH", False)
    _, del_errors = os_helpers.bulk(
        client,
        actions,
        raise_on_error=False,
        raise_on_exception=False,
        refresh=bool(refresh_per_batch),
    )
    # A 404 here is the outcome we wanted — the doc is gone, whether we
    # removed it or a concurrent run got there first. `purge.py` already
    # discounts exactly this on its own delete path; this one was left at
    # ERROR, so an overlapping reindex could red the cron for successfully
    # deleting an already-deleted chunk.
    real_del_errors = [e for e in del_errors if _bulk_error_info(e)[0] != 404]
    if real_del_errors:
        log.error("Stale-chunk delete reported %d errors", len(real_del_errors))
    RagChunk.objects.filter(chunk_id__in=stale_ids).delete()
    stats.chunks_deleted += len(stale_ids)


@transaction.atomic
def _upsert_tracking(
    *,
    entity_type: str,
    entity_id: str,
    chunks: dict[str, Chunk],
    hashes: dict[str, str],
    meta_hashes: dict[str, str],
    model: str,
    affected_ids: set[str],
) -> None:
    """Insert or update RagChunk rows for the chunks we just indexed.

    `affected_ids` are the ids we actually wrote to OpenSearch this pass
    — new + changed (re-embedded) plus metadata-only (refreshed in
    place). Both lanes must land here: skipping the metadata-only ones
    would leave `meta_hash` stale, so every subsequent pass would
    re-detect the same diff and re-issue the same update forever.
    Unchanged rows are left alone to avoid pointless writes.
    """
    # `sorted`, not bare iteration, and it is load-bearing.
    #
    # `update_or_create` is not the single statement it looks like: Django
    # wraps it in `SELECT ... FOR UPDATE` + `UPDATE` (see
    # `django/db/models/query.py`, "Lock the row so that a concurrent
    # update is blocked"). The `atomic` on this function is the OUTER
    # transaction, so Django's inner one degrades to a savepoint and every
    # row lock taken here is held until this whole loop commits.
    #
    # `affected_ids` arrives as a `set`, and `str` hashing is salted per
    # process (`PYTHONHASHSEED` is unset), so two processes iterate the
    # same ids in DIFFERENT orders. N held locks acquired in arbitrary
    # order across concurrent writers is the textbook deadlock, and it is
    # reachable: the Railway cron runs every 10 minutes with
    # `--since-minutes 11` (a deliberate overlap), `kick_off_demo_reindex`
    # fires a whole reindex on a daemon thread after every demo signin /
    # team join / starter-workspace seed, and `ingest_conversation_run`
    # re-enters this same function once per completed ask.
    #
    # Sorting gives every writer one global order, which is what makes the
    # cycle impossible. Concurrent runs still overlap — they now queue on a
    # row instead of one being killed. Postgres reported the cycle as
    # `DeadlockDetected ... while locking tuple (N,M) in relation
    # "search_engine_ragchunk"`, surfacing as `Failed to ingest entity
    # <type>/<id>` against an entity with nothing wrong with it, which is
    # why this cost real time to diagnose.
    #
    # This one line is the whole fix, confirmed by reverting it alone
    # against the dev DB (1448 chunks, `meta_hash` cleared, two concurrent
    # full reindexes): with it, 0 deadlocks; without it, 119-134 deadlock
    # lines and a failed run in 3 of 3 rounds. The `IN`-list order of the
    # *delete* paths was measured NOT to matter and deliberately left
    # alone — `EXPLAIN` shows a single bitmap/seq scan (so Postgres locks
    # in physical order, not list order) and those statements run in
    # autocommit, releasing at statement end. Only a held lock can join a
    # cycle, which is why this loop is the one place ordering is needed.
    for cid in sorted(affected_ids):
        c = chunks[cid]
        RagChunk.objects.update_or_create(
            chunk_id=cid,
            defaults={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "chunk_type": c.chunk_type,
                "team_id": c.team_id,
                "text_hash": hashes[cid],
                "meta_hash": meta_hashes[cid],
                "embedding_model": model,
                "index_schema_version": INDEX_SCHEMA_VERSION,
                "source_version": None,
            },
        )
