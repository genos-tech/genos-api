"""Dual-write helper for Track B Phase 1 of the chat system rewrite.

Every legacy `(dm|gm|pm|mdm)/messages/` POST/PUT (and the reaction /
mention / read-status writers) calls into this module to insert a
matching row in the unified `Channel` / `Message` / `MessageReaction` /
`MessageMention` / `ReadCursor` schema. After the Phase 6 cutover the
legacy callers go away and only the unified path remains.

Design notes:

- All writes are idempotent. The dual-write helper uses `update_or_create`
  / `get_or_create` keyed on natural identity (`(channel, seq)` for
  messages, `(channel, user)` for members, etc.). Re-running is safe;
  the drift cron lifts the lid on any divergence.

- Channel resolution is a single indexed read on `(kind, legacy_chat_id)`
  — the partial unique constraint added in migration `0126`. If the
  channel isn't found, the helper logs and returns `None`. It does NOT
  lazy-create channels here: per the rollout plan, dual-write is enabled
  AFTER the backfill has populated every legacy row's `Channel`, so a
  missing channel is a signal that the operator turned on the flag too
  early. Logging it loudly is the right escalation.

- All exceptions are caught and logged. The legacy write must succeed
  even if the unified mirror fails — dual-write is "best effort" until
  the cutover. The drift cron is the safety net that catches mirror
  failures over a 1h window.

- The whole module is a no-op when `settings.UNIFIED_MESSAGING_DUAL_WRITE`
  is False (default). Callers should still invoke the helper
  unconditionally; the flag check lives at the top of each entry point.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from django.conf import settings
from django.db import transaction
from django.db.models import F

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    Flag,
    Message,
    Pin,
)
from origin.services import (
    message_tombstone,
    message_via,
    realtime_push,
    task_card,
    task_card_post,
    thread_summary_cleanup,
)
from origin.services.activity_suppression import activity_suppressed

logger = logging.getLogger(__name__)

# Deterministic UUIDv5 namespace for task-comment → v3 thread-reply rows.
# `uuid5(NS, f"{task_id}-{comment_id}")` is stable for the lifetime of a
# comment, so re-running the backfill (or the dual-write retrying on a
# transient failure) idempotently collides on the existing Message row
# instead of creating duplicates.
_TASK_COMMENT_UUID_NS = uuid.UUID("3e4c8b1d-7f2a-4e9c-9b6d-5a1f8e2d3c4b")


def task_comment_message_uuid(task_id: int, comment_id: int) -> uuid.UUID:
    """Deterministic v3 `Message.id` for a (task_id, comment_id) pair.

    Used for both the dual-write at create time and the backfill. The
    fixed namespace + stable input means every caller for the same pair
    lands at the same UUID, which gives natural idempotency via the
    `Message.id` primary key.
    """
    return uuid.uuid5(_TASK_COMMENT_UUID_NS, f"{int(task_id)}-{int(comment_id)}")


# ---- Helpers ----------------------------------------------------------------


def _is_enabled() -> bool:
    """Single point of feature-flag gating. The caller is expected to
    invoke each writer unconditionally; we no-op here when the flag is
    off so the legacy code path stays clean of `if`s."""
    return bool(getattr(settings, "UNIFIED_MESSAGING_DUAL_WRITE", False))


def _kind_from_chat_type(chat_type: int) -> Optional[int]:
    """`chat_type` (1=DM/2=GM/3=PM/4=MDM) → `ChannelKind` int. Returns
    None for an unknown chat_type so callers can short-circuit."""
    try:
        return ChannelKind(int(chat_type)).value
    except (ValueError, TypeError):
        logger.warning("[unified_writer] unknown chat_type=%r", chat_type)
        return None


def _resolve_channel(chat_type: int, chat_id: int) -> Optional[Channel]:
    """Find the unified Channel for a legacy `(chat_type, chat_id)` pair.

    Uses the partial unique index on `(kind, legacy_chat_id)` (migration
    0126) for an O(1) indexed read. A miss means the channel hasn't
    been backfilled yet — log and return None.
    """
    kind = _kind_from_chat_type(chat_type)
    if kind is None:
        return None
    try:
        legacy_chat_id_int = int(chat_id)
    except (ValueError, TypeError):
        logger.warning(
            "[unified_writer] non-numeric chat_id=%r for chat_type=%s", chat_id, chat_type
        )
        return None
    channel = Channel.objects.filter(kind=kind, legacy_chat_id=legacy_chat_id_int).first()
    if channel is None:
        logger.warning(
            "[unified_writer] no Channel for (chat_type=%s, chat_id=%s) — "
            "backfill must run before UNIFIED_MESSAGING_DUAL_WRITE is enabled",
            chat_type,
            chat_id,
        )
    return channel


def _resolve_message(chat_type: int, chat_id: int, message_id: int) -> Optional[Message]:
    """Find a unified Message by its legacy `(chat_type, chat_id, seq)`
    composite. Returns None on a miss (logged)."""
    channel = _resolve_channel(chat_type, chat_id)
    if channel is None:
        return None
    try:
        seq_int = int(message_id)
    except (ValueError, TypeError):
        logger.warning("[unified_writer] non-numeric message_id=%r", message_id)
        return None
    return Message.objects.filter(channel=channel, seq=seq_int).first()


def _body_text_from_body(body: Any) -> str:
    """Best-effort `body_text` for the unified `Message`. Mirrors the
    backfill's logic so dual-written rows look the same as backfilled
    rows."""
    if not body:
        return ""
    try:
        # `generate_first_line` lives next to the legacy views and
        # accepts a single BlockNote block dict — same as how the
        # backfill computes it.
        from origin.views.chat.modules.common import generate_first_line

        return generate_first_line.get(body[0]) or ""
    except Exception:  # noqa: BLE001  — best effort, mirror only
        return ""


# ---- Public writers ---------------------------------------------------------


def write_message(
    *,
    chat_type: int,
    chat_id: int,
    message_id: int,
    sender_id: Optional[str],
    body: list,
    task_id: Optional[int] = None,
    is_deleted: bool = False,
) -> Optional[Message]:
    """Mirror a legacy `(DM|GM|PM|MDM)Messages` insert into `Message`.

    Idempotent via `(channel, seq)`. Re-callers (e.g. retries from the
    caller's view) won't produce duplicate rows.

    Returns the created/found Message on success, None on any failure
    (channel missing, exception, flag off). The legacy write is
    unaffected — this function does not raise.
    """
    if not _is_enabled():
        return None
    try:
        channel = _resolve_channel(chat_type, chat_id)
        if channel is None:
            return None
        with transaction.atomic():
            msg, created = Message.objects.get_or_create(
                channel=channel,
                seq=int(message_id),
                defaults={
                    "sender_id": sender_id,
                    "body": body or [],
                    "body_text": _body_text_from_body(body),
                    "task_id": task_id,
                    "is_thread_reply": False,
                    "metadata": {},
                    "reply_count": 0,
                },
            )
            # PUT path: the row already existed and the legacy view
            # is editing body / soft-deleting. Mirror the change.
            if not created:
                update_fields: list[str] = []
                # A deleted message takes no new text (MIP-1784).
                if body is not None and msg.body != body and msg.deleted_at is None:
                    msg.body = body
                    msg.body_text = _body_text_from_body(body)
                    update_fields.extend(["body", "body_text"])
                if update_fields:
                    msg.save(update_fields=update_fields)
                    thread_summary_cleanup.forget_message_thread(msg)
                # Soft-delete: legacy uses `is_deleted=True`; unified
                # uses `deleted_at` (nullable datetime), and the text goes with it.
                if is_deleted:
                    message_tombstone.tombstone_message(msg)
            return msg
    except Exception:  # noqa: BLE001 — never break the legacy write
        logger.exception(
            "[unified_writer] write_message failed for (chat_type=%s, chat_id=%s, message_id=%s)",
            chat_type,
            chat_id,
            message_id,
        )
        return None


def write_thread_message(
    *,
    chat_type: int,
    chat_id: int,
    thread_id: int,
    message_id: int,
    sender_id: Optional[str],
    body: list,
    is_deleted: bool = False,
) -> Optional[Message]:
    """Mirror a legacy `(DM|GM|PM|MDM)ThreadMessages` insert into
    `Message` with `is_thread_reply=True`.

    Resolves `parent` / `thread_root` from the legacy parent message via
    `(chat_type, chat_id, thread_id)` — `thread_id` in the legacy schema
    IS the parent message id. So `thread_root = parent` for the first
    reply, and we resolve the same parent for subsequent replies.

    Note: legacy thread `(chat_id, thread_id, thread_message_id)` is a
    3-tuple, but our unified schema uses `(channel, seq)` with `seq`
    drawn from a single per-channel counter. Backfill uses the legacy
    `thread_message_id` directly as `seq`, which means two parallel
    threads in the same channel can collide on `seq`. We mirror that
    behaviour here for consistency — drift cron will surface the
    collision if it happens. Track D's rewrite re-keys the unified
    `seq` to a single per-channel counter.
    """
    if not _is_enabled():
        return None
    try:
        channel = _resolve_channel(chat_type, chat_id)
        if channel is None:
            return None
        parent = _resolve_message(chat_type, chat_id, thread_id)
        if parent is None:
            logger.warning(
                "[unified_writer] thread parent not found for "
                "(chat_type=%s, chat_id=%s, thread_id=%s)",
                chat_type,
                chat_id,
                thread_id,
            )
            return None
        with transaction.atomic():
            msg, created = Message.objects.get_or_create(
                channel=channel,
                seq=int(message_id),
                defaults={
                    "sender_id": sender_id,
                    "body": body or [],
                    "body_text": _body_text_from_body(body),
                    "is_thread_reply": True,
                    "parent": parent,
                    "thread_root": parent,
                    "metadata": {},
                    "reply_count": 0,
                },
            )
            if not created and body is not None and msg.body != body and msg.deleted_at is None:
                msg.body = body
                msg.body_text = _body_text_from_body(body)
                msg.save(update_fields=["body", "body_text"])
                thread_summary_cleanup.forget_message_thread(msg)
            if is_deleted:
                message_tombstone.tombstone_message(msg)
            return msg
    except Exception:  # noqa: BLE001
        logger.exception(
            "[unified_writer] write_thread_message failed for "
            "(chat_type=%s, chat_id=%s, thread_id=%s, message_id=%s)",
            chat_type,
            chat_id,
            thread_id,
            message_id,
        )
        return None


def delete_message(
    *, chat_type: int, chat_id: int, message_id: int, is_thread: bool = False
) -> Optional[Message]:
    """Soft-delete the matching unified Message (sets `deleted_at`).
    Idempotent: a second call is a no-op."""
    if not _is_enabled():
        return None
    try:
        msg = _resolve_message(chat_type, chat_id, message_id)
        if msg is None:
            return None
        message_tombstone.tombstone_message(msg)
        return msg
    except Exception:  # noqa: BLE001
        logger.exception(
            "[unified_writer] delete_message failed for "
            "(chat_type=%s, chat_id=%s, message_id=%s, is_thread=%s)",
            chat_type,
            chat_id,
            message_id,
            is_thread,
        )
        return None


def write_pin(*, chat_type: int, chat_id: int, user_id: str) -> Optional[Pin]:
    """Mirror a pin add — legacy stores this in `UserChatMaster.pinned_chats`
    JSON; unified uses a `Pin` row."""
    if not _is_enabled():
        return None
    try:
        channel = _resolve_channel(chat_type, chat_id)
        if channel is None:
            return None
        pin, _ = Pin.objects.get_or_create(user_id=user_id, channel=channel)
        return pin
    except Exception:  # noqa: BLE001
        logger.exception("[unified_writer] write_pin failed")
        return None


def delete_pin(*, chat_type: int, chat_id: int, user_id: str) -> bool:
    if not _is_enabled():
        return False
    try:
        channel = _resolve_channel(chat_type, chat_id)
        if channel is None:
            return False
        deleted, _ = Pin.objects.filter(user_id=user_id, channel=channel).delete()
        return bool(deleted)
    except Exception:  # noqa: BLE001
        logger.exception("[unified_writer] delete_pin failed")
        return False


def write_flag(*, chat_type: int, chat_id: int, message_id: int, user_id: str) -> Optional[Flag]:
    """Mirror a flag add — legacy stores in `UserChatMaster.flagged_messages`
    JSON; unified uses a `Flag` row."""
    if not _is_enabled():
        return None
    try:
        msg = _resolve_message(chat_type, chat_id, message_id)
        if msg is None:
            return None
        flag, created = Flag.objects.get_or_create(user_id=user_id, message=msg)
        # Parity with FlagView.post: re-flagging a completed message must
        # reactivate it (clear completed_at) rather than leave it done.
        if not created and flag.completed_at is not None:
            flag.completed_at = None
            flag.save(update_fields=["completed_at"])
        return flag
    except Exception:  # noqa: BLE001
        logger.exception("[unified_writer] write_flag failed")
        return None


def delete_flag(*, chat_type: int, chat_id: int, message_id: int, user_id: str) -> bool:
    if not _is_enabled():
        return False
    try:
        msg = _resolve_message(chat_type, chat_id, message_id)
        if msg is None:
            return False
        deleted, _ = Flag.objects.filter(user_id=user_id, message=msg).delete()
        return bool(deleted)
    except Exception:  # noqa: BLE001
        logger.exception("[unified_writer] delete_flag failed")
        return False


def _header_content(task_id, sender_id, title) -> tuple[list, str, dict]:
    """The body, preview text and metadata of a lazily made task header: the task's full card,
    or, should building it fail, a bare title (a comment must never be lost to its card)."""
    try:
        parts = task_card_post.header_parts(task_id, language_of=sender_id)
        if parts is not None:
            return parts
    except Exception:  # noqa: BLE001
        logger.warning("[unified_writer] card build failed task=%s", task_id, exc_info=True)
    title = title or "Task"
    heading = {
        "type": "heading",
        "props": dict(task_card.CARD_HEADING_PROPS),
        "content": [{"type": "text", "text": f"🧾 {title}", "styles": {}}],
        "children": [],
    }
    return [heading], title, {"taskId": task_id}


def write_task_comment_as_thread_reply(
    *,
    task_id: int,
    comment_id: int,
    sender_id: Optional[str],
    body: list,
    project_id: Optional[int] = None,
    bypass_flag: bool = False,
    announce_header: bool = False,
) -> Optional[Message]:
    """Mirror a legacy `TaskComments` row as a v3 thread-reply `Message`
    under the PM task header.

    Task comments historically lived in their own `TaskComments` table,
    separate from `PMMessages`/`PMThreadMessages`. The v3 unified model
    treats thread replies as `Message` rows with `is_thread_reply=True`
    + `parent_id = <task header UUID>`. Bridging the two lets PM task
    threads render comments alongside any other thread replies, without
    a parallel comments-only endpoint.

    Idempotency: `Message.id` is `uuid5(NS, "{task_id}-{comment_id}")`.
    Re-running the backfill (or the dual-write retrying on a transient
    network failure) collides on the existing PK and is a no-op via
    `get_or_create`.

    `project_id`: when known at the call site, passing it skips a
    `TaskMaster` lookup. `repair_task_comment_mirrors` and the demo
    seeder always know the project; the live dual-write path can pass it
    from the request or omit it.

    `bypass_flag`: the dual-write entry points all gate on the
    `UNIFIED_MESSAGING_DUAL_WRITE` setting, but the comment fan-out, the
    demo seeder and `repair_task_comment_mirrors` run regardless of that
    runtime flag. They pass `True`.

    `announce_header`: tell open chats about a card this call had to make. Only a comment
    someone just wrote asks for it: an import or a seeder would push one per old task.

    Returns the created/found Message on success, None on any failure.
    Never raises — the caller's legacy `TaskComments` save is the
    source of truth and must not be affected by mirror failures.
    """
    if not bypass_flag and not _is_enabled():
        return None
    try:
        # Resolve the PM channel for this task. Each project has one
        # PM channel (kind=3, legacy_chat_id=project_id). When the
        # caller didn't pass project_id, walk through the TaskMaster
        # FK.
        if project_id is None:
            from origin.models.task.task_models import TaskMaster

            try:
                task = TaskMaster.objects.only("project_id").get(task_id=task_id)
            except TaskMaster.DoesNotExist:
                logger.warning(
                    "[unified_writer] task_comment mirror: task_id=%s not found",
                    task_id,
                )
                return None
            project_id = task.project_id
            if project_id is None:
                logger.warning(
                    "[unified_writer] task_comment mirror: task_id=%s has no project",
                    task_id,
                )
                return None

        channel = Channel.objects.filter(
            kind=ChannelKind.PM, legacy_chat_id=int(project_id)
        ).first()
        if channel is None:
            logger.warning(
                "[unified_writer] task_comment mirror: no PM Channel for project_id=%s",
                project_id,
            )
            return None

        # Find the task-card header Message (the v3 thread root). Each
        # PM task has exactly one top-level Message with `task_id` set.
        parent = (
            Message.objects.filter(
                channel=channel,
                task_id=task_id,
                is_thread_reply=False,
            )
            .only("id", "reply_count", "body")
            .order_by("seq")
            .first()
        )
        if parent is None:
            # Lazily create the PM task-card header. Headerless tasks —
            # sub-tasks, tasks predating the PM-card feature, or ones whose
            # create-time card send failed (it's best-effort + swallowed on
            # the client) — otherwise have nothing for the comment to thread
            # under, so this returns None and the ENTIRE comment fan-out
            # (assignee + collaborators + prior commenters) silently notifies
            # no one. That's why task comments failed to notify while
            # milestones (which always post a header) worked.
            #
            # The header is the task's full card (MIP-2483), built where every card is
            # (`task_card_post.header_parts`); posted silently, with no `MessageMention` rows
            # and no activity, so a comment on an old task notifies who it always did and puts
            # nobody's old card in their list of mentions. Sender is the task's reporter (the card's author),
            # never the commenter.
            from origin.models.task.task_models import TaskMaster

            task_row = (
                TaskMaster.objects.only("reporter_id", "assignee_id", "title")
                .filter(task_id=task_id)
                .first()
            )
            if task_row is None:
                logger.warning(
                    "[unified_writer] task_comment mirror: task_id=%s not found "
                    "for lazy header creation",
                    task_id,
                )
                return None
            header_sender = task_row.reporter_id or task_row.assignee_id or sender_id
            # Not `body`: that is the COMMENT's, which the reply below carries.
            card_body, card_text, card_metadata = _header_content(
                task_id, header_sender, task_row.title
            )
            with transaction.atomic():
                # Lock the channel + re-check under it: the task's own card may
                # land between the lookup above and here, so we must not insert
                # a duplicate header.
                Channel.objects.select_for_update().filter(pk=channel.pk).first()
                parent = (
                    Message.objects.filter(channel=channel, task_id=task_id, is_thread_reply=False)
                    .only("id", "reply_count", "body")
                    .order_by("seq")
                    .first()
                )
                if parent is None:
                    header_seq = (
                        Message.objects.filter(channel=channel)
                        .order_by("-seq")
                        .values_list("seq", flat=True)
                        .first()
                        or 0
                    ) + 1
                    parent = Message.objects.create(
                        channel=channel,
                        seq=header_seq,
                        sender_id=header_sender,
                        body=card_body,
                        body_text=card_text,
                        task_id=task_id,
                        is_thread_reply=False,
                        metadata=card_metadata,
                        reply_count=0,
                    )
                    # Chats that are open see the new card now, not at their next sync.
                    if announce_header and not activity_suppressed():
                        realtime_push.push_message(parent, realtime_push.CREATED)
                    logger.info(
                        "[unified_writer] lazily created PM task-card header for "
                        "headerless task_id=%s in channel %s",
                        task_id,
                        channel.id,
                    )
        elif task_card_post.is_title_only(parent.body):
            # A bare header from before MIP-2483: make it the full card.
            task_card_post.schedule_refresh(task_id=task_id)

        deterministic_id = task_comment_message_uuid(task_id, comment_id)

        # `seq` is per-channel; for thread replies we still need a unique
        # seq. Allocate the next `max(seq)+1` under a transaction that
        # locks the CHANNEL row.
        with transaction.atomic():
            existing = Message.objects.filter(id=deterministic_id).first()
            if existing is not None:
                return existing
            # Lock the always-present Channel row to serialize seq
            # allocation. The prior `select_for_update().filter(channel=...)`
            # locked the matching MESSAGE rows — of which there are NONE on
            # a channel whose first message is this comment — so two
            # concurrent first-replies both read seq=0, both inserted seq=1,
            # and collided on the `(channel, seq)` unique constraint; the
            # broad `except` below then silently dropped the mirror. Locking
            # the channel row (as `message_views._allocate_seq_and_create_
            # message` does) serializes the allocation even when empty.
            Channel.objects.select_for_update().filter(pk=channel.pk).first()
            next_seq = (
                Message.objects.filter(channel=channel)
                .order_by("-seq")
                .values_list("seq", flat=True)
                .first()
                or 0
            ) + 1
            msg = Message.objects.create(
                id=deterministic_id,
                channel=channel,
                seq=next_seq,
                sender_id=sender_id,
                body=body or [],
                body_text=_body_text_from_body(body),
                task_id=task_id,
                parent=parent,
                thread_root=parent,
                is_thread_reply=True,
                # The connected app that wrote this comment, if one did (MIP-1570): its kind and
                # family only, the form the whole channel may read.
                metadata=message_via.stamped({"taskCommentId": int(comment_id)}),
                reply_count=0,
            )
            # Atomically increment the parent's reply_count. Using
            # F() avoids the read-modify-write race against concurrent
            # comment creates.
            #
            # Also bump ts_updated_at: `.update()` skips the `auto_now`,
            # and the FE syncs top-level messages incrementally via
            # `?since=ts_updated_at`. Without this the task-header row is
            # never re-served after a comment, so a cached bubble keeps its
            # stale `taskCommentCount` chip until a full resync. The
            # direct-reply path (`message_views`) already bumps it via
            # `.save(update_fields=[..., "ts_updated_at"])`; match that.
            from django.utils import timezone

            Message.objects.filter(id=parent.id).update(
                reply_count=F("reply_count") + 1,
                ts_updated_at=timezone.now(),
            )
        return msg
    except Exception:  # noqa: BLE001
        logger.exception(
            "[unified_writer] write_task_comment_as_thread_reply failed: task=%s comment=%s",
            task_id,
            comment_id,
        )
        return None


def update_task_comment_thread_reply(
    *,
    task_id: int,
    comment_id: int,
    body: list,
) -> Optional[Message]:
    """Carry an edit of a `TaskComments` row over to its v3 mirror.

    The PM task thread renders a task comment only through the mirror, so an
    edit that stops at the comment row leaves the old text in the thread for
    good (MIP-946). Like the thread's own edit (`MessageDetailView.patch`), this
    sets the body, its plain text and `edited_at`; `ts_updated_at` moves with the
    save, which is what a client syncing the thread by `?since=` sees.

    Only a live mirror is touched: a comment with none (older than the thread,
    or never mirrored) has nothing to update, and a tombstoned one stays
    deleted. Returns the mirror when this call updated it, else None. Never
    raises: the caller's `TaskComments` row is the source of truth and must not
    be affected by a mirror failure.
    """
    from django.utils import timezone

    try:
        msg = Message.objects.filter(
            id=task_comment_message_uuid(task_id, comment_id), deleted_at__isnull=True
        ).first()
        if msg is None:
            return None
        old_body, old_body_text = msg.body, msg.body_text
        msg.body = body or []
        msg.body_text = _body_text_from_body(body)
        msg.edited_at = timezone.now()
        update_fields = ["body", "body_text", "edited_at", "ts_updated_at"]
        # The stamp says who wrote the text that is there now (MIP-1570): this edit's writer
        # replaces the old one's, and a person's edit leaves none.
        metadata = message_via.stamped(msg.metadata)
        if metadata != (msg.metadata or {}):
            msg.metadata = metadata
            update_fields.append("metadata")
        with transaction.atomic():
            msg.save(update_fields=update_fields)
            thread_summary_cleanup.forget_edited_message(
                msg, old_body=old_body, old_body_text=old_body_text
            )
        return msg
    except Exception:  # noqa: BLE001
        logger.exception(
            "[unified_writer] update_task_comment_thread_reply failed: task=%s comment=%s",
            task_id,
            comment_id,
        )
        return None


def soft_delete_task_comment_as_thread_reply(
    *,
    task_id: int,
    comment_id: int,
) -> bool:
    """Tombstone the v3 mirror of a soft-deleted `TaskComments` row.

    The inverse of `write_task_comment_as_thread_reply`. Without it a
    deleted comment stays visible in the PM task thread, which renders
    comments exclusively through the mirror.

    Semantics match the v3 `MessageDetailView.delete`: set `deleted_at` and
    clear the text (`message_tombstone`, MIP-1784), decrement the
    parent's `reply_count` (floored at 0 so a double-delete can't
    underflow), and bump the parent's `ts_updated_at` so the FE's
    `?since=` message delta re-serves the task-header row and its
    `taskCommentCount` chip re-annotates.

    Idempotent: an already-tombstoned (or never-mirrored) comment is a
    no-op returning False, so the parent's `reply_count` is only ever
    decremented once per comment.

    Returns True when this call tombstoned the mirror. Never raises —
    the caller's legacy `TaskComments` row is the source of truth and
    must not be affected by mirror failures.
    """
    from django.utils import timezone

    try:
        deterministic_id = task_comment_message_uuid(task_id, comment_id)
        with transaction.atomic():
            msg = Message.objects.filter(id=deterministic_id).first()
            if msg is None:
                return False
            if msg.deleted_at is not None:
                # Already a tombstone: nothing to count down. One an older delete left with its
                # text is cleaned (MIP-1784); a clean one is not written.
                message_tombstone.tombstone_message(msg)
                return False
            # Flag and text together (MIP-1784), the same as a chat delete.
            message_tombstone.tombstone_message(msg)
            if msg.parent_id is not None:
                Message.objects.filter(pk=msg.parent_id, reply_count__gt=0).update(
                    reply_count=F("reply_count") - 1,
                    ts_updated_at=timezone.now(),
                )
        return True
    except Exception:  # noqa: BLE001
        logger.exception(
            "[unified_writer] soft_delete_task_comment_as_thread_reply failed: task=%s comment=%s",
            task_id,
            comment_id,
        )
        return False
