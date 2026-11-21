"""Thread summaries that outlived their thread (MIP-1802): count them, and remove them on request.

Until MIP-1802 nothing deleted a `ThreadSummary` (see `thread_summary_cleanup`), so a deleted
reply, a deleted thread, a deleted chat and a torn-down team each left a digest of the replies'
text behind. New deletes clean up after themselves; this finds the rows the old ones left.

A row is an orphan for the FIRST of these reasons that holds (counted per reason, never listed):

* `chat_missing`: no chat of that kind and id (hard-deleted, or the row names nothing);
* `chat_deleted`: the chat is soft-deleted (a deleted group, or a deleted project's chat);
* `thread_missing`: the thread-root message is gone;
* `no_live_reply`: the digest is made of the replies and none is left (all deleted);
* `text_of_deleted_message`: a message of the thread was deleted AFTER the digest was written, so
  the digest may quote it. (A message deleted before the digest was made is not in it: the ask
  loads live messages only.) The next ask writes a fresh digest from what is left.

The rule is deliberately stricter than the search sweep's (`purge._resolve_thread_summaries`),
which keeps a chunk while the thread has any live message, root included.

Counts only: no title, text, id or name is ever returned. Batched by primary key, so a large table
is never read at once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Q

from origin.models.chat.unified_models import Channel, Message

log = logging.getLogger("origin.mcp")

REASONS = (
    "chat_missing",
    "chat_deleted",
    "thread_missing",
    "no_live_reply",
    "text_of_deleted_message",
)
_BATCH = 500


@dataclass
class PruneResult:
    matched: int
    changed: int  # rows deleted (0 on a dry run)
    by_reason: dict = field(default_factory=dict)  # reason -> rows (counts only)
    dry_run: bool = True
    # Rows that are NOT orphans but whose digest is older than the newest edit of a live message
    # of their thread (MIP-1819): edits made before edits forgot the summary. A count only; `--apply`
    # does not remove them.
    edited_since_digest: int = 0


def _reasons(rows: list[dict]) -> dict[int, str]:
    """`{summary pk: reason}` for the orphans among `rows` (one batch)."""
    chat_ids = {r["chat_id"] for r in rows}
    thread_ids = {r["thread_id"] for r in rows}

    channels = {
        (kind, cid): deleted
        for cid, kind, deleted in Channel._base_manager.filter(id__in=chat_ids).values_list(
            "id", "kind", "is_deleted"
        )
    }
    roots = set(
        Message._base_manager.filter(
            id__in=thread_ids, channel_id__in=chat_ids, is_thread_reply=False
        ).values_list("id", "channel_id")
    )
    live_replies = set(
        Message._base_manager.filter(
            thread_root_id__in=thread_ids,
            channel_id__in=chat_ids,
            is_thread_reply=True,
            deleted_at__isnull=True,
        )
        .values_list("thread_root_id", "channel_id")
        .distinct()
    )
    last_deleted: dict[tuple, object] = {}
    for mid, root_id, cid, deleted_at in Message._base_manager.filter(
        Q(thread_root_id__in=thread_ids) | Q(id__in=thread_ids),
        channel_id__in=chat_ids,
        deleted_at__isnull=False,
    ).values_list("id", "thread_root_id", "channel_id", "deleted_at"):
        key = (root_id or mid, cid)
        if key not in last_deleted or deleted_at > last_deleted[key]:
            last_deleted[key] = deleted_at

    out: dict[int, str] = {}
    for r in rows:
        key = (r["thread_id"], r["chat_id"])
        chat = (r["chat_type"], r["chat_id"])
        if chat not in channels:
            reason = "chat_missing"
        elif channels[chat]:
            reason = "chat_deleted"
        elif key not in roots:
            reason = "thread_missing"
        elif key not in live_replies:
            reason = "no_live_reply"
        elif key in last_deleted and last_deleted[key] > r["ts_updated_at"]:
            reason = "text_of_deleted_message"
        else:
            continue
        out[r["pk"]] = reason
    return out


def _edited_since_digest(rows: list[dict]) -> set[int]:
    """Pks of `rows` whose digest predates the newest edit of a live message of the thread."""
    chat_ids = {r["chat_id"] for r in rows}
    thread_ids = {r["thread_id"] for r in rows}
    newest: dict[tuple, object] = {}
    for mid, root_id, cid, edited_at in Message._base_manager.filter(
        Q(thread_root_id__in=thread_ids) | Q(id__in=thread_ids),
        channel_id__in=chat_ids,
        deleted_at__isnull=True,
        edited_at__isnull=False,
    ).values_list("id", "thread_root_id", "channel_id", "edited_at"):
        key = (root_id or mid, cid)
        if key not in newest or edited_at > newest[key]:
            newest[key] = edited_at
    return {
        r["pk"]
        for r in rows
        if (r["thread_id"], r["chat_id"]) in newest
        and newest[(r["thread_id"], r["chat_id"])] > r["ts_updated_at"]
    }


def prune(*, apply: bool = False) -> PruneResult:
    """Count (default) or remove (`apply=True`) the orphaned thread summaries and their chunks."""
    from origin.search_engine.models import ThreadSummary
    from origin.search_engine.purge import purge_thread_summaries

    by_reason = dict.fromkeys(REASONS, 0)
    changed = 0
    edited = 0
    last_pk = 0
    while True:
        rows = list(
            ThreadSummary.objects.filter(pk__gt=last_pk)
            .order_by("pk")
            .values("pk", "chat_type", "chat_id", "thread_id", "ts_updated_at")[:_BATCH]
        )
        if not rows:
            break
        last_pk = rows[-1]["pk"]
        found = _reasons(rows)
        for reason in found.values():
            by_reason[reason] += 1
        edited += len(_edited_since_digest([r for r in rows if r["pk"] not in found]))
        if apply and found:
            gone = [r for r in rows if r["pk"] in found]
            with transaction.atomic():
                changed += ThreadSummary.objects.filter(pk__in=found).delete()[0]
            purge_thread_summaries((r["chat_type"], r["chat_id"], r["thread_id"]) for r in gone)
    matched = sum(by_reason.values())
    return PruneResult(matched, changed, by_reason, dry_run=not apply, edited_since_digest=edited)
