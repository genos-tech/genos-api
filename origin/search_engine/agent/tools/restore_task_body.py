"""`restore_task_body` write tool (MIP-880): put a task's description back.

`update_task` with `content_text` replaces the whole description, and on a
surface with no undo that is the edit most worth being able to walk back. When
the rewrite came from an agent or an API key, the previous body is kept on the
`description_edited` row of `get_task_history`, flagged `restorable`, and this
restores it, through the one implementation the page's restore shares
(`services/task_body_restore`).

The restore is itself a rewrite, and it keeps the body it replaces, so a
mistaken restore can be undone the same way.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.task_access import as_int, load_task
from origin.services.task_body_restore import RestoreRefused, restore_task_body


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    task = load_task(args.get("task_id"), ctx, action="restore the description of")
    activity_id = as_int(args.get("activity_id"), "activity_id")
    try:
        result = restore_task_body(task=task, activity_id=activity_id, actor_id=ctx.user_id)
    except RestoreRefused as refused:
        raise ToolError(
            f"{refused.message} Use `get_task_history` and pick a `description_edited` entry "
            "whose `restorable` is true; its `activity_id` is what this takes."
        )
    except Exception as e:  # noqa: BLE001
        ref = log_failure("restore_task_body tool failed", e)
        raise ToolError(f"Failed to restore the description (ref {ref}).")
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "restored_from": result["restoredFrom"],
        "replaced_body_kept": result["replacedBodyKept"],
        "__summary__": (
            f"Restored the description of {task.display_id} from history entry "
            f"{result['restoredFrom']}"
        ),
    }


RESTORE_TASK_BODY = Tool(
    name="restore_task_body",
    description=(
        "Put a task's description back to what it was before an earlier edit. Call "
        "`get_task_history` FIRST (not `fetch_task`): pick a `description_edited` entry whose "
        "`restorable` is true, which is the edit whose OLD text you want back, and take its "
        "`activity_id`. REQUIRES USER APPROVAL — the current description is replaced. Required: "
        "task_id, activity_id. The body being replaced is kept, so a restore can be undone the "
        "same way. Use it to walk back a bad rewrite; for a small change to the current text "
        "use update_task."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "Numeric task id whose description to restore.",
            },
            "activity_id": {
                "type": "INTEGER",
                "description": (
                    "The `activity_id` of a restorable `description_edited` entry from "
                    "get_task_history."
                ),
            },
        },
        "required": ["task_id", "activity_id"],
    },
    run=_run,
    requires_approval=True,
)
