"""Purge deleted entities' chunks from OpenSearch + the RagChunk table.

Ingestion (`ingestion.py`) only cleans up chunks when an entity is
*regenerated* — and every chunker filters deleted rows out of iteration,
so an entity that is deleted (hard `.delete()` or an `is_deleted` /
`deleted_at` soft flag) is simply never revisited and its chunks stay
searchable forever. This module is the missing delete path. Two layers:

  * `purge_entities(pairs)` / `purge_chunks(ids)` — exact removal for
    callers that already know what died. The `purge_*` wrappers below
    are best-effort (never raise) so the delete views can call them
    in-request without adding a failure mode; a lost purge is retried
    by the sweep.
  * `sweep_orphans()` — walk every entity tracked in `RagChunk`, verify
    the backing row still exists (and is live where the model
    soft-deletes), and purge the dead ones. Runs after every
    `opensearch_reindex` pass, so cleanup lag is bounded by the cron
    cadence even for paths with no hook (project-delete cascades,
    channel soft-deletes, demo-user cleanup, ...).

Fail-safe rules the sweep MUST keep:
  * An entity id that doesn't parse, or an entity_type the sweep doesn't
    know, is KEPT and logged — never purged. A parser bug must not be
    able to empty the index.
  * OpenSearch deletes run before the RagChunk rows are removed, so a
    failed bulk leaves the tracking rows in place and the next sweep
    retries.

Known limitation: a `spotlight_answer` / `conversation` chunk is purged
only when its `AgentRun` row is gone. An old answer whose *sources* were
since deleted keeps its (already-collected) text until the run itself is
deleted — re-auditing every run's source ACLs per sweep is deliberately
out of scope (see `source_visibility.py` for the collection-time rule).
"""

from __future__ import annotations

import logging
import uuid as uuid_mod
from collections import defaultdict
from typing import Iterable, Optional

from opensearchpy import helpers as os_helpers

from origin.search_engine.models import RagChunk
from origin.search_engine.opensearch_client import get_client, get_index_alias

log = logging.getLogger(__name__)

_BATCH = 500


# --------------------------------------------------------------------------- #
# Core purge                                                                  #
# --------------------------------------------------------------------------- #


def purge_chunks(chunk_ids: list[str], *, refresh: bool = True) -> int:
    """Delete specific chunks from OpenSearch and RagChunk. Returns the
    number of tracking rows removed. Raises on OpenSearch transport
    failure (RagChunk rows are then kept so the sweep can retry)."""
    if not chunk_ids:
        return 0
    alias = get_index_alias()
    actions = [{"_op_type": "delete", "_index": alias, "_id": cid} for cid in chunk_ids]
    _, errors = os_helpers.bulk(
        get_client(),
        actions,
        chunk_size=_BATCH,
        raise_on_error=False,
        raise_on_exception=False,
        refresh=refresh,
    )
    # A 404 means the doc was already gone (e.g. tracking drift) — that's
    # the outcome we wanted; only report real failures.
    real_errors = [
        e for e in errors if (e.get("delete") or {}).get("status") != 404
    ]
    if real_errors:
        log.error("Purge bulk reported %d errors", len(real_errors))
        for err in real_errors[:5]:
            log.warning("  %s", err)
    deleted = 0
    for i in range(0, len(chunk_ids), _BATCH):
        batch = chunk_ids[i : i + _BATCH]
        deleted += RagChunk.objects.filter(chunk_id__in=batch).delete()[0]
    return deleted


def purge_entities(pairs: Iterable[tuple[str, str]], *, refresh: bool = True) -> int:
    """Delete every chunk tracked under the given (entity_type, entity_id)
    pairs. Returns the number of chunks removed."""
    by_type: dict[str, list[str]] = defaultdict(list)
    for etype, eid in pairs:
        by_type[etype].append(eid)

    chunk_ids: list[str] = []
    for etype, eids in by_type.items():
        for i in range(0, len(eids), _BATCH):
            chunk_ids.extend(
                RagChunk.objects.filter(
                    entity_type=etype, entity_id__in=eids[i : i + _BATCH]
                ).values_list("chunk_id", flat=True)
            )
    return purge_chunks(chunk_ids, refresh=refresh)


