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
from typing import Any, Optional

from django.conf import settings
from django.db import transaction

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    Flag,
    Message,
    MessageMention,
    MessageReaction,
    Pin,
    ReadCursor,
)

logger = logging.getLogger(__name__)


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
                if body is not None and msg.body != body:
                    msg.body = body
                    msg.body_text = _body_text_from_body(body)
                    update_fields.extend(["body", "body_text"])
                # Soft-delete: legacy uses `is_deleted=True`; unified
                # uses `deleted_at` (nullable datetime).
                if is_deleted and msg.deleted_at is None:
                    from django.utils import timezone

                    msg.deleted_at = timezone.now()
                    update_fields.append("deleted_at")
                if update_fields:
                    msg.save(update_fields=update_fields)
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
            if not created and body is not None and msg.body != body:
                msg.body = body
                msg.body_text = _body_text_from_body(body)
                msg.save(update_fields=["body", "body_text"])
            if is_deleted and msg.deleted_at is None:
                from django.utils import timezone

                msg.deleted_at = timezone.now()
                msg.save(update_fields=["deleted_at"])
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
        if msg.deleted_at is None:
            from django.utils import timezone

            msg.deleted_at = timezone.now()
            msg.save(update_fields=["deleted_at"])
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


def write_reaction(
    *,
    chat_type: int,
    chat_id: int,
    message_id: int,
    user_id: str,
    emoji: str,
    is_thread: bool = False,
    thread_id: Optional[int] = None,
) -> Optional[MessageReaction]:
    """Mirror a legacy `ReactionFact` insert into `MessageReaction`.

    The legacy schema keys reactions by `(chat_type, chat_id, message_id,
    is_thread, thread_id)` because messages and thread messages share a
    flat namespace. The unified schema collapses both into `Message`,
    so we resolve the underlying Message row first (top-level messages
    use `message_id`; thread replies use `(chat_id, thread_id, message_id)`
    which is the per-thread reply id with `seq=message_id` per the
    write_thread_message contract).
    """
    if not _is_enabled():
        return None
    try:
        msg = _resolve_message(chat_type, chat_id, message_id)
        if msg is None:
            return None
        reaction, _ = MessageReaction.objects.get_or_create(
            message=msg,
            user_id=user_id,
            emoji=emoji,
        )
        return reaction
    except Exception:  # noqa: BLE001
        logger.exception("[unified_writer] write_reaction failed")
        return None


def delete_reaction(
    *,
    chat_type: int,
    chat_id: int,
    message_id: int,
    user_id: str,
    emoji: str,
) -> bool:
    """Remove the matching `MessageReaction`. Idempotent."""
    if not _is_enabled():
        return False
    try:
        msg = _resolve_message(chat_type, chat_id, message_id)
        if msg is None:
            return False
        deleted, _ = MessageReaction.objects.filter(
            message=msg, user_id=user_id, emoji=emoji
        ).delete()
        return bool(deleted)
    except Exception:  # noqa: BLE001
        logger.exception("[unified_writer] delete_reaction failed")
        return False


def write_mention(
    *,
    chat_type: int,
    chat_id: int,
    message_id: int,
    mentioned_user_id: str,
    via_group_id: Optional[str] = None,
) -> Optional[MessageMention]:
    """Mirror a legacy `MentionFact` insert into `MessageMention`."""
    if not _is_enabled():
        return None
    try:
        msg = _resolve_message(chat_type, chat_id, message_id)
        if msg is None:
            return None
        mention, _ = MessageMention.objects.get_or_create(
            message=msg,
            mentioned_user_id=mentioned_user_id,
            defaults={"via_group_id": via_group_id},
        )
        return mention
    except Exception:  # noqa: BLE001
        logger.exception("[unified_writer] write_mention failed")
        return None


def delete_mention(
    *,
    chat_type: int,
    chat_id: int,
    message_id: int,
    mentioned_user_id: str,
) -> bool:
    """Remove a matching `MessageMention`. Idempotent."""
    if not _is_enabled():
        return False
    try:
        msg = _resolve_message(chat_type, chat_id, message_id)
        if msg is None:
            return False
        deleted, _ = MessageMention.objects.filter(
            message=msg, mentioned_user_id=mentioned_user_id
        ).delete()
        return bool(deleted)
    except Exception:  # noqa: BLE001
        logger.exception("[unified_writer] delete_mention failed")
        return False


def write_read_cursor(
    *,
    chat_type: int,
    chat_id: int,
    user_id: str,
    last_read_message_id: int,
    is_thread: bool = False,
    thread_id: Optional[int] = None,
) -> Optional[ReadCursor]:
    """Mirror a legacy `ReadStatus` upsert into `ReadCursor`.

    Forward-only at the legacy view layer (it explicitly rejects
    backwards motion). We mirror that by checking the existing cursor
    before overwriting — keeping the unified store consistent with the
    legacy store's monotonic guarantee.
    """
    if not _is_enabled():
        return None
    try:
        channel = _resolve_channel(chat_type, chat_id)
        if channel is None:
            return None
        last_msg = _resolve_message(chat_type, chat_id, last_read_message_id)
        if last_msg is None:
            return None
        thread_root = None
        if is_thread and thread_id is not None:
            thread_root = _resolve_message(chat_type, chat_id, thread_id)
            if thread_root is None:
                return None
        cursor, created = ReadCursor.objects.get_or_create(
            user_id=user_id,
            channel=channel,
            thread_root=thread_root,
            defaults={"last_read_message": last_msg},
        )
        # Forward-only: only update if the incoming `seq` is higher.
        if (
            not created
            and cursor.last_read_message
            and last_msg.seq > cursor.last_read_message.seq
        ):
            cursor.last_read_message = last_msg
            cursor.save(update_fields=["last_read_message"])
        return cursor
    except Exception:  # noqa: BLE001
        logger.exception("[unified_writer] write_read_cursor failed")
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
        flag, _ = Flag.objects.get_or_create(user_id=user_id, message=msg)
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
