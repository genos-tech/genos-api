"""`reply_to_task_body_comment` (MIP-1863): MCP-only, see `task_body_threads/__init__.py`.

Adds a comment to a thread on a task's body, as the caller. Anyone who may read the task may reply
(the editor lets viewers comment too), so the access check is `load_task`'s, as for the other task writes.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.task_access import load_task
from origin.services import collab_push
from origin.views.public.mcp.note_threads._common import actor_id, reduced_via
from origin.views.public.mcp.task_body_threads._common import clean_thread_id, refusal

MAX_TEXT_CHARS = 4000
MAX_LINES = 50


def _text(raw: Any) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        raise ToolError("`text` is required: the comment, as plain text.")
    if len(text) > MAX_TEXT_CHARS:
        raise ToolError(
            f"`text` is {len(text)} characters; a comment can be at most {MAX_TEXT_CHARS}."
        )
    if len([line for line in text.splitlines() if line.strip()]) > MAX_LINES:
        raise ToolError(f"`text` has more than {MAX_LINES} lines; shorten the comment.")
    return text


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    thread_id = clean_thread_id(args.get("thread_id"))
    text = _text(args.get("text"))
    task = load_task(args.get("task_id"), ctx, action="reply to a comment on")
    try:
        done = collab_push.reply_thread_in(
            collab_push.task_body_document(task.task_id),
            thread_id=thread_id,
            user_id=actor_id(ctx),
            text=text,
            via=reduced_via(ctx),
        )
    except collab_push.ThreadWriteError as err:
        raise refusal(err, thread_id=thread_id)
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "thread_id": thread_id,
        "comment_id": done.get("commentId"),
        "comment_count": done.get("commentCount"),
        "thread_resolved": done.get("threadResolved") is True,
        "__summary__": f"Replied to a comment thread on the body of {task.display_id}.",
    }


REPLY_TO_TASK_BODY_COMMENT = Tool(
    name="reply_to_task_body_comment",
    description=(
        "Reply to a comment thread on a task's body, as the caller. REQUIRES USER APPROVAL. Pass "
        "`task_id` and the `thread_id` from `body_comment_threads` in `get_task`, and `text` (plain text)."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {"type": "INTEGER", "description": "The task's id."},
            "thread_id": {
                "type": "STRING",
                "description": "The thread to answer: `thread_id` from `body_comment_threads`.",
            },
            "text": {
                "type": "STRING",
                "description": (
                    f"The reply, plain text (at most {MAX_TEXT_CHARS} characters). Each line becomes a "
                    "paragraph; no formatting or @-mentions."
                ),
            },
        },
        "required": ["task_id", "thread_id", "text"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)