# --------------------------------------------------------------------------- #
# Best-effort hooks for the delete views                                      #
# --------------------------------------------------------------------------- #
#
# All of these swallow every exception: a purge failure must never fail
# the user's delete. The orphan sweep retries anything a hook missed.

_NOTE_TYPE_CODE = {"personal": 1, "task": 2, "chat": 3}


def purge_task(task_id) -> None:
    """Task deleted (hard delete, or milestone-path soft delete)."""
    try:
        purge_entities([("task", f"task:{task_id}")])
    except Exception:  # noqa: BLE001
        log.exception("Best-effort purge failed for task %s", task_id)


def purge_milestone(milestone_id) -> None:
    try:
        purge_entities([("milestone", f"milestone:{milestone_id}")])
    except Exception:  # noqa: BLE001
        log.exception("Best-effort purge failed for milestone %s", milestone_id)


def purge_note(note_type_label: str, note_id) -> None:
    """Note deleted. Also purges the note's summary chunks — the
    `NoteSummary` row is keyed (note_type, note_id) with no FK, so it
    survives the note's hard delete."""
    code = _NOTE_TYPE_CODE.get(note_type_label)
    pairs = [("note", f"note:{note_type_label}:{note_id}")]
    if code is not None:
        pairs.append(("note_summary", f"note_summary:{code}:{note_id}"))
    try:
        purge_entities(pairs)
    except Exception:  # noqa: BLE001
        log.exception("Best-effort purge failed for note %s:%s", note_type_label, note_id)


def purge_todo_item(item_id) -> None:
    """Todo item deleted. The entity id embeds the group's local_date
    (`todo:<date>:item:<id>`) which the caller no longer has after the
    delete, so match on the stable `:item:<id>` suffix instead."""
    try:
        pairs = [
            ("todo", eid)
            for eid in RagChunk.objects.filter(
                entity_type="todo", entity_id__endswith=f":item:{item_id}"
            )
            .values_list("entity_id", flat=True)
            .distinct()
        ]
        if pairs:
            purge_entities(pairs)
    except Exception:  # noqa: BLE001
        log.exception("Best-effort purge failed for todo item %s", item_id)


def purge_task_comment(task_id, comment_id) -> None:
    """Task comment soft-deleted. Chunk-level (not entity-level): the
    task entity stays live, only the one comment chunk dies."""
    try:
        purge_chunks([f"task:{task_id}:comment:{comment_id}"])
    except Exception:  # noqa: BLE001
        log.exception(
            "Best-effort purge failed for task %s comment %s", task_id, comment_id
        )


# --------------------------------------------------------------------------- #
# Orphan sweep                                                                #
# --------------------------------------------------------------------------- #


def sweep_orphans(*, dry_run: bool = False) -> dict:
    """Purge chunks whose backing entity no longer exists (or is
    soft-deleted). Driven by the RagChunk tracking table — an OpenSearch
    doc with no tracking row is invisible here (that drift is healed by
    `opensearch_setup --recreate` + full reindex, not by this sweep).

    Returns a stats dict; with `dry_run` nothing is deleted and the
    stats report what *would* be purged.
    """
    stats = {
        "entities_scanned": 0,
        "entities_purged": 0,
        "chunks_purged": 0,
        "kept_unparseable": 0,
        "kept_unknown_type": 0,
        "purged_by_type": defaultdict(int),
    }

    ids_by_type: dict[str, list[str]] = defaultdict(list)
    for etype, eid in (
        RagChunk.objects.values_list("entity_type", "entity_id").distinct().iterator()
    ):
        ids_by_type[etype].append(eid)
        stats["entities_scanned"] += 1

    dead_pairs: list[tuple[str, str]] = []
    for etype, eids in ids_by_type.items():
        resolver = _RESOLVERS.get(etype)
        if resolver is None:
            # Unknown type (e.g. a chunker added without updating the
            # sweep): keep, loudly. Purging here would delete live data.
            log.warning(
                "Orphan sweep: unknown entity_type %r (%d entities) — skipped",
                etype,
                len(eids),
            )
            stats["kept_unknown_type"] += len(eids)
            continue
        for i in range(0, len(eids), _BATCH):
            batch = eids[i : i + _BATCH]
            dead, unparseable = resolver(batch)
            stats["kept_unparseable"] += unparseable
            for eid in dead:
                dead_pairs.append((etype, eid))
                stats["purged_by_type"][etype] += 1

    stats["entities_purged"] = len(dead_pairs)
    if dry_run:
        stats["chunks_purged"] = sum(
            RagChunk.objects.filter(entity_type=etype, entity_id=eid).count()
            for etype, eid in dead_pairs
        )
    elif dead_pairs:
        # Defer per-batch refresh; one refresh at the end (mirrors
        # ingest_all's deferred-refresh policy).
        stats["chunks_purged"] = purge_entities(dead_pairs, refresh=False)
        try:
            get_client().indices.refresh(index=get_index_alias())
        except Exception:  # noqa: BLE001 — refresh failure is non-fatal
            log.exception("Post-sweep refresh failed; deletes apply on next refresh.")

    stats["purged_by_type"] = dict(stats["purged_by_type"])
    return stats


