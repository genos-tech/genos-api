"""`update_comment` write tool (MIP-880): fix what you said in one of your comments.

`add_comment` is one-way: an agent that mistyped a comment, or posted it on the
wrong task's thread, could only add another. This changes the text of one of the
caller's own comments, through the one implementation the page's edit shares
(`services/task_comment_manage.edit_comment`): the comment, its copy in the PM
task thread, and a notification to anyone the edit newly @-mentions.

Only the author can edit, as on the page. The markdown is turned into the same
blocks `add_comment` writes, with the same chip syntax.

Approval flow: `requires_approval=True`. The user sees the new text before it
replaces the old.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.search_engine.agent.chip_syntax import CHIP_SYNTAX_HINT
from origin.search_engine.agent.tools.add_comment import _wrap_blocknote
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.body_limits import check_body_size
from origin.search_engine.agent.tools.task_access import load_own_comment, load_task
from origin.services.task_comment_manage import edit_comment


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    check_body_size(args.get("body_text"), "body_text")
    body_text = (args.get("body_text") or "").strip()
    if not body_text:
        raise ToolError("`body_text` is required and must be non-empty.")

    task = load_task(args.get("task_id"), ctx, action="edit a comment on")
    comment = load_own_comment(task, args.get("comment_id"), ctx)

    try:
        newly_mentioned = edit_comment(comment, _wrap_blocknote(body_text, ctx))
    except Exception as e:  # noqa: BLE001
        ref = log_failure("update_comment tool failed", e)
        raise ToolError(f"Failed to edit the comment (ref {ref}).")

    summary = f"Edited comment #{comment.comment_id} on {task.display_id}"
    if newly_mentioned:
        summary += f"; {len(newly_mentioned)} newly mentioned person(s) were notified"
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "comment_id": comment.comment_id,
        "newly_mentioned": len(newly_mentioned),
        "__summary__": summary,
    }


UPDATE_COMMENT = Tool(
    name="update_comment",
    description=(
        "Fix one of the user's OWN comments on a task. Call `fetch_task` FIRST: it lists each "
        "comment's `comment_id` and `sender_id` (only comments the user wrote can be changed, so "
        "match `sender_id` against their id from `get_current_user`). REQUIRES USER APPROVAL — "
        "the user sees the new text before it replaces the old. Required: task_id, comment_id, "
        "body_text. The whole comment is replaced, so send the full new text. Use it to fix a "
        "typo or a wrong detail; to say something new, use add_comment."
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
            "body_text": {
                "type": "STRING",
                "description": (
                    "The full new comment, in markdown (headings, lists, **bold**, *italic*, "
                    "`code`, fenced code blocks, tables, links)." + CHIP_SYNTAX_HINT
                ),
            },
        },
        "required": ["task_id", "comment_id", "body_text"],
    },
    run=_run,
    requires_approval=True,
)
