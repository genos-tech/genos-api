"""The closed-ancestor rule on MCP task reads (MIP-1413): published to a connected client,
never shown to the in-app agent.

`augment` functions add `effective_status` (and, when an ancestor decided it,
`closed_by_ancestor`) to the results of `get_task` and `list_tasks`. They run AFTER the
registry tool, so its access check has already decided which tasks are shown; they add a field
and never change what the tool returned. The in-app agent shares those tools, and what it
sees waits for the December eval, so the registry tools are left as they are. The rule is
`origin/services/task_ancestry.py`.

`closed_by_ancestor` names the ancestor by `task_id`, and by `display_id` only when the caller
may read that task (`task_ancestry.effective_fields`, which the in-app tools share).
"""

from __future__ import annotations

from origin.search_engine.agent.tools import ToolContext
from origin.services.task_ancestry import effective_fields


def with_task_ancestry(payload: dict, args: dict, mcp_args: dict, ctx: ToolContext) -> dict:
    """`get_task`: the task's `effective_status`."""
    fields = effective_fields([payload.get("task_id")], ctx)
    try:
        mine = fields.get(int(payload.get("task_id")))
    except (TypeError, ValueError):
        mine = None
    return {**payload, **mine} if mine else payload


def with_rows_ancestry(payload: dict, args: dict, mcp_args: dict, ctx: ToolContext) -> dict:
    """`list_tasks`: `effective_status` on each row, for the rows already returned."""
    rows = payload.get("tasks")
    if not isinstance(rows, list):
        return payload
    fields = effective_fields((r.get("task_id") for r in rows if isinstance(r, dict)), ctx)

    def add(row):
        if not isinstance(row, dict):
            return row
        try:
            extra = fields.get(int(row.get("task_id")))
        except (TypeError, ValueError):
            extra = None
        return {**row, **extra} if extra else row

    return {**payload, "tasks": [add(r) for r in rows]}