# --------------------------------------------------------------------------- #
# Per-type liveness resolvers                                                 #
#                                                                             #
# Each takes a batch of entity ids and returns (dead_ids, unparseable_count). #
# "Live" mirrors the corresponding chunker's iteration filter — an entity     #
# the chunker would still emit must never be reported dead.                   #
# --------------------------------------------------------------------------- #


def _int_after(eid: str, prefix: str) -> Optional[int]:
    if not eid.startswith(prefix):
        return None
    try:
        return int(eid[len(prefix) :])
    except ValueError:
        return None


def _uuid(value: str):
    try:
        return uuid_mod.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def _resolve_tasks(eids: list[str]) -> tuple[set[str], int]:
    from origin.models.task.task_models import TaskMaster

    by_pk: dict[int, str] = {}
    unparseable = 0
    for eid in eids:
        pk = _int_after(eid, "task:")
        if pk is None:
            unparseable += 1
        else:
            by_pk[pk] = eid
    live = set(
        TaskMaster.objects.filter(
            task_id__in=by_pk.keys(), is_deleted=False, is_init_task=False
        ).values_list("task_id", flat=True)
    )
    return {eid for pk, eid in by_pk.items() if pk not in live}, unparseable


def _resolve_milestones(eids: list[str]) -> tuple[set[str], int]:
    from origin.models.task.milestone_models import MilestoneMaster

    by_pk: dict[int, str] = {}
    unparseable = 0
    for eid in eids:
        pk = _int_after(eid, "milestone:")
        if pk is None:
            unparseable += 1
        else:
            by_pk[pk] = eid
    live = set(
        MilestoneMaster.objects.filter(
            milestone_id__in=by_pk.keys(), is_deleted=False
        ).values_list("milestone_id", flat=True)
    )
    return {eid for pk, eid in by_pk.items() if pk not in live}, unparseable


def _note_model(code_or_label):
    from origin.models.note.chat_note_models import ChatNoteMaster
    from origin.models.note.personal_note_models import PersonalNoteMaster
    from origin.models.note.task_note_models import TaskNoteMaster

    return {
        "personal": PersonalNoteMaster,
        "task": TaskNoteMaster,
        "chat": ChatNoteMaster,
        1: PersonalNoteMaster,
        2: TaskNoteMaster,
        3: ChatNoteMaster,
    }.get(code_or_label)


def _resolve_notes(eids: list[str]) -> tuple[set[str], int]:
    # note:<label>:<id>
    by_label: dict[str, dict[int, str]] = defaultdict(dict)
    unparseable = 0
    for eid in eids:
        parts = eid.split(":")
        pk = None
        if len(parts) == 3 and parts[0] == "note":
            try:
                pk = int(parts[2])
            except ValueError:
                pk = None
        if pk is None or _note_model(parts[1]) is None:
            unparseable += 1
            continue
        by_label[parts[1]][pk] = eid

    dead: set[str] = set()
    for label, by_pk in by_label.items():
        model = _note_model(label)
        live = set(
            model.objects.filter(note_id__in=by_pk.keys()).values_list("note_id", flat=True)
        )
        dead.update(eid for pk, eid in by_pk.items() if pk not in live)
    return dead, unparseable


