"""`set_item_importance` (MIP-1087): MCP-only, see `secretary/__init__.py`.

The member's correction of one item: not important to them, or "its priority is right". Stored
until they clear it; nothing else about the task changes.

With `AGENT_PARITY_TOOLS` on (MIP-1588) it also offers `on_it`, the mark a report row's "On it" button
sets: the task leaves the member's reports for 7 days, or until its status, priority, due date or
overdue state changes (`services/report_memory._answer_holds`). It is the same row the app writes
(`importance.set_mark`, which the app's endpoint calls too), with the source `mcp` instead of `report`.
The result then also names the mark it replaced (`previous`), so an overwrite is visible: `on_it` is
weaker than `not_important`, and the member may well have set the stronger one before. With the flag off
the tool is exactly what it was: three values, `on_it` refused, no `previous`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.parity import parity_on
from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import importance

_WHAT = {
    None: "cleared",
    "not_important": "marked not important",
    "priority_is_right": "marked as having the right priority",
    "on_it": 'marked "on it"',
}
# Said after an `on_it` (flag on): what the mark does.
_ON_IT_NOTE = " It is left out of reports for 7 days, or until it changes."
# What a mark that was replaced is called, after "it was marked" (flag on).
_WAS = {
    "not_important": "not important",
    "priority_is_right": "as having the right priority",
    "on_it": '"on it"',
}


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    if not parity_on():
        done = importance.set_mark(ctx, args.get("task_id"), args.get("mark"), source="mcp")
        done.pop("previous", None)  # the Undo of the report card; this tool's answer is as it was
        what = _WHAT[done["mark"]]
        return {**done, "__summary__": f"{done['display_id']} {what}."}
    done = importance.set_mark(
        ctx, args.get("task_id"), args.get("mark"), allowed=importance.ALL_MARKS, source="mcp"
    )
    # Kept: the mark it replaced, so an overwrite is visible (`on_it` over `not_important` weakens it).
    previous = done.get("previous")
    if previous is None:
        replaced = " (there was no mark)" if done["mark"] is None else ""
    elif previous == done["mark"]:
        replaced = " (it already was)"
    else:
        replaced = f" (it was marked {_WAS.get(previous, previous)})"
    note = _ON_IT_NOTE if done["mark"] == "on_it" else ""
    return {
        **done,
        "__summary__": f"{done['display_id']} {_WHAT[done['mark']]}{replaced}.{note}",
    }


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
