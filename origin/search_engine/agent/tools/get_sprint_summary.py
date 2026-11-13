"""`get_sprint_summary` tool — per-sprint status / priority breakdown.

Returns total / per-status / per-priority task counts, overdue count,
days remaining until end_date, and the list of milestones associated
with the sprint. Answers "how is sprint X going?".

Counts source: only tasks with `sprint_id == sprint.sprint_id` are
included — sprint membership is a direct FK on TaskMaster, unlike
milestones which have the dual FK + parent_task_id linkage. Subtasks
inherit their parent's sprint when the parent is moved into a sprint
(see TaskMaster mutators in views/task/task_views.py), so a single
sprint_id filter is the canonical scope.

ACL contract:
  * Tenant guard: sprint.team_id == ctx.team_id.
  * Membership guard: sprint.project_id is in caller's ProjectMembers.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ancestor_rule
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.services.milestone_rollup import milestone_work_q
from origin.services.sprint_status import derive_status
from origin.services.user_time import today_for_user_id

_STATUS_LABELS = ["Open", "WIP", "Blocked", "Pending", "Closed", "Deleted"]
_PRIORITY_LABELS = ["Minimal", "Low", "Normal", "High", "Critical"]
_CLOSED_STATUSES = ["Closed", "Deleted"]


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_id = args.get("sprint_id")
    try:
        sprint_id = int(raw_id)
    except (TypeError, ValueError):
        raise ToolError(f"`sprint_id` must be an integer (got {raw_id!r}).")

    not_found = f"Sprint {sprint_id} not found."
    try:
        sprint = Sprint.objects.select_related("project").get(sprint_id=sprint_id, is_deleted=False)
    except Sprint.DoesNotExist:
        raise ToolError(not_found)

    # A sprint in another team, or in a project the caller isn't in, answers
    # with the same words as one that doesn't exist, so the id can't be probed
    # (this tool is published over MCP, MIP-877). Still a ToolForbidden, so the
    # MCP refusal counter sees the probe.
    if str(getattr(sprint, "team_id", "") or "") != ctx.team_id:
        raise ToolForbidden(not_found)
    is_member = ProjectMembers.objects.filter(
        project_id=sprint.project_id,
        attendee_id=ctx.user_id,
    ).exists()
    if not is_member:
        raise ToolForbidden(not_found)

    today = today_for_user_id(ctx.user_id)

    base_qs = TaskMaster.objects.filter(
        team_id=ctx.team_id,
        sprint_id=sprint_id,
        is_deleted=False,
        is_init_task=False,
    )
    # MIP-1413: with the flag, the tasks are read by their effective status (see `ancestor_rule`).
    rule = None
    if ancestor_rule.enabled():
        project_ids = set(base_qs.values_list("project_id", flat=True)) | set(
            MilestoneMaster.objects.filter(sprint_id=sprint_id, is_deleted=False).values_list(
                "project_id", flat=True
            )
        )
        rule = ancestor_rule.rule_for_projects(project_ids, ctx.team_id)
    counted_qs, sf = ancestor_rule.read_by_rule(base_qs, rule)

    annotations: dict[str, Any] = {
        "total_tasks": Count("task_id", distinct=True),
        "overdue_count": Count(
            "task_id",
            distinct=True,
            filter=Q(due_date__isnull=False, due_date__lt=today)
            & ~Q(**{f"{sf}__in": _CLOSED_STATUSES}),
        ),
    }
    for label in _STATUS_LABELS:
        annotations[f"_status_{label}"] = Count("task_id", distinct=True, filter=Q(**{sf: label}))
    for label in _PRIORITY_LABELS:
        annotations[f"_priority_{label}"] = Count(
            "task_id", distinct=True, filter=Q(priority=label)
        )
    # "Deleted" is gone, not finished (MIP-777): it is left out of the totals
    # and of "closed", and counted only in its own row of the breakdown.
    agg = counted_qs.exclude(status="Deleted").aggregate(**annotations)

    status_breakdown = {label: agg.get(f"_status_{label}", 0) or 0 for label in _STATUS_LABELS}
    status_breakdown["Deleted"] = (
        ancestor_rule.with_effective_status(base_qs, rule).filter(eff_status="Deleted").count()
        if rule is not None
        else base_qs.filter(status="Deleted").count()
    )
    priority_breakdown = {
        label: agg.get(f"_priority_{label}", 0) or 0 for label in _PRIORITY_LABELS
    }

    # Milestone rollups for milestones associated with this sprint. Each
    # milestone uses the same Q-union as `_serialize_milestone` so the
    # counts reconcile with the UI's milestone table.
    milestones: list[dict[str, Any]] = []
    for m in MilestoneMaster.objects.select_related("task").filter(
        sprint_id=sprint_id,
        is_deleted=False,
    ):
        m_qs, m_sf = ancestor_rule.read_by_rule(
            TaskMaster.objects.filter(milestone_work_q(m)).distinct(), rule
        )
        m_agg = m_qs.aggregate(
            tasks_total=Count("task_id", distinct=True),
            tasks_closed=Count("task_id", distinct=True, filter=Q(**{m_sf: "Closed"})),
        )
        if rule is not None and m.status == "Closed":
            m_agg["tasks_closed"] = m_agg["tasks_total"]  # a Closed milestone reads N/N
        milestones.append(
            {
                "milestone_id": m.milestone_id,
                "title": m.title,
                "status": m.status,
                "tasks_total": m_agg.get("tasks_total", 0) or 0,
                "tasks_closed": m_agg.get("tasks_closed", 0) or 0,
            }
        )

    # Signed days-until-end. Negative when the sprint has already ended
    # so the model can say "ran 3 days over" without separate plumbing.
    days_remaining: int | None = None
    if sprint.end_date is not None:
        days_remaining = (sprint.end_date - today).days

    # Derived from the dates and the same `today` as `days_remaining`, so the
    # two can't disagree; the stored column is a snapshot that goes stale (MIP-922).
    status = derive_status(sprint, today)

    total = agg.get("total_tasks", 0) or 0
    open_n = status_breakdown.get("Open", 0)
    wip_n = status_breakdown.get("WIP", 0)
    closed_n = status_breakdown.get("Closed", 0)
    overdue_n = agg.get("overdue_count", 0) or 0

    return {
        "sprint_id": sprint.sprint_id,
        "project_id": sprint.project_id,
        "project_name": sprint.project.project_name if sprint.project_id else None,
        "name": sprint.name,
        "sequence_number": sprint.sequence_number,
        "status": status,
        "start_date": sprint.start_date.isoformat() if sprint.start_date else None,
        "end_date": sprint.end_date.isoformat() if sprint.end_date else None,
        "days_remaining": days_remaining,
        "total_tasks": total,
        "status_breakdown": status_breakdown,
        "priority_breakdown": priority_breakdown,
        "overdue_count": overdue_n,
        "milestones": milestones,
        "__summary__": (
            f"Sprint '{sprint.name}' ({status}): {total} task(s) — "
            f"{open_n} open, {wip_n} WIP, {closed_n} closed, "
            f"{overdue_n} overdue"
            + (f", {days_remaining} day(s) left" if days_remaining is not None else "")
        ),
    }


GET_SPRINT_SUMMARY = Tool(
    name="get_sprint_summary",
    description=(
        "Per-sprint task rollup: total_tasks, status_breakdown (Open / "
        "WIP / Pending / Closed / Deleted), priority_breakdown (Minimal "
        "/ Low / Normal / High / Critical), overdue_count, "
        "`days_remaining` (signed: negative when the sprint has already "
        "ended), and a list of milestones in the sprint with their own "
        "tasks_total / tasks_closed. Use for 'how is the current sprint "
        "going?', 'what's overdue in sprint N?', or any sprint-progress "
        "question. Resolve a sprint name to an id with `list_sprints` "
        "first. Only accessible to project members."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "sprint_id": {
                "type": "INTEGER",
                "description": "Numeric sprint id to summarise.",
            },
        },
        "required": ["sprint_id"],
    },
    run=_run,
)
