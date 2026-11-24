"""Job `team_activity` (MIP-2061): what the team's projects are doing, who joined, who delivered.

"Monday 09:00: team activity", posted into a team chat. Data only: no model, no cost, nothing
written. It reads the team's own rows, as the schedule's owner, and names only what EVERY reader
may read:

  * Projects: only projects the owner is on AND every other reader of the target chat is on
    (`AUDIENCE_KEY`). A project somebody in the chat is not on is not named, and its tasks are
    not counted anywhere in the report. In the owner's Inbox the audience is the owner alone.
  * New on the team: people who joined the team in the window (live team members, no system
    accounts). The audience rule (`schedule_audience`) lets this job into a chat only when every
    reader is on the team, so a guest or an external member never reads a roster.
  * Top contributors: who closed the most tasks in the window, counted over the projects named
    above and no others.

The run itself refuses an owner or a reader who is not on the team, so a reader added to the chat
since the schedule was saved cannot receive the roster (the delivery asks the rule again too).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Count
from django.utils import timezone

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.task_closes import CLOSE_Q, CREATED_Q
from origin.services import schedule_audience
from origin.services.schedule_jobs import AUDIENCE_KEY, Job, Report, ReportSection, register_job
from origin.services.schedule_jobs.report_kit import (
    AI_SUMMARY_PARAM,
    SUMMARY_EFFORT_DEFAULT,
    Links,
    add_narrative,
    ai_sentences_on,
    counted,
    item,
    more,
    span,
)

DEFAULT_HOURS = 168
MAX_LIST = 8
MAX_TOP = 5


def _public(message: str):
    from origin.services.schedules import ScheduleRunError  # noqa: PLC0415 (import cycle)

    return ScheduleRunError(message)


def _audience(ctx: ToolContext, params: dict[str, Any]) -> set[str]:
    return {str(ctx.user_id), *(str(i) for i in params.get(AUDIENCE_KEY) or [])}


def _shared_projects(ctx: ToolContext, audience: set[str]) -> list[ProjectMaster]:
    """The team's live projects every member of `audience` is on."""
    on = (
        ProjectMembers.objects.filter(
            project__team_id=ctx.team_id,
            project__is_deleted=False,
            attendee_id__in=audience,
        )
        .values("project_id")
        .annotate(n=Count("attendee_id", distinct=True))
        .filter(n=len(audience))
        .values("project_id")
    )
    return list(
        ProjectMaster.objects.filter(project_id__in=on, team_id=ctx.team_id, is_deleted=False)
        .only("project_id", "project_name")
        .order_by("project_name", "project_id")
    )


