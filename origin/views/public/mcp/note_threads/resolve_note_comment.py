"""`resolve_note_comment` (MIP-1100): MCP-only, see `note_threads/__init__.py`.

Marks a comment thread on a note resolved. Narrower than the editor, where any commenter may
resolve any thread: only the thread's author or the note's owner may, here. The note's owner is
decided here (`is_note_owner`); the thread's author is read from the doc by genos-collab, in the
same transaction as the write.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services import collab_push
from origin.views.public.mcp.note_threads._common import (
    actor_id,
    clean_thread_id,
    is_note_owner,
    refusal,
)


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    thread_id = clean_thread_id(args.get("thread_id"))
    ref = load_readable_note(args, ctx)
    try:
        done = collab_push.resolve_note_thread(
            ref.family,
            ref.note_id,
            thread_id=thread_id,
            user_id=actor_id(ctx),
            may_resolve_any=is_note_owner(ref, ctx),
        )
    except collab_push.ThreadWriteError as err:
        raise refusal(err, thread_id=thread_id, resolving=True)
    already = done.get("alreadyResolved") is True
    return {
        "note_id": ref.note_id,
        "note_type": ref.family,
        "thread_id": thread_id,
        "resolved": True,
        "already_resolved": already,
        "resolved_by": done.get("resolvedBy"),
        "__summary__": (
            f"Thread {thread_id} on note {ref.note_id} was already resolved."
            if already
            else f"Resolved thread {thread_id} on note {ref.note_id}."
        ),
    }


RESOLVE_NOTE_COMMENT = Tool(
    name="resolve_note_comment",
    description=(
        "Mark a comment thread on a note resolved. REQUIRES USER APPROVAL. Only the thread's "
        "author or the note's owner can. Pass `note_type` with `note_id` and the `thread_id` "
        "from the note's comment threads."
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
                "description": "The thread to resolve: `thread_id` from `comment_threads`.",
            },
        },
        "required": ["note_type", "note_id", "thread_id"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)
