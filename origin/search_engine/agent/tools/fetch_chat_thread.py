"""`fetch_chat_thread` tool — load a chat conversation or thread.

Dispatches by `chat_type` to the right ORM tables and returns
messages in chronological order with sender + text + timestamp.

Two modes:
  * `thread_id` omitted: returns a page of main-channel messages, the newest
    `limit` (50 unless asked, never more than 100) oldest-first. `before`
    pages back through older ones: a page that is not the last reports
    `has_more` and a `next_cursor` to pass back as `before`.
  * `thread_id` provided: returns the thread's parent message (if it
    still exists) + up to 100 replies. A thread longer than that reports
    `has_more`; there is no cursor for replies.

Text extraction uses the same `extract_text(...)` helper the chunker
uses on JSONField bodies — same BlockNote → plaintext conversion.

ACL: derived via `agent.acl.chat_acl_user_ids`. Reuses the same logic
the indexer used to stamp each chunk's `acl_user_ids`. On top of that the
chat has to belong to the caller's chat list for THIS team
(`channel_scope.team_scope_q`), so a key for one team cannot read the same
person's chats in another. A chat the caller cannot read is answered exactly
like one that does not exist.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.exceptions import ValidationError

from origin.models.chat.unified_models import Channel, Message
from origin.search_engine.agent.acl import chat_acl_user_ids
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolForbidden,
    wrap_workspace_content,
)
from origin.search_engine.agent.tools.note_targets import parse_chat_ref
from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL
from origin.search_engine.text_extraction import extract_text
from origin.services.channel_scope import team_scope_q

# The page of main-channel messages a call returns unless asked for fewer or
# more, and the most it will ever return. A thread keeps its own cap.
_MAIN_CHANNEL_CAP = 50
_MAIN_CHANNEL_MAX = 100
_THREAD_CAP = 100
# A chat URL is a few hundred characters at most; anything longer is not one.
_MAX_REF_CHARS = 2048

# String label → int code. Inverse of CHAT_TYPE_LABEL.
_LABEL_TO_CODE: dict[str, int] = {v: k for k, v in CHAT_TYPE_LABEL.items()}


def _unreadable(chat_id: str) -> str:
    """The one answer for a chat that is missing, in another team, or not the
    caller's. Same words for all three, so the reply says nothing about which."""
    return f"Chat {chat_id} not found, or you are not a member of it."


def _channel_by_uuid(chat_id: str) -> Channel | None:
    """Resolve a live `Channel` by its UUID. Returns None for an unknown
    or malformed id rather than raising."""
    try:
        return Channel.objects.filter(id=chat_id, is_deleted=False).first()
    except (ValidationError, ValueError, TypeError):
        return None


def _message_by_uuid(channel: Channel, message_id: str) -> Message | None:
    """Resolve a top-level (non-reply) `Message` by its UUID within a
    channel. Returns None for an unknown or malformed id."""
    try:
        return Message.objects.filter(channel=channel, id=message_id, is_thread_reply=False).first()
    except (ValidationError, ValueError, TypeError):
        return None


def _as_uuid(raw: Any, name: str) -> str:
    try:
        return str(uuid.UUID(str(raw).strip()))
    except (ValueError, AttributeError, TypeError):
        raise ToolError(f"`{name}` must be a message id (a UUID), got {str(raw)[:80]!r}.")


