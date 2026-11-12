"""The closed-ancestor rule on MCP task reads (MIP-1413): published to a connected client,
never shown to the in-app agent.

`augment` functions add `effective_status` (and, when an ancestor decided it,
`closed_by_ancestor`) to the results of `get_task` and `list_tasks`. They run AFTER the
registry tool, so its access check has already decided which tasks are shown; they add a field
and never change what the tool returned. The in-app agent shares those tools, and what it
sees waits for the December eval, so the registry tools are left as they are. The rule is
`origin/services/task_ancestry.py`.

`closed_by_ancestor` names the ancestor by `task_id`, and by `display_id` only when the caller
may read that task (`LabelAccess`): a task's assignee who is not in its project can read the
task, but not necessarily its grandparent.
"""

from __future__ import annotations

from typing import Any, Iterable

from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools import ToolContext
from origin.services.task_ancestry import effective_statuses


def _fields(task_ids: Iterable[Any], ctx: ToolContext) -> dict[int, dict[str, Any]]:
    ids = []
    for raw in task_ids:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    found = effective_statuses(ids, ctx.team_id)
    ancestors = {e.closed_by_ancestor_id for e in found.values() if e.closed_by_ancestor_id}
    readable = LabelAccess(ctx).tasks(ancestors) if ancestors else {}
    out: dict[int, dict[str, Any]] = {}
    for task_id, e in found.items():
        fields: dict[str, Any] = {"effective_status": e.status}
        if e.closed_by_ancestor_id is not None:
            ancestor = readable.get(e.closed_by_ancestor_id)
            fields["closed_by_ancestor"] = {
                "task_id": e.closed_by_ancestor_id,
                "display_id": ancestor.display_id if ancestor else None,
            }
        out[task_id] = fields
    return out


def with_task_ancestry(payload: dict, args: dict, mcp_args: dict, ctx: ToolContext) -> dict:
    """`get_task`: the task's `effective_status`."""
    fields = _fields([payload.get("task_id")], ctx)
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
    fields = _fields((r.get("task_id") for r in rows if isinstance(r, dict)), ctx)

    def add(row):
        if not isinstance(row, dict):
            return row
        try:
            extra = fields.get(int(row.get("task_id")))
        except (TypeError, ValueError):
            extra = None
        return {**row, **extra} if extra else row

    return {**payload, "tasks": [add(r) for r in rows]}
