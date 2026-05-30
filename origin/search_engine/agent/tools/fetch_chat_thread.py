"""`fetch_chat_thread` tool — load a chat conversation or thread.

Dispatches by `chat_type` to the right ORM tables and returns
messages in chronological order with sender + text + timestamp.

Two modes:
  * `thread_id` omitted: returns the last N main-channel messages.
  * `thread_id` provided: returns the thread's parent message (if it
    still exists) + all replies in that thread.

Text extraction uses the same `extract_text(...)` helper the chunker
uses on JSONField bodies — same BlockNote → plaintext conversion.

ACL: derived via `agent.acl.chat_acl_user_ids`. Reuses the same logic
the indexer used to stamp each chunk's `acl_user_ids`.
"""

from __future__ import annotations

from typing import Any

from origin.models.chat.unified_models import Message
from origin.search_engine.agent.acl import chat_acl_user_ids
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    wrap_workspace_content,
)
from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL
from origin.search_engine.text_extraction import extract_text
from origin.services.legacy_chat_bridge import resolve_channel

_MAIN_CHANNEL_CAP = 50
_THREAD_CAP = 100

# String label → int code. Inverse of CHAT_TYPE_LABEL.
_LABEL_TO_CODE: dict[str, int] = {v: k for k, v in CHAT_TYPE_LABEL.items()}


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    chat_type_label = (args.get("chat_type") or "").lower().strip()
    chat_type_code = _LABEL_TO_CODE.get(chat_type_label)
    if chat_type_code is None:
        raise ToolError(
            f"Unknown chat_type {chat_type_label!r}; expected one of " f"{sorted(_LABEL_TO_CODE)}."
        )

    raw_chat_id = args.get("chat_id")
    try:
        chat_id = int(raw_chat_id)
    except (TypeError, ValueError):
        raise ToolError(f"chat_id must be an integer (got {raw_chat_id!r}).")

    raw_thread = args.get("thread_id")
    thread_id: int | None = None
    if raw_thread is not None and raw_thread != "":
        try:
            thread_id = int(raw_thread)
        except (TypeError, ValueError):
            raise ToolError(f"thread_id must be an integer (got {raw_thread!r}).")

    # ACL gate before any data fetch.
    allowed = chat_acl_user_ids(chat_type_code, chat_id)
    if not allowed:
        raise ToolError(f"Chat {chat_type_label}:{chat_id} not found or has no members.")
    if ctx.user_id not in allowed:
        raise ToolError(f"Not authorized to read chat {chat_type_label}:{chat_id}.")

    # Resolve the legacy chat ref to its v3 channel. PM may have allowed
    # members (via ProjectMembers) yet no bridged channel — treat as empty.
    channel = resolve_channel(chat_type_code, chat_id)

    messages: list[dict[str, Any]] = []

    if channel is not None and thread_id is None:
        # Main-channel mode: most-recent N top-level messages, oldest-first.
        # v3 `Message.seq` mirrors the legacy `message_id`; `body` is the
        # same JSONField the legacy `*_body` fields held.
        qs = Message.objects.filter(
            channel=channel, is_thread_reply=False, deleted_at__isnull=True
        ).order_by("-seq")[:_MAIN_CHANNEL_CAP]
        for m in reversed(list(qs)):
            text = extract_text(m.body)
            if not text:
                continue
            messages.append(
                {
                    "message_id": m.seq,
                    "sender_id": str(m.sender_id or ""),
                    "text": wrap_workspace_content(text),
                    "ts": m.ts_sent_at.isoformat() if m.ts_sent_at else None,
                }
            )
    elif channel is not None:
        # Thread mode: the root message (anchor, at seq == thread_id) +
        # its replies (rooted by `thread_root_id`).
        root = Message.objects.filter(
            channel=channel, seq=thread_id, is_thread_reply=False
        ).first()
        if root is not None and root.deleted_at is None:
            text = extract_text(root.body)
            if text:
                messages.append(
                    {
                        "message_id": root.seq,
                        "sender_id": str(root.sender_id or ""),
                        "text": wrap_workspace_content(text),
                        "ts": root.ts_sent_at.isoformat() if root.ts_sent_at else None,
                        "is_thread_anchor": True,
                    }
                )
        if root is not None:
            replies_qs = Message.objects.filter(
                channel=channel,
                thread_root_id=root.id,
                is_thread_reply=True,
                deleted_at__isnull=True,
            ).order_by("seq")[:_THREAD_CAP]
            for r in replies_qs:
                text = extract_text(r.body)
                if not text:
                    continue
                messages.append(
                    {
                        "thread_message_id": r.seq,
                        "sender_id": str(r.sender_id or ""),
                        "text": wrap_workspace_content(text),
                        "ts": r.ts_sent_at.isoformat() if r.ts_sent_at else None,
                    }
                )

    if not messages:
        # Authorized but nothing to show. Return empty result (not an
        # error) so the model can tell the user "the thread is empty"
        # rather than thinking the call failed.
        return {
            "chat_type": chat_type_label,
            "chat_id": chat_id,
            "thread_id": thread_id,
            "messages": [],
            "__summary__": (
                f"Chat {chat_type_label}:{chat_id}"
                + (f" thread {thread_id}" if thread_id else "")
                + " has no messages."
            ),
        }

    summary_scope = (
        f"thread {chat_type_label}:{chat_id}:thread:{thread_id}"
        if thread_id is not None
        else f"channel {chat_type_label}:{chat_id}"
    )
    return {
        "chat_type": chat_type_label,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "messages": messages,
        "__summary__": f"Loaded {len(messages)} messages from {summary_scope}",
    }


FETCH_CHAT_THREAD = Tool(
    name="fetch_chat_thread",
    description=(
        "Load a chat conversation as plain text — either the most "
        "recent main-channel messages (omit thread_id) or every "
        "message in a specific thread (pass thread_id). Use after "
        "`search_knowledge_base` when you need to read who said what "
        "in context. Each message includes sender_id and timestamp. "
        "ACL is enforced — only chats the user is a member of."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "chat_type": {
                "type": "STRING",
                "enum": ["dm", "gm", "mdm", "pm"],
                "description": (
                    "Which chat surface: dm (direct), gm (named group), "
                    "mdm (multi-DM), pm (project chat — chat_id is the "
                    "project id in this case)."
                ),
            },
            "chat_id": {
                "type": "INTEGER",
                "description": "Numeric chat id (or project_id for chat_type=pm).",
            },
            "thread_id": {
                "type": "INTEGER",
                "description": (
                    "Optional. If provided, returns the thread's "
                    "messages instead of the main channel."
                ),
            },
        },
        "required": ["chat_type", "chat_id"],
    },
    run=_run,
)
