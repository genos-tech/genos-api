"""`get_progress_history` (MIP-1361): MCP-only, see `progress/__init__.py`.

What happened at or under one task or milestone, newest first: which task changed, how
far below it, what kind of change, who, when and how it arrived. It reads
`TaskProgressFact` (MIP-1351), which holds one row per change per ancestor, so this is
one indexed lookup however deep the tree is.

Access: the tenant guard first, then PROJECT MEMBERSHIP. That is stricter than
`get_task`, which also lets the assignee or reporter in: this tool names tasks other
than the one asked about, and a person who is only the assignee of one task has no
business reading what happened to its siblings.
"""

from __future__ import annotations

from datetime import datetime, time
from datetime import timezone as dt_timezone
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.models.task.task_progress_models import TaskProgressFact
from origin.search_engine.agent.tools import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.base import wrap_workspace_content
from origin.search_engine.agent.tools.get_task_history import _via

_DEFAULT_LIMIT = 30
_MAX_LIMIT = 100


def _since(raw: Any) -> datetime | None:
    """`since` as an aware datetime: a date (start of that day, UTC) or a datetime."""
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    when = parse_datetime(text)
    if when is None:
        day = parse_date(text)
        when = datetime.combine(day, time.min) if day else None
    if when is None:
        raise ToolError(f"`since` must be an ISO date or datetime (got {raw!r}).")
    return when if timezone.is_aware(when) else timezone.make_aware(when, dt_timezone.utc)


def _limit(raw: Any) -> int:
    if raw in (None, ""):
        return _DEFAULT_LIMIT
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"`limit` must be an integer (got {raw!r}).")
    if not 1 <= limit <= _MAX_LIMIT:
        raise ToolError(f"`limit` must be between 1 and {_MAX_LIMIT} (got {limit}).")
    return limit


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_task_id = args.get("task_id")
    try:
        task_id = int(raw_task_id)
    except (TypeError, ValueError):
        raise ToolError(f"task_id must be an integer (got {raw_task_id!r}).")
    since, limit = _since(args.get("since")), _limit(args.get("limit"))

    try:
        task = TaskMaster.objects.select_related("project").get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        raise ToolError(f"Task {task_id} not found.")
    if task.is_deleted:
        raise ToolError(f"Task {task_id} has been deleted.")
    if str(getattr(task, "team_id", "") or "") != ctx.team_id:
        raise ToolForbidden("Not authorized: task is in a different team.")
    if not ProjectMembers.objects.filter(
        project_id=task.project_id, attendee_id=ctx.user_id
    ).exists():
        raise ToolForbidden(f"Not authorized to read the progress under task {task_id}.")

    facts = TaskProgressFact.objects.filter(ancestor_task_id=task_id)
    if since is not None:
        facts = facts.filter(ts__gte=since)
    # One more than asked for, to say whether there is more.
    rows = list(
        facts.select_related("source_task__project", "activity").order_by("-ts", "-fact_id")[
            : limit + 1
        ]
    )
    more = len(rows) > limit
    rows = rows[:limit]

    # Imported here: `label_acl` imports `tools.base`, which runs this package's
    # `__init__` (the same cycle `fetch_task` documents).
    from origin.search_engine.agent.label_acl import LabelAccess  # noqa: PLC0415

    wanted = {str(r.actor_id) for r in rows if r.actor_id}
    names = LabelAccess(ctx).usernames(wanted) if wanted else {}

    entries = []
    for r in rows:
        source = r.source_task
        entries.append(
            {
                "ts": r.ts.isoformat(),
                "task_id": r.source_task_id,
                "display_id": source.display_id,
                "title": wrap_workspace_content(source.title or ""),
                # 0 = this task's own change, 1 = a direct child's, 2 = a grandchild's...
                "depth": r.depth,
                "action_type": r.action_type,
                "field": r.activity.field_name,
                "actor": (
                    {
                        "user_id": str(r.actor_id),
                        "username": wrap_workspace_content(names[str(r.actor_id)])
                        if str(r.actor_id) in names
                        else None,
                    }
                    if r.actor_id
                    else None
                ),
                "via": _via(r.activity),
            }
        )

    last = task.last_progress_at
    return {
        "task_id": task_id,
        "display_id": task.display_id,
        "last_progress_at": last.isoformat() if last else None,
        "entries": entries,
        "has_more": more,
        "__summary__": (
            f"{len(entries)} change(s) at or under {task.display_id}"
            + (f" since {since.date().isoformat()}" if since else "")
            + ("; more before the oldest shown" if more else "")
        ),
    }


GET_PROGRESS_HISTORY = Tool(
    name="get_progress_history",
    description="What changed at or under one task or milestone, newest first.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "The task, or the milestone's own task row.",
            },
            "since": {
                "type": "STRING",
                "description": "Only changes from this ISO date or datetime on.",
            },
            "limit": {
                "type": "INTEGER",
                "description": f"How many entries (1-{_MAX_LIMIT}, default {_DEFAULT_LIMIT}).",
            },
        },
        "required": ["task_id"],
    },
    run=_run,
    mcp_only=True,
)
