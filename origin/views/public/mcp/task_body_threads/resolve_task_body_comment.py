"""`resolve_task_body_comment` (MIP-1863): MCP-only, see `task_body_threads/__init__.py`.

Marks a thread on a task's body resolved. Narrower than the editor: only the thread's author, or the
task's assignee or reporter (`_common.may_resolve_any`), may. Collab checks the thread's author in the same
transaction as the write. Reopening is done in the editor, as for notes.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.search_engine.agent.tools.task_access import load_task
from origin.services import collab_push
from origin.views.public.mcp.note_threads._common import actor_id
from origin.views.public.mcp.task_body_threads._common import (
    clean_thread_id,
    may_resolve_any,
    refusal,
    refuse_if_archived,
)


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    thread_id = clean_thread_id(args.get("thread_id"))
    task = load_task(args.get("task_id"), ctx, action="resolve a comment on")
    refuse_if_archived(task)
    try:
        done = collab_push.resolve_thread_in(
            collab_push.task_body_document(task.task_id),
            thread_id=thread_id,
            user_id=actor_id(ctx),
            may_resolve_any=may_resolve_any(task, ctx),
        )
    except collab_push.ThreadWriteError as err:
        raise refusal(err, thread_id=thread_id, resolving=True)
    already = done.get("alreadyResolved") is True
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "thread_id": thread_id,
        "resolved": True,
        "already_resolved": already,
        "resolved_by": done.get("resolvedBy"),
        "__summary__": (
            f"Thread {thread_id} on the body of {task.display_id} was already resolved."
            if already
            else f"Resolved thread {thread_id} on the body of {task.display_id}."
        ),
    }


RESOLVE_TASK_BODY_COMMENT = Tool(
    name="resolve_task_body_comment",
    description=(
        "Mark a comment thread on a task's body resolved. REQUIRES USER APPROVAL. Only the thread's "
        "author or the task's assignee or reporter can; pass `task_id` and the `thread_id` from "
        "`body_comment_threads` in `get_task`."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {"type": "INTEGER", "description": "The task's id."},
            "thread_id": {
                "type": "STRING",
                "description": "The thread to resolve: `thread_id` from `body_comment_threads`.",
            },
        },
        "required": ["task_id", "thread_id"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)
