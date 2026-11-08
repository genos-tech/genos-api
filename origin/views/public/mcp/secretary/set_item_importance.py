"""`set_item_importance` (MIP-1087): MCP-only, see `secretary/__init__.py`.

The member's correction of one item: not important to them, or "its priority is right". Stored
until they clear it; nothing else about the task changes.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import importance


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    done = importance.set_mark(ctx, args.get("task_id"), args.get("mark"), source="mcp")
    done.pop("previous", None)  # the Undo of the report card; this tool's answer is as it was
    what = {
        None: "cleared",
        "not_important": "marked not important",
        "priority_is_right": "marked as having the right priority",
    }[done["mark"]]
    return {**done, "__summary__": f"{done['display_id']} {what}."}


SET_ITEM_IMPORTANCE = Tool(
    name="set_item_importance",
    description=(
        "Correct what matters to the member on one task: not important, its priority is right, "
        "or clear the mark."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {"type": "INTEGER", "description": "The task."},
            "mark": {
                "type": "STRING",
                "enum": [*importance.MARKS, "clear"],
                "description": (
                    "not_important: never offer it as a top item or suggestion. "
                    "priority_is_right: stop asking whether to change its priority. "
                    "clear: remove the mark."
                ),
            },
        },
        "required": ["task_id", "mark"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)
