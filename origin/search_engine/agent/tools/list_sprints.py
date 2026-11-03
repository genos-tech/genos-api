"""`list_sprints` tool — sprint list with per-row task rollups.

Returns each sprint with `tasks_total`, `tasks_closed`, and
`milestone_count` so the agent can answer "what sprints are in
project X?" or "how is sprint 4 going?" with a single call. Pair
with `get_sprint_summary(sprint_id)` for the deeper per-sprint
breakdown.

Status is the one `get_sprint_summary` and the sprint board show: derived from
the sprint's dates and the asking user's today (`services/sprint_status.py`),
not the stored `Sprint.status` column, which is a snapshot nothing rolls over
(MIP-922). The `status` filter, the "active first" order and the `limit` all
work on that derived status, in the query, so the current sprint can't be cut
off by a limit applied before the sort.

ACL contract:
  * Tenant guard: ctx.team_id.
  * Membership guard: only sprints in projects where ctx.user_id is a
    `ProjectMembers` row. Explicit `project_id` validated against the
    set.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.services.sprint_status import (
    SPRINT_STATUS_ACTIVE,
    SPRINT_STATUS_ARCHIVED,
    SPRINT_STATUS_COMPLETED,
    SPRINT_STATUS_UPCOMING,
    derive_status,
    derived_status_rank,
    status_q,
)
from origin.services.user_time import today_for_user_id

_MAX_LIMIT = 50
_VALID_STATUSES = {
    SPRINT_STATUS_UPCOMING,
    SPRINT_STATUS_ACTIVE,
    SPRINT_STATUS_COMPLETED,
    SPRINT_STATUS_ARCHIVED,
}
_CLOSED_STATUSES = ["Closed", "Deleted"]


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    member_project_ids = set(
        ProjectMembers.objects.filter(
            attendee_id=ctx.user_id,
            project__team_id=ctx.team_id,
            project__is_deleted=False,
        ).values_list("project_id", flat=True)
    )

    raw_project_id = args.get("project_id")
    scoped_project_ids: set[int] = member_project_ids
    if raw_project_id is not None:
        try:
            project_id = int(raw_project_id)
        except (TypeError, ValueError):
            raise ToolError(f"`project_id` must be an integer (got {raw_project_id!r}).")
        if project_id not in member_project_ids:
            raise ToolForbidden(
                f"Not authorized to list sprints in project {project_id}. "
                "You are not a member of that project."
            )
        scoped_project_ids = {project_id}

    raw_status = args.get("status")
    statuses: list[str] | None = None
    if raw_status is not None:
        if isinstance(raw_status, str):
            raw_status = [raw_status]
        invalid = set(raw_status) - _VALID_STATUSES
        if invalid:
            raise ToolError(
                f"Invalid sprint status(es): {sorted(invalid)}. "
                f"Must be one of {sorted(_VALID_STATUSES)}."
            )
        statuses = list(raw_status)

    try:
        limit = int(args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, _MAX_LIMIT))

    # The asking user's day, the same one `get_sprint_summary` uses.
    today = today_for_user_id(ctx.user_id)

    qs = Sprint.objects.select_related("project").filter(
        team_id=ctx.team_id,
        project_id__in=scoped_project_ids,
        is_deleted=False,
    )
    if statuses:
        # On the derived status, not the stored column. Combine with OR.
        status_filter = status_q(statuses[0], today)
        for status in statuses[1:]:
            status_filter |= status_q(status, today)
        qs = qs.filter(status_filter)

    # Show active first, then upcoming, then completed, then archived (most
    # recent first within each). The model's default ordering is by start_date;
    # override so callers get the "what's happening now" view by default. The
    # order is applied in the query, BEFORE the limit.
    sprints_raw = list(
        qs.annotate(status_rank=derived_status_rank(today)).order_by(
            "status_rank", "-start_date", "project_id", "sprint_id"
        )[:limit]
    )

    sprints: list[dict[str, Any]] = []
    for sp in sprints_raw:
        # "Deleted" is gone, not finished (MIP-777): neither in the total nor closed.
        agg = (
            TaskMaster.objects.filter(
                team_id=ctx.team_id,
                project_id=sp.project_id,
                sprint_id=sp.sprint_id,
                is_deleted=False,
                is_init_task=False,
            )
            .exclude(status="Deleted")
            .aggregate(
                tasks_total=Count("task_id", distinct=True),
                tasks_closed=Count("task_id", distinct=True, filter=Q(status="Closed")),
            )
        )
        milestone_count = MilestoneMaster.objects.filter(
            sprint_id=sp.sprint_id,
            is_deleted=False,
        ).count()
        sprints.append(
            {
                "sprint_id": sp.sprint_id,
                "project_id": sp.project_id,
                "project_name": sp.project.project_name if sp.project_id else None,
                "name": sp.name,
                "sequence_number": sp.sequence_number,
                "start_date": sp.start_date.isoformat() if sp.start_date else None,
                "end_date": sp.end_date.isoformat() if sp.end_date else None,
                "status": derive_status(sp, today),
                "tasks_total": agg.get("tasks_total", 0) or 0,
                "tasks_closed": agg.get("tasks_closed", 0) or 0,
                "milestone_count": milestone_count,
            }
        )

    if sprints:
        head = ", ".join(
            f"{s['name']} ({s['status']}, {s['tasks_closed']}/{s['tasks_total']})"
            for s in sprints[:3]
        )
        summary = f"Found {len(sprints)} sprint(s): {head}" + (" …" if len(sprints) > 3 else "")
    else:
        summary = "No sprints in scope."

    return {
        "project_id": int(raw_project_id) if raw_project_id is not None else None,
        "sprints": sprints,
        "__summary__": summary,
    }


LIST_SPRINTS = Tool(
    name="list_sprints",
    description=(
        "List sprints with per-row rollups: tasks_total, tasks_closed, "
        "milestone_count. Use for 'what sprints are in project X?', "
        "'show me the active sprint', or as a setup call before "
        "`get_sprint_summary` for the deeper breakdown. Sprint status "
        "values are `upcoming | active | completed | archived`. Results "
        "default to active sprints first, then upcoming, then completed. "
        "Scoped to sprints in projects the current user is a member of."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "project_id": {
                "type": "INTEGER",
                "description": (
                    "Restrict to one project: the id the user gives ('project 3' is 3), "
                    "or one from list_projects for a name. Omit to span all accessible projects."
                ),
            },
            "status": {
                "type": "ARRAY",
                "items": {
                    "type": "STRING",
                    "enum": ["upcoming", "active", "completed", "archived"],
                },
                "description": (
                    "Filter sprints by one or more status values. Useful "
                    "to ask 'show me active sprints across my projects'."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": f"Max sprints to return (1–{_MAX_LIMIT}). Default 20.",
            },
        },
        "required": [],
    },
    run=_run,
)