def _report(ctx: ToolContext, params: dict[str, Any]) -> Report:
    hours = int(params.get("hours", DEFAULT_HOURS))
    audience = _audience(ctx, params)
    on_team = schedule_audience.team_member_ids(ctx.team_id)
    if not audience <= on_team:
        # A guest or an external member among the readers (or an owner who left the team).
        raise _public("This report is for people on the team only.")
    since = timezone.now() - timedelta(hours=hours)

    projects = _shared_projects(ctx, audience)
    by_id = {p.project_id: p for p in projects}
    created: dict[int, int] = {}
    closed: dict[int, int] = {}
    closers: dict[str, int] = {}
    if by_id:
        base = TaskActivity.objects.filter(
            project_id__in=by_id,
            ts_created_at__gte=since,
            task__is_deleted=False,
            task__is_init_task=False,
            task__is_milestone=False,
        )
        created = dict(
            base.filter(CREATED_Q)
            .values_list("project_id")
            .annotate(n=Count("activity_id"))
            .order_by()
        )
        close_rows = base.filter(CLOSE_Q)
        closed = dict(
            close_rows.values_list("project_id").annotate(n=Count("activity_id")).order_by()
        )
        closers = {
            str(actor): n
            for actor, n in close_rows.filter(
                actor__isnull=False, actor__is_deleted=False, actor__is_system_user=False
            )
            .values_list("actor_id")
            .annotate(n=Count("activity_id"))
            .order_by("-n", "actor_id")[: MAX_TOP * 2]
        }
        # A closer who left the team is not named in a team report.
        closers = {a: n for a, n in closers.items() if a in on_team}

    active = sorted(
        (p for p in projects if created.get(p.project_id) or closed.get(p.project_id)),
        key=lambda p: (
            -(created.get(p.project_id, 0) + closed.get(p.project_id, 0)),
            p.project_name.casefold(),
            p.project_id,
        ),
    )
    sections: list[ReportSection] = []

    rows = []
    for p in active[:MAX_LIST]:
        said = ", ".join(
            f"{n} {label}"
            for n, label in (
                (closed.get(p.project_id), "closed"),
                (created.get(p.project_id), "new"),
            )
            if n
        )
        rows.append(item(f"{p.project_name}: {said}", Links.project(p.project_id), "project"))
    if len(active) > len(rows):
        rows.append(more(len(active) - len(rows), "projects"))
    # Projects with nothing new or closed are not listed: a quiet project is not news (MIP-1417).
    if rows:
        sections.append({"heading": "Projects", "key": "projects", "items": rows})

    joined = list(
        TeamMembers.objects.filter(
            team_id=ctx.team_id,
            is_deleted=False,
            attendee__is_deleted=False,
            attendee__is_system_user=False,
            ts_joined_at__gte=since,
        )
        .select_related("attendee")
        .order_by("-ts_joined_at", "attendee_id")[: MAX_LIST + 1]
    )
    if joined:
        rows = [item(m.attendee.username or "A new member") for m in joined[:MAX_LIST]]
        if len(joined) > MAX_LIST:
            rows.append(more(1, "people"))
        sections.append({"heading": "New on the team", "key": "new_members", "items": rows})

    if closers:
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        names = {
            str(i): u
            for i, u in CustomUser.objects.filter(id__in=list(closers)).values_list(
                "id", "username"
            )
        }
        top = [
            item(f"{names.get(a) or 'Someone'}: {n} closed")
            for a, n in sorted(closers.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_TOP]
        ]
        sections.append({"heading": "Top contributors", "key": "top", "items": top})

    total_closed = sum(closed.values())
    total_created = sum(created.values())
    bits = []
    if total_closed:
        bits.append(f"{counted(total_closed, 'task')} closed")
    if total_created:
        bits.append(f"{counted(total_created, 'new task')}")
    if joined:
        bits.append(f"{len(joined)} joined")
    return {
        "title": "Team activity",
        "summary": (
            f"{', '.join(bits)} in the last {span(hours)}."
            if bits
            else f"Nothing to report for the last {span(hours)}."
        ),
        "sections": sections,
        # No headline chip for the closed count: no section carries its name, so the chip would
        # be a bare number; the summary line says it (MIP-1417).
        "stats": [
            {"key": key, "count": n}
            for key, n in (("projects", len(active)), ("new_members", len(joined)))
            if n
        ],
        "quiet": not sections,
    }


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    """The report, with a written summary on top when the member turned the AI part on."""
    return add_narrative(ctx, params, _report(ctx, params), effort_default=SUMMARY_EFFORT_DEFAULT)


register_job(
    Job(
        key="team_activity",
        title="Team activity",
        description=(
            "What the team's projects did lately (tasks closed and created), who joined the "
            "team, and who closed the most tasks. In a chat it names only the projects everyone "
            "in that chat is on, and only to team members."
        ),
        run=run,
        share_team=True,
        params_schema={
            "properties": {
                "hours": {"type": "integer", "minimum": 1, "maximum": 720},
                "ai_sentences": AI_SUMMARY_PARAM,
            }
        },
        uses_ai=True,
        ai_when=ai_sentences_on,
        effort_default=SUMMARY_EFFORT_DEFAULT,
        cost_basis="narrative",
    )
)
