"""`reply_to_note_comment` (MIP-1100): MCP-only, see `note_threads/__init__.py`.

Adds a comment to an existing thread on a note, as the caller. Whoever may open the note may
comment on it in the editor (viewers included), so the access check is the one `get_note` makes.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services import collab_push
from origin.views.public.mcp.note_threads._common import (
    actor_id,
    clean_thread_id,
    reduced_via,
    refusal,
)

# One comment, not a document: the same size `get_note` shows a comment at.
MAX_TEXT_CHARS = 4000
MAX_LINES = 50


def _text(raw: Any) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise ToolError("`text` is required: the reply, as plain text.")
    if len(text) > MAX_TEXT_CHARS:
        raise ToolError(
            f"`text` is {len(text)} characters; a reply can be at most {MAX_TEXT_CHARS}."
        )
    if len([line for line in text.splitlines() if line.strip()]) > MAX_LINES:
        raise ToolError(f"`text` has more than {MAX_LINES} lines; shorten the reply.")
    return text


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    thread_id = clean_thread_id(args.get("thread_id"))
    text = _text(args.get("text"))
    ref = load_readable_note(args, ctx)
    try:
        done = collab_push.reply_note_thread(
            ref.family,
            ref.note_id,
            thread_id=thread_id,
            user_id=actor_id(ctx),
            text=text,
            via=reduced_via(ctx),
        )
    except collab_push.ThreadWriteError as err:
        raise refusal(err, thread_id=thread_id)
    resolved = done.get("threadResolved") is True
    return {
        "note_id": ref.note_id,
        "note_type": ref.family,
        "thread_id": thread_id,
        "comment_id": done.get("commentId"),
        "comment_count": done.get("commentCount"),
        "thread_resolved": resolved,
        "__summary__": (
            f"Replied to thread {thread_id} on note {ref.note_id}."
            + (" The thread is still resolved." if resolved else "")
        ),
    }


REPLY_TO_NOTE_COMMENT = Tool(
    name="reply_to_note_comment",
    description=(
        "Reply to a comment thread on a note, as the caller. REQUIRES USER APPROVAL. "
        "Pass `note_type` with `note_id` and the `thread_id` from the note's comment threads; "
        "`text` is the reply in plain text."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "note_type": {
                "type": "STRING",
                "enum": ["personal", "team", "shared", "task", "chat"],
                "description": "Which kind of note (personal = My Notes).",
            },
            "note_id": {"type": "INTEGER", "description": "The note's id."},
            "thread_id": {
                "type": "STRING",
                "description": "The thread to reply to: `thread_id` from `comment_threads`.",
            },
            "text": {
                "type": "STRING",
                "description": (
                    f"The reply, plain text (at most {MAX_TEXT_CHARS} characters). Each line "
                    "becomes a paragraph; no formatting or @-mentions."
                ),
            },
        },
        "required": ["note_type", "note_id", "thread_id", "text"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)