def _resolve_note_summaries(eids: list[str]) -> tuple[set[str], int]:
    # note_summary:<code>:<id> — live iff the NoteSummary row AND the
    # underlying note both still exist (the summary row has no FK, so it
    # survives the note's hard delete).
    from origin.search_engine.models import NoteSummary

    by_code: dict[int, dict[int, str]] = defaultdict(dict)
    unparseable = 0
    for eid in eids:
        parts = eid.split(":")
        code = pk = None
        if len(parts) == 3 and parts[0] == "note_summary":
            try:
                code, pk = int(parts[1]), int(parts[2])
            except ValueError:
                code = pk = None
        if pk is None or _note_model(code) is None:
            unparseable += 1
            continue
        by_code[code][pk] = eid

    dead: set[str] = set()
    for code, by_pk in by_code.items():
        model = _note_model(code)
        live_notes = set(
            model.objects.filter(note_id__in=by_pk.keys()).values_list("note_id", flat=True)
        )
        live_summaries = set(
            NoteSummary.objects.filter(
                note_type=code, note_id__in=by_pk.keys()
            ).values_list("note_id", flat=True)
        )
        live = live_notes & live_summaries
        dead.update(eid for pk, eid in by_pk.items() if pk not in live)
    return dead, unparseable


def _live_channel_uuids(by_kind: dict[int, set]) -> set:
    from origin.models.chat.unified_models import Channel

    live: set = set()
    for kind, cuuids in by_kind.items():
        live.update(
            Channel.objects.filter(
                kind=kind, id__in=cuuids, is_deleted=False
            ).values_list("id", flat=True)
        )
    return live


def _roots_with_live_replies(root_uuids: set) -> set:
    """Roots that still anchor a chat *thread entity*. Mirrors
    chat_chunker emission exactly: a thread entity exists iff at least
    one live reply points at the root (a live root with no live replies
    is a plain main-timeline message — its old thread chunks are dead)."""
    from origin.models.chat.unified_models import Message

    return set(
        Message.objects.filter(
            thread_root_id__in=root_uuids, deleted_at__isnull=True
        ).values_list("thread_root_id", flat=True)
    )


def _roots_with_any_live_message(root_uuids: set) -> set:
    """Roots whose thread still has ANY live message (root or reply).
    Used for `thread_summary` liveness — the summary text derives from
    the thread's messages, so it dies only when they're all gone."""
    from origin.models.chat.unified_models import Message

    live = set(
        Message.objects.filter(id__in=root_uuids, deleted_at__isnull=True).values_list(
            "id", flat=True
        )
    )
    live.update(_roots_with_live_replies(root_uuids - live))
    return live


def _chat_kind_by_label() -> dict[str, int]:
    from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL

    return {label: code for code, label in CHAT_TYPE_LABEL.items()}


def _resolve_chats(eids: list[str]) -> tuple[set[str], int]:
    # <label>:<channel-uuid>                  (main timeline)
    # <label>:<channel-uuid>:thread:<uuid>    (thread)
    kind_by_label = _chat_kind_by_label()
    unparseable = 0
    parsed: list[tuple[str, int, object, Optional[object]]] = []
    for eid in eids:
        parts = eid.split(":")
        kind = kind_by_label.get(parts[0])
        cuuid = _uuid(parts[1]) if len(parts) >= 2 else None
        root = None
        if len(parts) == 4 and parts[2] == "thread":
            root = _uuid(parts[3])
            ok = kind is not None and cuuid is not None and root is not None
        else:
            ok = len(parts) == 2 and kind is not None and cuuid is not None
        if not ok:
            unparseable += 1
            continue
        parsed.append((eid, kind, cuuid, root))

    by_kind: dict[int, set] = defaultdict(set)
    root_uuids: set = set()
    for _, kind, cuuid, root in parsed:
        by_kind[kind].add(cuuid)
        if root is not None:
            root_uuids.add(root)

    live_channels = _live_channel_uuids(by_kind)
    live_roots = _roots_with_live_replies(root_uuids) if root_uuids else set()

    dead: set[str] = set()
    for eid, _kind, cuuid, root in parsed:
        if cuuid not in live_channels:
            dead.add(eid)
        elif root is not None and root not in live_roots:
            dead.add(eid)
    return dead, unparseable


