"""Job `project_updates` (MIP-628): what moved, what slipped and what is blocked in one project.

"17:00 every Friday: project X updates". It is `milestone_updates` for a whole project, and it
is read the same way: through the tool the agent uses (`get_project_summary`), so the access
rule is that tool's: the member must be in the project and its team, and a project they cannot
open fails the run in words rather than reading as an empty project. No model, no cost,
nothing written.

What each section means (the window is the last `hours`, default a week):

  * Done: tasks that are Closed now and whose last status change is in the window.
  * Moved: tasks whose last status change is in the window and that are neither Closed nor
    Blocked now (started, paused, reopened, handed to review).
  * Slipped: open tasks that went overdue in the window, or whose due date somebody pushed
    later in the window. Not every overdue task every week: a task that has been late for a
    month is not news (the total is in "Where it stands").
  * Blocked: tasks that are Blocked now, with what they wait on when it is a task.

Every task NAMED is one the member may open (`LabelAccess`), and a list's "and N more" counts
only what they may open. "Where it stands" is the agent's own `get_project_summary` count, the
same numbers the agent gives for the project. The milestones' own
backing rows are left out: a milestone is not a task of the work.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.utils import timezone

from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools import ancestor_rule
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.get_project_summary import GET_PROJECT_SUMMARY
from origin.search_engine.agent.tools.project_gone import live_tasks_q
from origin.services import briefing
from origin.services.schedule_jobs import SHARED_KEY, Job, Report, ReportSection, register_job
from origin.services.schedule_jobs.report_kit import (
    Links,
    arrange,
    clip,
    in_order,
    item,
    more,
)
from origin.services.task_blocking import CLOSED_STATUSES
from origin.services.user_time import zone_for_user_id

DEFAULT_HOURS = 168
MAX_LIST = 8
_POOL = 400


def _public(message: str):
    from origin.services.schedules import ScheduleRunError  # noqa: PLC0415 (import cycle)

    return ScheduleRunError(message)


def _day(value: Any) -> date | None:
    """A due date as the activity log keeps it ("2026-11-10"), or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _span(hours: int) -> str:
    days, rest = divmod(hours, 24)
    if days and not rest:
        return "1 day" if days == 1 else f"{days} days"
    return "1 hour" if hours == 1 else f"{hours} hours"


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    project_id = params.get("project_id")
    if project_id is None:
        raise _public("This report needs a project: choose one in the schedule.")
    try:
        summary = GET_PROJECT_SUMMARY.run({"project_id": project_id}, ctx)
    except ToolError as exc:
        # The tool's sentences name no content ("not found", "not a member of that project").
        raise _public(str(exc)[:200]) from None
    project_id = summary["project_id"]
    hours = int(params.get("hours", DEFAULT_HOURS))
    now = timezone.now()
    since = now - timedelta(hours=hours)
    zone = zone_for_user_id(ctx.user_id)
    today = now.astimezone(zone).date()
    since_day = since.astimezone(zone).date()

    work = TaskMaster.objects.filter(
        live_tasks_q(),
        team_id=ctx.team_id,
        project_id=project_id,
        is_deleted=False,
        is_init_task=False,
        is_milestone=False,
    ).exclude(status="Deleted")
    # A task under a Closed parent reads as Closed (the flag the agent's counts follow): it is
    # not open work, so it is neither late nor blocked.
    rule = (
        ancestor_rule.rule_for_projects([project_id], ctx.team_id)
        if ancestor_rule.enabled()
        else None
    )
    not_open = rule.not_open if rule is not None else frozenset()

    def in_window(action: str):
        # Newest first, so the first row seen for a task is its latest. One budget per kind of
        # row: a burst of due-date edits must not push the closes out of the pool.
        return list(
            TaskActivity.objects.filter(
                task_id__in=work.values("task_id"),
                action_type=action,
                ts_created_at__gte=since,
            ).order_by("-ts_created_at")[:_POOL]
        )

    last_status: dict[int, TaskActivity] = {}
    for a in in_window(TaskActivityActionType.STATUS):
        last_status.setdefault(a.task_id, a)
    pushed: dict[int, TaskActivity] = {}
    edited_due: set[int] = set()
    for a in in_window(TaskActivityActionType.DUE_DATE):
        edited_due.add(a.task_id)
        old, new = _day(a.old_value), _day(a.new_value)
        if a.task_id not in pushed and old is not None and new is not None and new > old:
            pushed[a.task_id] = a
    # Went overdue by the calendar. A task whose due date somebody edited in the window is
    # judged by that edit alone (pushed later = slipped), not by landing in the past.
    went_late = dict(
        work.exclude(status__in=CLOSED_STATUSES)
        .exclude(task_id__in=not_open)
        .exclude(task_id__in=edited_due)
        .filter(due_date__lt=today, due_date__gte=since_day)
        .order_by("due_date", "task_id")
        .values_list("task_id", "due_date")[:_POOL]
    )
    blocked_ids = list(
        work.filter(status="Blocked")
        .exclude(task_id__in=not_open)
        .order_by("due_date", "task_id")
        .values_list("task_id", flat=True)[:_POOL]
    )

    access = LabelAccess(ctx)
    readable = access.tasks({*last_status, *pushed, *went_late, *blocked_ids})
    names = access.usernames(
        {str(a.actor_id) for a in [*last_status.values(), *pushed.values()] if a.actor_id}
    )

    def who(row: TaskActivity) -> str:
        if row.actor_id:
            return names.get(str(row.actor_id), "someone")
        return "system"

    done_rows: list[tuple[Any, TaskActivity]] = []
    moved_rows: list[tuple[Any, TaskActivity]] = []
    for task_id, row in last_status.items():
        task = readable.get(task_id)
        if task is None:
            continue
        if task.status == "Closed":
            done_rows.append((task, row))
        elif task.status != "Blocked" and task_id not in not_open:
            moved_rows.append((task, row))
    for rows in (done_rows, moved_rows):
        rows.sort(key=lambda pair: pair[1].ts_created_at, reverse=True)

    slipped: dict[int, str] = {}
    for task_id, row in pushed.items():
        task = readable.get(task_id)
        if task is not None and task.status not in CLOSED_STATUSES and task_id not in not_open:
            slipped[task_id] = (
                f"due date moved {_day(row.old_value)} → {_day(row.new_value)} by {who(row)}"
            )
    for task_id in went_late:
        if task_id in readable:
            slipped.setdefault(task_id, "went overdue")

    blocked = [t for t in (readable.get(i) for i in blocked_ids) if t is not None]
    # What each blocked task waits on, when it waits on tasks (one query; a blocker the member
    # may not open is not named).
    waits: dict[int, list[int]] = {}
    shown_blocked = blocked[:MAX_LIST]
    if shown_blocked:
        for blocked_id, blocker_id in (
            TaskDependency.objects.filter(
                blocked_task_id__in=[t.task_id for t in shown_blocked],
                blocker_task__is_deleted=False,
            )
            .exclude(blocker_task__status__in=CLOSED_STATUSES)
            .values_list("blocked_task_id", "blocker_task_id")
        ):
            waits.setdefault(blocked_id, []).append(blocker_id)
    blockers = access.tasks({b for ids in waits.values() for b in ids})
    if params.get(SHARED_KEY):
        # A blocker can sit in another project; in a chat of this project's people it is not
        # named (MIP-1420).
        blockers = {i: t for i, t in blockers.items() if t.project_id == project_id}

    ranked = sorted(slipped, key=lambda i: (readable[i].due_date or date.max, readable[i].task_id))
    links = Links(ctx)
    links.preload(
        tasks=[
            *(t.task_id for t, _ in done_rows[:MAX_LIST]),
            *(t.task_id for t, _ in moved_rows[:MAX_LIST]),
            *ranked[:MAX_LIST],
            *(t.task_id for t in shown_blocked),
        ]
    )

    def line(task: Any, said: str):
        return links.task_item(
            f"{task.display_id} · {task.title or 'untitled'}: {said}", task.task_id, said
        )

    def moved_line(task: Any, row: TaskActivity):
        # In a chat other people read nothing is "you": the owner is one reader of several.
        viewer = "" if params.get(SHARED_KEY) else ctx.user_id
        said = f"{briefing.activity_event(row, viewer)['summary']} by {who(row)}"
        return line(task, said)

    def capped(items: list, total: int) -> list:
        if total > len(items):
            items.append(more(total - len(items), "tasks"))
        return items

    sections: list[ReportSection] = []
    breakdown = summary["status_breakdown"]
    stands = [
        item(
            f"{summary['total_tasks']} tasks: "
            + ", ".join(
                f"{breakdown.get(s, 0)} {s.lower()}"
                for s in ("Open", "WIP", "Blocked", "Pending", "Closed")
            )
        )
    ]
    if summary.get("overdue_count"):
        stands.append(item(f"{summary['overdue_count']} overdue"))
    sections.append({"heading": "Where it stands", "key": "where_it_stands", "items": stands})

    if done_rows:
        sections.append(
            {
                "heading": "Done",
                "key": "closed",
                "items": capped(
                    in_order([moved_line(t, r) for t, r in done_rows[:MAX_LIST]], today),
                    len(done_rows),
                ),
            }
        )
    if moved_rows:
        sections.append(
            {
                "heading": "Moved",
                "key": "changed",
                "items": capped(
                    in_order([moved_line(t, r) for t, r in moved_rows[:MAX_LIST]], today),
                    len(moved_rows),
                ),
            }
        )
    if slipped:
        sections.append(
            {
                "heading": "Slipped",
                "key": "overdue",
                "items": capped(
                    arrange([line(readable[i], slipped[i]) for i in ranked[:MAX_LIST]], today),
                    len(ranked),
                ),
            }
        )
    if blocked:
        rows = []
        for task in shown_blocked:
            names_of = [
                blockers[b].display_id for b in waits.get(task.task_id, []) if b in blockers
            ]
            said = (
                "newly blocked"
                if task.task_id in last_status and last_status[task.task_id].new_value == "Blocked"
                else "blocked"
            ) + (f", waiting on {', '.join(sorted(names_of)[:3])}" if names_of else "")
            rows.append(line(task, said))
        sections.append(
            {
                "heading": "Blocked",
                "key": "blocked",
                "items": capped(arrange(rows, today), len(blocked)),
            }
        )

    counts = (
        ("done", len(done_rows)),
        ("moved", len(moved_rows)),
        ("slipped", len(slipped)),
        ("blocked", len(blocked)),
    )
    said_counts = [f"{n} {label}" for label, n in counts if n]
    line_text = (
        f"{', '.join(said_counts)} in the last {_span(hours)}."
        if said_counts
        else f"Nothing moved in the last {_span(hours)}."
    )
    return {
        "title": f"Project: {summary.get('project_name') or 'Project'}",
        "summary": clip(line_text),
        "sections": sections,
        "stats": [
            {"key": key, "count": n}
            for key, n in (
                ("closed", len(done_rows)),
                ("changed", len(moved_rows)),
                ("overdue", len(slipped)),
                ("blocked", len(blocked)),
            )
            if n
        ],
    }


def _share_project(params: dict[str, Any], team_id: str) -> int | None:
    """The project this report is about: a chat of its people may read it (MIP-1420)."""
    project_id = params.get("project_id")
    return project_id if isinstance(project_id, int) and not isinstance(project_id, bool) else None


register_job(
    Job(
        key="project_updates",
        title="Project updates",
        description=(
            "What moved, what slipped and what is blocked in one project lately: tasks done "
            "and moved in the last days, tasks that went overdue or had their due date pushed "
            "later, and tasks that are blocked."
        ),
        agent_note="`project_id` is the project's numeric id, from `list_projects`.",
        run=run,
        share_project=_share_project,
        params_schema={
            "properties": {
                "project_id": {"type": "integer", "minimum": 1},
                "hours": {"type": "integer", "minimum": 1, "maximum": 720},
            },
            "required": ["project_id"],
        },
    )
)
