"""Task and milestone progress on MCP (MIP-1351, MIP-1361): published to a connected
client, never shown to the in-app agent.

`last_progress_at` is when a task, or anything under it, last had real progress; see
`origin/services/task_progress.py`. Two things live here:

  * `get_progress_history`, an MCP-only tool (see `secretary/__init__.py` for why those
    live outside `origin/search_engine/`);
  * `augment` functions that add `last_progress_at` to the results of tools the in-app
    agent shares (`get_task`, `list_tasks`, `list_milestones`, `get_milestone_summary`,
    `get_stale_tasks`). They run AFTER the registry tool, so its access check has already
    decided what is shown, and they add a field to what it returned and never change what
    it returned. Anything that changes what the in-app model sees waits for the December
    eval, so the registry tools are left as they are.

One tool per module, as in `search_engine/agent/tools/`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from django.utils import timezone

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext, register_tool
from origin.views.public.mcp.progress.get_progress_history import GET_PROGRESS_HISTORY

PROGRESS_TOOLS = (GET_PROGRESS_HISTORY,)

for _tool in PROGRESS_TOOLS:
    register_tool(_tool)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _times(task_ids: Iterable[Any], ctx: ToolContext) -> dict[int, datetime | None]:
    """`last_progress_at` for tasks already in a result, within the caller's team."""
    ids = {int(i) for i in task_ids if i is not None}
    if not ids:
        return {}
    return dict(
        TaskMaster.objects.filter(task_id__in=ids, team_id=ctx.team_id).values_list(
            "task_id", "last_progress_at"
        )
    )


def with_task_progress(payload: dict, args: dict, mcp_args: dict, ctx: ToolContext) -> dict:
    """`get_task`: the task's own `last_progress_at`."""
    times = _times([payload.get("task_id")], ctx)
    return {**payload, "last_progress_at": _iso(times.get(payload.get("task_id")))}


def with_rows_progress(payload: dict, args: dict, mcp_args: dict, ctx: ToolContext) -> dict:
    """`list_tasks`, `list_milestones`: `last_progress_at` on each row.

    A milestone's value is on its backing task, which the row names as `task_id`."""
    for key in ("tasks", "milestones"):
        rows = payload.get(key)
        if not isinstance(rows, list):
            continue
        times = _times((r.get("task_id") for r in rows if isinstance(r, dict)), ctx)
        payload = {
            **payload,
            key: [
                {**r, "last_progress_at": _iso(times.get(r.get("task_id")))}
                if isinstance(r, dict)
                else r
                for r in rows
            ],
        }
    return payload


def with_milestone_progress(payload: dict, args: dict, mcp_args: dict, ctx: ToolContext) -> dict:
    """`get_milestone_summary`: the milestone's `last_progress_at`, from its backing task."""
    backing = (
        MilestoneMaster.objects.filter(
            milestone_id=payload.get("milestone_id"), team_id=ctx.team_id
        )
        .values_list("task_id", flat=True)
        .first()
    )
    times = _times([backing], ctx)
    return {**payload, "last_progress_at": _iso(times.get(backing))}


def with_stale_progress(payload: dict, args: dict, mcp_args: dict, ctx: ToolContext) -> dict:
    """`get_stale_tasks`: tell a task that is quiet itself from one with activity under it.

    The tool judges by the task's own `ts_updated_at`, so a milestone whose tasks are
    moving every day still lists as stale. Each row gets `last_progress_at` and
    `days_since_progress` (null when nothing is recorded yet), and nothing is dropped: the
    tool's limit applies before this, so removing rows here would hand back fewer than
    asked for with no way to ask for more."""
    rows = payload.get("tasks")
    if not isinstance(rows, list):
        return payload
    times = _times((r.get("task_id") for r in rows if isinstance(r, dict)), ctx)
    now = timezone.now()
    out = []
    for r in rows:
        if not isinstance(r, dict):
            out.append(r)
            continue
        last = times.get(r.get("task_id"))
        out.append(
            {
                **r,
                "last_progress_at": _iso(last),
                "days_since_progress": (now - last).days if last else None,
            }
        )
    return {**payload, "tasks": out}