def _sender_name(message: Message) -> str | None:
    return (message.sender.username or None) if message.sender_id and message.sender else None


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # ---- read and check every argument before touching a row ----
    raw_chat = args.get("chat_id")
    if raw_chat is None or not str(raw_chat).strip():
        raise ToolError("chat_id must be a non-empty UUID string (the Channel id) or a chat URL.")
    if len(str(raw_chat)) > _MAX_REF_CHARS:
        raise ToolError("chat_id is too long: pass the chat's id (a UUID) or its Genos URL.")
    chat_id, thread_id = parse_chat_ref(raw_chat, args.get("thread_id"))
    chat_id = str(uuid.UUID(chat_id))
    thread_id = str(uuid.UUID(thread_id)) if thread_id else None

    chat_type_label = (args.get("chat_type") or "").lower().strip()
    if chat_type_label and chat_type_label not in _LABEL_TO_CODE:
        raise ToolError(
            f"Unknown chat_type {chat_type_label!r}; expected one of {sorted(_LABEL_TO_CODE)}."
        )

    raw_before = args.get("before")
    before = _as_uuid(raw_before, "before") if raw_before not in (None, "") else None
    if before and thread_id:
        raise ToolError(
            "`before` pages the main channel; it can't be combined with `thread_id`. "
            "Send one or the other."
        )

    raw_limit = args.get("limit")
    if raw_limit in (None, ""):
        limit = _MAIN_CHANNEL_CAP
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            raise ToolError(f"`limit` must be an integer (got {raw_limit!r}).")
        limit = max(1, min(limit, _MAIN_CHANNEL_MAX))

    # ---- access: member of the chat, and the chat is in this team's list ----
    channel = _channel_by_uuid(chat_id)
    if channel is None:
        raise ToolError(_unreadable(chat_id))
    if chat_type_label and channel.kind != _LABEL_TO_CODE[chat_type_label]:
        # A wrong chat_type reads as "no such chat", as it always has. Not a
        # refusal: the caller named the wrong kind, they did not probe.
        raise ToolError(_unreadable(chat_id))
    chat_type_label = CHAT_TYPE_LABEL[channel.kind]
    # The team's own channel needs no lookup; a chat another team shared with
    # this one (or a shared project's PM chat) is found by `team_scope_q`.
    in_team_list = (
        str(channel.team_id) == str(ctx.team_id)
        or Channel.objects.filter(team_scope_q(ctx.team_id, ctx.user_id), id=channel.id).exists()
    )
    if ctx.user_id not in chat_acl_user_ids(channel.kind, chat_id) or not in_team_list:
        # Found, and not the caller's: a refusal the burst alert counts, in
        # the same words as a chat that is not there.
        raise ToolForbidden(_unreadable(chat_id))

    messages: list[dict[str, Any]] = []
    has_more = False
    next_cursor: str | None = None

    if thread_id is None:
        # Main-channel mode: the newest `limit` top-level messages, oldest-first.
        # `id` is the stable client-facing UUID; `body` is the JSONField.
        qs = Message.objects.filter(
            channel=channel, is_thread_reply=False, deleted_at__isnull=True
        ).select_related("sender")
        if before:
            anchor_seq = (
                Message.objects.filter(channel=channel, id=before, is_thread_reply=False)
                .values_list("seq", flat=True)
                .first()
            )
            if anchor_seq is None:
                raise ToolError(
                    "`before` is not a message in this chat. Pass the `next_cursor` of the "
                    "previous page, or a `message_id` from it."
                )
            qs = qs.filter(seq__lt=anchor_seq)
        # One past the page says whether anything older is left. The cursor is
        # the oldest ROW of the page, not the oldest message shown: a row with
        # no text is skipped below, and a cursor taken from what was shown could
        # step back past it, or end the paging early over a run of them.
        rows = list(qs.order_by("-seq")[: limit + 1])
        has_more = len(rows) > limit
        rows = rows[:limit]
        if has_more:
            next_cursor = str(rows[-1].id)
        for m in reversed(rows):
            text = extract_text(m.body)
            if not text:
                continue
            row = {
                "message_id": str(m.id),
                "sender_id": str(m.sender_id or ""),
                "sender_username": _sender_name(m),
                "text": wrap_workspace_content(text),
                "ts": m.ts_sent_at.isoformat() if m.ts_sent_at else None,
            }
            if m.reply_count:
                # Its replies are a thread: pass `message_id` as `thread_id`.
                row["reply_count"] = m.reply_count
            messages.append(row)
    else:
        # Thread mode: the root message (anchor, whose `id` == thread_id) +
        # its replies (rooted by `thread_root_id`).
        root = _message_by_uuid(channel, thread_id)
        if root is not None and root.deleted_at is None:
            text = extract_text(root.body)
            if text:
                messages.append(
                    {
                        "message_id": str(root.id),
                        "sender_id": str(root.sender_id or ""),
                        "sender_username": _sender_name(root),
                        "text": wrap_workspace_content(text),
                        "ts": root.ts_sent_at.isoformat() if root.ts_sent_at else None,
                        "is_thread_anchor": True,
                    }
                )
        if root is not None:
            replies = list(
                Message.objects.filter(
                    channel=channel,
                    thread_root_id=root.id,
                    is_thread_reply=True,
                    deleted_at__isnull=True,
                )
                .select_related("sender")
                .order_by("seq")[: _THREAD_CAP + 1]
            )
            has_more = len(replies) > _THREAD_CAP
            for r in replies[:_THREAD_CAP]:
                text = extract_text(r.body)
                if not text:
                    continue
                messages.append(
                    {
                        "thread_message_id": str(r.id),
                        "sender_id": str(r.sender_id or ""),
                        "sender_username": _sender_name(r),
                        "text": wrap_workspace_content(text),
                        "ts": r.ts_sent_at.isoformat() if r.ts_sent_at else None,
                    }
                )

    result: dict[str, Any] = {
        "chat_type": chat_type_label,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "messages": messages,
        "has_more": has_more,
    }
    if next_cursor:
        result["next_cursor"] = next_cursor

    if not messages:
        # Authorized but nothing to show. Return empty result (not an
        # error) so the model can tell the user "the thread is empty"
        # rather than thinking the call failed.
        result["__summary__"] = (
            f"Chat {chat_type_label}:{chat_id}"
            + (f" thread {thread_id}" if thread_id else "")
            + " has no messages."
            + (" Older messages exist: pass next_cursor as `before`." if has_more else "")
        )
        return result

    summary_scope = (
        f"thread {chat_type_label}:{chat_id}:thread:{thread_id}"
        if thread_id is not None
        else f"channel {chat_type_label}:{chat_id}"
    )
    summary = f"Loaded {len(messages)} messages from {summary_scope}"
    if has_more:
        summary += (
            f" (only the first {_THREAD_CAP} replies)"
            if thread_id is not None
            else " (older messages exist: pass next_cursor as `before`)"
        )
    result["__summary__"] = summary
    return result