def _resolve_thread_summaries(eids: list[str]) -> tuple[set[str], int]:
    # thread_summary:<chat_type>:<channel-uuid>:<thread-uuid> — live iff
    # the ThreadSummary row exists AND the channel is live AND the thread
    # still has a live message.
    from origin.search_engine.models import ThreadSummary

    unparseable = 0
    parsed: list[tuple[str, int, object, object]] = []
    for eid in eids:
        parts = eid.split(":")
        ok = len(parts) == 4 and parts[0] == "thread_summary"
        code = cuuid = root = None
        if ok:
            try:
                code = int(parts[1])
            except ValueError:
                ok = False
        if ok:
            cuuid, root = _uuid(parts[2]), _uuid(parts[3])
            ok = cuuid is not None and root is not None
        if not ok:
            unparseable += 1
            continue
        parsed.append((eid, code, cuuid, root))

    if not parsed:
        return set(), unparseable

    triples = {(code, cuuid, root) for _, code, cuuid, root in parsed}
    existing = set(
        ThreadSummary.objects.filter(
            chat_type__in={t[0] for t in triples},
            chat_id__in={t[1] for t in triples},
            thread_id__in={t[2] for t in triples},
        ).values_list("chat_type", "chat_id", "thread_id")
    )

    by_kind: dict[int, set] = defaultdict(set)
    for _, code, cuuid, _root in parsed:
        by_kind[code].add(cuuid)
    live_channels = _live_channel_uuids(by_kind)
    live_roots = _roots_with_any_live_message({root for _, _, _, root in parsed})

    dead: set[str] = set()
    for eid, code, cuuid, root in parsed:
        if (
            (code, cuuid, root) not in existing
            or cuuid not in live_channels
            or root not in live_roots
        ):
            dead.add(eid)
    return dead, unparseable


def _resolve_todos(eids: list[str]) -> tuple[set[str], int]:
    # todo:<YYYY-MM-DD>:item:<id>
    from origin.models.chat.todo_models import ToDoItem

    by_pk: dict[int, list[str]] = defaultdict(list)
    unparseable = 0
    for eid in eids:
        parts = eid.split(":")
        pk = None
        if len(parts) == 4 and parts[0] == "todo" and parts[2] == "item":
            try:
                pk = int(parts[3])
            except ValueError:
                pk = None
        if pk is None:
            unparseable += 1
            continue
        by_pk[pk].append(eid)
    live = set(
        ToDoItem.objects.filter(item_id__in=by_pk.keys()).values_list("item_id", flat=True)
    )
    dead: set[str] = set()
    for pk, ids in by_pk.items():
        if pk not in live:
            dead.update(ids)
    return dead, unparseable


def _resolve_agent_runs(prefix: str):
    def resolve(eids: list[str]) -> tuple[set[str], int]:
        from origin.search_engine.models import AgentRun

        by_pk: dict[object, str] = {}
        unparseable = 0
        for eid in eids:
            run_uuid = _uuid(eid[len(prefix) :]) if eid.startswith(prefix) else None
            if run_uuid is None:
                unparseable += 1
            else:
                by_pk[run_uuid] = eid
        live = set(
            AgentRun.objects.filter(run_id__in=by_pk.keys()).values_list("run_id", flat=True)
        )
        return {eid for pk, eid in by_pk.items() if pk not in live}, unparseable

    return resolve


_RESOLVERS = {
    "task": _resolve_tasks,
    "milestone": _resolve_milestones,
    "note": _resolve_notes,
    "note_summary": _resolve_note_summaries,
    "chat": _resolve_chats,
    "thread_summary": _resolve_thread_summaries,
    "todo": _resolve_todos,
    "conversation": _resolve_agent_runs("conversation:"),
    "spotlight_answer": _resolve_agent_runs("spotlight_answer:"),
}
