"""`get_milestone_summary` tool — task-count statistics for one milestone.

Returns total / per-status / per-priority / per-effort task counts and
overdue count, plus the milestone's assignees, and where to start
(`ready_now`, `in_progress`, `blocked` with their open blockers, and the
`critical_path` — see `milestone_order`). Counts tasks via the
`_milestone_task_q` predicate from `list_tasks` so subtasks (linked
through `parent_task_id`) are included — same Q-union as
`_serialize_milestone` in milestone_views.py, so the agent rollup
reconciles with the table view.

Note on `status_breakdown`: it keeps a "Deleted" key (the status value, not
the soft-delete flag `is_deleted`) so the agent can answer the literal
'closed/opened/wip/deleted' question. A Deleted task is GONE, not finished
(MIP-777): it is counted only in that row, never in `total_tasks`, the
priority/effort breakdowns, or as closed. The milestone's own backing task is
not one of its tasks either.

ACL contract:
  * Tenant guard: milestone.team_id must equal ctx.team_id.
  * Membership guard: milestone.project_id must be in caller's
    `ProjectMembers` set.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.milestone_order import order_of_work
from origin.search_engine.agent.tools.task_enums import blank_to_none
from origin.services.milestone_rollup import milestone_tasks_q, milestone_work_q
from origin.services.user_time import today_for_user_id

# Five-row breakdown (vs four in get_project_summary). "Deleted" is the
# enum value, not the is_deleted tombstone.
_STATUS_LABELS = ["Open", "WIP", "Blocked", "Pending", "Closed", "Deleted"]
_PRIORITY_LABELS = ["Minimal", "Low", "Normal", "High", "Critical"]
_EFFORT_LABELS = ["Minimal", "Low", "Moderate", "High", "Extensive"]
_CLOSED_STATUSES = ["Closed", "Deleted"]


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_id = args.get("milestone_id")
    try:
        milestone_id = int(raw_id)
    except (TypeError, ValueError):
        raise ToolError(f"`milestone_id` must be an integer (got {raw_id!r}).")

    try:
        m = (
            MilestoneMaster.objects.select_related("project", "task")
            .prefetch_related("milestone_assignees__user")
            .get(milestone_id=milestone_id, is_deleted=False)
        )
    except MilestoneMaster.DoesNotExist:
        raise ToolError(f"Milestone {milestone_id} not found.")

    if str(getattr(m, "team_id", "") or "") != ctx.team_id:
        raise ToolForbidden("Not authorized: milestone is in a different team.")

    is_member = ProjectMembers.objects.filter(
        project_id=m.project_id,
        attendee_id=ctx.user_id,
    ).exists()
    if not is_member:
        raise ToolForbidden(
            f"Not authorized to access milestone {milestone_id}. "
            "You are not a member of that project."
        )

    today = today_for_user_id(ctx.user_id)

    # Single aggregate query for every breakdown so this is one DB round
    # trip rather than O(status × priority × effort) counts.
    # The milestone's live work: its own backing row and "Deleted" tasks are
    # not part of its totals or breakdowns (MIP-777). Deleted tasks are still
    # counted, apart, in the `Deleted` row of `status_breakdown`.
    qs = TaskMaster.objects.filter(milestone_work_q(m)).distinct()
    annotations: dict[str, Any] = {
        "total_tasks": Count("task_id", distinct=True),
        "overdue_count": Count(
            "task_id",
            distinct=True,
            filter=Q(due_date__isnull=False, due_date__lt=today) & ~Q(status__in=_CLOSED_STATUSES),
        ),
        "unassigned_task_count": Count(
            "task_id", distinct=True, filter=Q(assignee_id__isnull=True)
        ),
    }
    for label in _STATUS_LABELS:
        annotations[f"_status_{label}"] = Count("task_id", distinct=True, filter=Q(status=label))
    for label in _PRIORITY_LABELS:
        annotations[f"_priority_{label}"] = Count(
            "task_id", distinct=True, filter=Q(priority=label)
        )
    for label in _EFFORT_LABELS:
        annotations[f"_effort_{label}"] = Count(
            "task_id", distinct=True, filter=Q(effort_level=label)
        )
    agg = qs.aggregate(**annotations)

    status_breakdown = {label: agg.get(f"_status_{label}", 0) or 0 for label in _STATUS_LABELS}
    status_breakdown["Deleted"] = (
        TaskMaster.objects.filter(milestone_tasks_q(m, own_row=False), status="Deleted")
        .distinct()
        .count()
    )
    priority_breakdown = {
        label: agg.get(f"_priority_{label}", 0) or 0 for label in _PRIORITY_LABELS
    }
    effort_breakdown = {label: agg.get(f"_effort_{label}", 0) or 0 for label in _EFFORT_LABELS}

    assignees = [
        {
            "user_id": str(a.user_id) if a.user_id else None,
            "username": getattr(a.user, "username", None) or "",
        }
        for a in m.milestone_assignees.all()
        if a.user_id is not None
    ]

    open_n = status_breakdown.get("Open", 0)
    wip_n = status_breakdown.get("WIP", 0)
    # Deleted is gone, not finished: it is not progress.
    closed_n = status_breakdown.get("Closed", 0)
    overdue_n = agg.get("overdue_count", 0) or 0
    total = agg.get("total_tasks", 0) or 0

    member_project_ids = set(
        ProjectMembers.objects.filter(
            attendee_id=ctx.user_id, project__team_id=ctx.team_id
        ).values_list("project_id", flat=True)
    )
    order = order_of_work(m, member_project_ids)

    return {
        "milestone_id": m.milestone_id,
        "project_id": m.project_id,
        "project_name": m.project.project_name if m.project_id else None,
        "title": m.title,
        "status": m.status,
        "priority": blank_to_none(m.priority),
        "effort_level": blank_to_none(m.effort_level),
        "due_date": m.due_date.isoformat() if m.due_date else None,
        "start_date": m.start_date.isoformat() if m.start_date else None,
        "assignees": assignees,
        "total_tasks": total,
        "unassigned_task_count": agg.get("unassigned_task_count", 0) or 0,
        "status_breakdown": status_breakdown,
        "priority_breakdown": priority_breakdown,
        "effort_breakdown": effort_breakdown,
        "overdue_count": overdue_n,
        **order,
        "__summary__": (
            f"Milestone '{m.title}': {total} task(s) — "
            f"{open_n} open, {wip_n} WIP, {closed_n} closed, "
            f"{overdue_n} overdue; {order['ready_now_count']} ready to start, "
            f"{order['blocked_count']} blocked"
        ),
    }


GET_MILESTONE_SUMMARY = Tool(
    name="get_milestone_summary",
    description=(
        "Return task-count statistics for one milestone: total tasks, "
        "status_breakdown (Open / WIP / Pending / Closed, plus a separate "
        "Deleted row for tasks with status Deleted, which are gone: they "
        "are NOT in total_tasks or the other breakdowns and do not count as "
        "closed), priority_breakdown (Minimal/Low/Normal/High/Critical), "
        "effort_breakdown (Minimal/Low/Moderate/High/Extensive), overdue "
        "count, unassigned count, and the milestone's assignees. It also "
        "says where to start: `ready_now` (open tasks whose blockers are all "
        "closed, counting the blockers of their parent tasks), `in_progress`, "
        "`blocked` (each with its open blockers) and `critical_path` (the "
        "longest chain of open tasks along the dependencies). 'Open' status "
        "alone does not mean ready: use `ready_now`. Counts "
        "include subtasks (tasks whose parent_task_id is the milestone's "
        "backing task), matching the milestone table view. Use for 'how "
        "many tasks in milestone X?', 'how many Critical tasks under "
        "milestone X?', 'what's overdue in milestone X?', 'which task should "
        "I take first in milestone X?'. Resolve a "
        "milestone name to a numeric id with `list_milestones` first if "
        "needed. Only accessible to project members."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "milestone_id": {
                "type": "INTEGER",
                "description": "Numeric milestone id to summarise.",
            },
        },
        "required": ["milestone_id"],
    },
    run=_run,
)