FETCH_CHAT_THREAD = Tool(
    name="fetch_chat_thread",
    description=(
        "Load a chat conversation as plain text — either a page of its "
        "most recent main-channel messages (omit thread_id) or every "
        "message in a specific thread (pass thread_id). Use after "
        "`search_knowledge_base`, `list_my_chats` or `list_my_mentions` "
        "when you need to read who said what in context. Each message "
        "includes sender_id, sender_username and timestamp; a message that "
        "started a thread has reply_count (about how many replies), and its "
        "message_id is the thread_id to read the replies with. The main channel returns the "
        "newest 50 messages (up to `limit`, at most 100), oldest first: "
        "when has_more is true there are older ones, and next_cursor, "
        "passed back as `before`, returns the page before this one. A "
        "thread returns its first message and up to 100 replies. ACL is "
        "enforced — only chats the user is a member of."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "chat_type": {
                "type": "STRING",
                "enum": ["dm", "gm", "mdm", "pm"],
                "description": (
                    "Optional; the chat's kind, if you know it: dm (direct), gm "
                    "(named group), mdm (multi-DM), pm (project chat — chat_id is "
                    "the PM channel's UUID). It is worked out from chat_id when "
                    "omitted, and a chat of another kind is treated as not found."
                ),
            },
            "chat_id": {
                "type": "STRING",
                "description": (
                    "Chat UUID (the Channel id), or the chat's Genos URL. A thread "
                    "URL also names the thread."
                ),
            },
            "thread_id": {
                "type": "STRING",
                "description": (
                    "Optional. UUID of the thread-root message. If "
                    "provided, returns the thread's messages instead of "
                    "the main channel."
                ),
            },
            "before": {
                "type": "STRING",
                "description": (
                    "Optional. Main channel only: the `next_cursor` of the previous "
                    "page, or any `message_id`, to get the messages older than it. "
                    "Not with thread_id."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": (
                    f"Optional. Main channel only: how many messages (1–{_MAIN_CHANNEL_MAX}). "
                    f"Default {_MAIN_CHANNEL_CAP}."
                ),
            },
        },
        "required": ["chat_id"],
    },
    run=_run,
)
