"""`delete_comment` write tool (MIP-880): take back one of your own comments.

The page's delete, through the one implementation it shares
(`services/task_comment_manage.soft_delete_comment`): the comment is
soft-deleted, its copy in the PM task thread is tombstoned, and the search index
forgets it. Soft, because `comment_id` is a per-task sequence and a hard delete
would let the next comment reuse the id.

Its own tool rather than an option of `update_comment`, so the MCP opt-in for
destructive tools covers a delete and nothing else: a key without it can still
fix a comment. It is destructive there because nothing brings the comment back
from this surface.

Only the author can delete, as on the page. Deleting a deleted comment succeeds.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.task_access import load_own_comment, load_task
from origin.services.task_comment_manage import soft_delete_comment


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    task = load_task(args.get("task_id"), ctx, action="delete a comment on")
    comment = load_own_comment(task, args.get("comment_id"), ctx, allow_deleted=True)
    already = comment.is_deleted
    try:
        remaining = soft_delete_comment(comment)
    except Exception as e:  # noqa: BLE001
        ref = log_failure("delete_comment tool failed", e)
        raise ToolError(f"Failed to delete the comment (ref {ref}).")
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "comment_id": comment.comment_id,
        "deleted": True,
        "already_deleted": already,
        "comments_left": remaining,
        "__summary__": (
            f"Comment #{comment.comment_id} on {task.display_id} was already deleted."
            if already
            else f"Deleted comment #{comment.comment_id} on {task.display_id}"
        ),
    }


DELETE_COMMENT = Tool(
    name="delete_comment",
    description=(
        "Delete one of the user's OWN comments on a task. Call `fetch_task` FIRST: it lists each "
        "comment's `comment_id` and `sender_id` (only comments the user wrote can be deleted, so "
        "match `sender_id` against their id from `get_current_user`). REQUIRES USER APPROVAL — "
        "destructive: the comment disappears from the task, its thread and search, and can't be "
        "brought back from here. Use it only when the user explicitly asks to delete that "
        "comment; to fix its text use update_comment. Required: task_id, comment_id. Deleting "
        "an already-deleted comment succeeds."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "Numeric task id the comment is on.",
            },
            "comment_id": {
                "type": "INTEGER",
                "description": "The comment's id on that task, from fetch_task.",
            },
        },
        "required": ["task_id", "comment_id"],
    },
    run=_run,
    requires_approval=True,
)
