"""The layout of each built-in dashboard (MIP-2420): what a milestone, a project and "my progress"
show, as the blocks `services/dashboard_page` draws.

Each answers its own question. A milestone: will it be done, and what is in the way. A project:
which part is stuck, and is the work moving. My progress: what did I finish, and what is next.

The audience rule is `ai_dashboard`'s (MIP-2354): every task NAMED here, and every task in the
open work's counts (workload, timeline, the attention cards), passed `visible_tasks` for every
reader. The totals (progress, status, closes) are the summary tools' own, as in the headline
numbers: a milestone or project page is refused when a reader is not on the project. A person is
named only while they are on the team.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any

from django.db.models import Case, IntegerField, Value, When

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.tools import ancestor_rule
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.task_closes import CLOSE_Q
from origin.services import dashboard_links
from origin.services.dashboard_page import (
    MAX_ATTENTION_ROWS,
    MAX_EXTRA_ATTENTION,
    MAX_EXTRA_TIMELINE,
    PACE_DAYS,
)
from origin.services.milestone_rollup import milestone_work_q
from origin.services.schedule_jobs.report_kit import plain

OPEN_STATUSES = ("Open", "WIP", "Blocked", "Pending")
# A project with more open work than this shows its soonest-due part.
MAX_OPEN = 2000
MAX_MILESTONES = 40
MAX_NAMES = 8
MAX_PEOPLE = 6


def row_of(display_id: Any, title: Any, task_id: Any, **extra: Any) -> dict[str, Any]:
    """One named task: its words and what it is (`link_page` adds the link)."""
    return {
        "text": f"{display_id} · {plain(title) or 'untitled'}",
        "ref": {"kind": dashboard_links.KIND_TASK, "id": task_id},
        **extra,
    }


def rule_of(project_ids: Iterable[int], team_id: Any):
    """The ancestor rule of these projects (None while its flag is off). It reads every task of
    them, so a page asks once and passes it on."""
    if not ancestor_rule.enabled():
        return None
    return ancestor_rule.rule_for_projects(set(project_ids), team_id)


def statuses_of(tasks, rule) -> dict[int, str]:
    """`{task_id: status}` of the queryset's tasks, each read as the summary tools read it (a task
    under a closed or deleted parent counts as that)."""
    qs, field = ancestor_rule.read_by_rule(tasks.distinct(), rule)
    return dict(qs.values_list("task_id", field))


class Open:
    """The open tasks of one page, already limited to what every reader may open."""

    def __init__(
        self, ctx: ToolContext, statuses: dict[int, str], readers: set[str], someone: str = ""
    ) -> None:
        """With `someone` (the page's word for a person who left the team) each row says who has
        the task, for the page's person filter; a personal page passes none."""
        from origin.services.schedule_jobs.ai_dashboard import visible_tasks  # noqa: PLC0415

        ids = [i for i, status in statuses.items() if status in OPEN_STATUSES]
        if len(ids) > MAX_OPEN:
            ids = list(
                TaskMaster.objects.filter(task_id__in=ids)
                .order_by("due_date", "task_id")
                .values_list("task_id", flat=True)[:MAX_OPEN]
            )
        self.tasks = visible_tasks(ctx, ids, readers)
        # True when an open task is left out because a reader may not open it.
        self.hidden = len(self.tasks) < len(ids)
        self.status = {i: statuses[i] for i in self.tasks}
        self.someone = someone
        ids_of_people = {t.assignee_id for t in self.tasks.values() if t.assignee_id}
        self.names = _names(ids_of_people, ctx.team_id)
        # blocked task -> the open tasks it waits for, those every reader may open
        deps = list(
            TaskDependency.objects.filter(
                blocked_task_id__in=list(self.tasks), blocker_task__is_deleted=False
            )
            .exclude(blocker_task__status__in=("Closed", "Deleted"))
            .order_by("id")
            .values_list("blocked_task_id", "blocker_task_id")
        )
        named = visible_tasks(ctx, [blocker for _, blocker in deps], readers)
        self.blockers: dict[int, list[Any]] = {}
        self.has_blocker: set[int] = set()
        for blocked, blocker in deps:
            self.has_blocker.add(blocked)
            if blocker in named:
                self.blockers.setdefault(blocked, []).append(named[blocker])

    def ordered(self) -> list[Any]:
        """Soonest due first; the undated last."""
        return sorted(self.tasks.values(), key=lambda t: (t.due_date or date.max, t.task_id))

    def row(self, task, **extra: Any) -> dict[str, Any]:
        due = task.due_date.isoformat() if task.due_date else None
        if self.someone:
            extra["owner"] = self.owner(task, self.someone)
        return row_of(
            task.display_id,
            task.title,
            task.task_id,
            due=due,
            status=self.status[task.task_id],
            **extra,
        )

    def owner(self, task, someone: str) -> str | None:
        """Who has the task: their name while they are on the team, else `someone`."""
        if task.assignee_id is None:
            return None
        return self.names.get(str(task.assignee_id)) or someone

    def blocked_ids(self) -> set[int]:
        return {
            i
            for i, status in self.status.items()
            if status == "Blocked" or (i in self.has_blocker and status != "WIP")
        }


def attention(
    work: Open, today: date, *, blocked: set[int] | None = None, mine: bool = False
) -> dict[str, Any]:
    """Cards for what is overdue, blocked and (not on a personal page) without an owner; the
    all-clear line when there is none."""
    blocked = work.blocked_ids() if blocked is None else blocked & set(work.tasks)
    ordered = work.ordered()
    groups = []
    for reason, tasks in (
        ("overdue", [t for t in ordered if t.due_date and t.due_date < today]),
        ("blocked", [t for t in ordered if t.task_id in blocked]),
        ("unowned", [] if mine else [t for t in ordered if t.assignee_id is None]),
    ):
        if not tasks:
            continue
        rows = []
        for task in tasks[: MAX_ATTENTION_ROWS + MAX_EXTRA_ATTENTION]:
            row = work.row(task)
            if reason == "blocked" and (waits := work.blockers.get(task.task_id)):
                first = waits[0]
                row["blocker"] = row_of(first.display_id, first.title, first.task_id)
            rows.append(row)
        group = {"reason": reason, "count": len(tasks), "rows": rows[:MAX_ATTENTION_ROWS]}
        if rows[MAX_ATTENTION_ROWS:]:
            # Held back until the reader asks (MIP-2452); `visible_tasks` passed them all.
            group["more_rows"] = rows[MAX_ATTENTION_ROWS:]
        groups.append(group)
    if not groups:
        return {"type": "clear", "mine": mine}
    return {"type": "attention", "mine": mine, "groups": groups}


def timeline(work: Open, *, goal: date | None = None, mine: bool = False) -> dict[str, Any]:
    ordered = work.ordered()
    dated = [t for t in ordered if t.due_date]
    held = dated[MAX_NAMES : MAX_NAMES + MAX_EXTRA_TIMELINE]
    return {
        "type": "timeline",
        "mine": mine,
        "goal": goal.isoformat() if goal else None,
        "rows": [work.row(t) for t in dated[:MAX_NAMES]],
        **({"more_rows": [work.row(t) for t in held]} if held else {}),
        "more": max(0, len(dated) - MAX_NAMES),
        "undated": len(ordered) - len(dated),
    }


def _names(user_ids: set[Any], team_id: Any) -> dict[str, str]:
    """`{user id: name}` for the ones still on the team."""
    on_team = {
        str(i)
        for i in TeamMembers.objects.filter(
            team_id=team_id, attendee_id__in=user_ids, is_deleted=False
        ).values_list("attendee_id", flat=True)
    }
    owner = TeamMaster.objects.filter(team_id=team_id).values_list("owner_id", flat=True).first()
    if owner is not None:
        on_team.add(str(owner))
    return {
        str(u.id): u.username
        for u in CustomUser.objects.filter(id__in=user_ids, is_active=True, is_deleted=False)
        if str(u.id) in on_team
    }


def workload(work: Open, someone: str) -> dict[str, Any]:
    """Open tasks per person, by status: the busiest first, the unassigned last."""
    loads: dict[str | None, Counter] = {}
    for task in work.tasks.values():
        loads.setdefault(work.owner(task, someone), Counter())[work.status[task.task_id]] += 1
    people = sorted(
        ((name, load) for name, load in loads.items() if name is not None),
        key=lambda p: (-sum(p[1].values()), p[0]),
    )
    shown = people[:MAX_PEOPLE]
    rest = people[MAX_PEOPLE:]
    rows = [{"name": name, **load} for name, load in shown]
    if None in loads:
        rows.append({"name": None, **loads[None]})
    return {
        "type": "workload",
        "rows": rows,
        "more_people": len(rest),
        "more_tasks": sum(sum(load.values()) for _, load in rest),
    }


def closed_tasks(task_ids, since: datetime, until: datetime | None = None) -> int:
    """How many DIFFERENT tasks of `task_ids` were closed in the window."""
    rows = TaskActivity.objects.filter(CLOSE_Q, task_id__in=task_ids, ts_created_at__gte=since)
    if until is not None:
        rows = rows.filter(ts_created_at__lt=until)
    return rows.values("task_id").distinct().count()


def meters(
    ctx: ToolContext, project_id: int, readers: set[str], now: datetime, since: datetime, rule
):
    """One progress bar per milestone of the project that still has work, had a close in the
    window, or is waiting for its first task."""
    # The unfinished ones first: a long-lived project's old milestones must not use up the room.
    milestones = list(
        MilestoneMaster.objects.filter(
            team_id=ctx.team_id, project_id=project_id, is_deleted=False
        ).order_by(
            Case(
                When(status__in=("Closed", "Deleted"), then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            ),
            "due_date",
            "milestone_id",
        )[:MAX_MILESTONES]
    )
    readable = dashboard_links.paths_for(
        ctx,
        [dashboard_links.Ref(dashboard_links.KIND_MILESTONE, m.milestone_id) for m in milestones],
        readers,
    )
    rows = []
    for milestone in milestones:
        ref = dashboard_links.Ref(dashboard_links.KIND_MILESTONE, milestone.milestone_id)
        if ref not in readable:
            continue
        work = TaskMaster.objects.filter(milestone_work_q(milestone), project_id=project_id)
        counts = Counter(statuses_of(work, rule).values())
        total = sum(n for status, n in counts.items() if status != "Deleted")
        ids = work.values("task_id")
        finished = total and counts["Closed"] == total
        if finished and not closed_tasks(ids, since):
            continue
        if not total and milestone.status in ("Closed", "Deleted"):
            continue
        rows.append(
            {
                "text": plain(milestone.title) or "untitled",
                "ref": dashboard_links.as_dict(ref),
                "done": counts["Closed"],
                "wip": counts["WIP"],
                "total": total,
                "due": milestone.due_date.isoformat() if milestone.due_date else None,
                "created": milestone.ts_created_at.date().isoformat(),
                "closed_recently": closed_tasks(ids, now - timedelta(days=PACE_DAYS)),
            }
        )
    return {"type": "meters", "rows": rows}


def _bucket(day: date, unit: str) -> date:
    if unit == "week":
        return day - timedelta(days=day.weekday())
    if unit == "month":
        return day.replace(day=1)
    return day


def pace(series: list[Any], period: str, today: date, days: int) -> dict[str, Any]:
    """The series as even buckets, the quiet ones included: days for a week, weeks for a month or
    a quarter, months for a year."""
    unit = {"week": "day", "month": "week", "quarter": "week", "year": "month"}[period]
    totals: dict[date, list[int]] = {}
    cursor = today - timedelta(days=days - 1)
    while cursor <= today:
        totals.setdefault(_bucket(cursor, unit), [0, 0])
        cursor += timedelta(days=1)
    for point in series:
        try:
            at = _bucket(date.fromisoformat(str(point["label"])), unit)
        except ValueError:
            continue
        # The queries bucket by UTC days and `today` is the owner's: what falls just outside
        # either end belongs to the end it is next to.
        at = min(max(at, min(totals)), max(totals))
        totals[at][0] += point["closed"]
        totals[at][1] += point["created"]
    return {
        "type": "trend",
        "unit": unit,
        "points": [
            {"label": at.isoformat(), "closed": closed, "created": created}
            for at, (closed, created) in sorted(totals.items())
        ],
    }


def _done(series: list[Any], closed_rows: list[Any], days: int) -> dict[str, Any]:
    """The latest closes under the same count the headline numbers and the chat notice give."""
    count = sum(p["closed"] for p in series) if closed_rows else 0
    return {"type": "done", "count": count, "days": days, "rows": closed_rows}


def milestone_layout(
    ctx: ToolContext,
    summary: dict[str, Any],
    milestone,
    readers: set[str],
    *,
    now: datetime,
    since: datetime,
    today: date,
    days: int,
    series: list[Any],
    closed_rows: list[Any],
    someone: str,
) -> tuple[list[dict[str, Any]], bool]:
    """The blocks, and whether an open task is left out of them."""
    work_q = TaskMaster.objects.filter(milestone_work_q(milestone))
    ids = work_q.values("task_id")
    counts = dict(summary.get("status_breakdown") or {})
    counts.pop("Deleted", None)
    hero = {
        "type": "hero",
        "done": counts.get("Closed", 0),
        "total": summary.get("total_tasks") or 0,
        "due": milestone.due_date.isoformat() if milestone.due_date else None,
        "created": milestone.ts_created_at.date().isoformat(),
        "closed_recently": closed_tasks(ids, now - timedelta(days=PACE_DAYS)),
    }
    if not hero["total"]:
        return [hero], False
    rule = rule_of(set(work_q.values_list("project_id", flat=True)), ctx.team_id)
    work = Open(ctx, statuses_of(work_q, rule), readers, someone)
    blocks: list[dict[str, Any]] = [hero, {"type": "status", "heading": "", "counts": counts}]
    if work.tasks:
        # The summary also knows a task that waits through its parent.
        waiting = {r["task_id"] for r in summary.get("blocked") or []}
        blocks.append(attention(work, today, blocked=work.blocked_ids() | waiting))
        blocks.append(timeline(work, goal=milestone.due_date))
        blocks.append(workload(work, someone))
    blocks.append(_done(series, closed_rows, days))
    return blocks, work.hidden


def project_layout(
    ctx: ToolContext,
    summary: dict[str, Any],
    work_ids,
    readers: set[str],
    *,
    now: datetime,
    since: datetime,
    today: date,
    days: int,
    period: str,
    series: list[Any],
    closed_rows: list[Any],
    someone: str,
) -> tuple[list[dict[str, Any]], bool]:
    project_id = summary["project_id"]
    tasks = TaskMaster.objects.filter(task_id__in=work_ids)
    rule = rule_of([project_id], ctx.team_id)
    work = Open(ctx, statuses_of(tasks, rule), readers, someone)
    counts = dict(summary.get("status_breakdown") or {})
    overdue = summary.get("overdue_count") or 0
    blocked = len(work.blocked_ids())
    numbers = {
        "type": "numbers",
        "items": [
            {"key": "num_tasks", "value": str(summary.get("total_tasks") or 0)},
            {"key": "num_closed", "days": days, "value": str(sum(p["closed"] for p in series))},
            {"key": "num_overdue", "value": str(overdue), "alert": bool(overdue)},
            {"key": "num_blocked", "value": str(blocked), "alert": bool(blocked)},
        ],
    }
    blocks: list[dict[str, Any]] = [
        numbers,
        meters(ctx, project_id, readers, now, since, rule),
        {"type": "status", "counts": counts, "wide": True},
        pace(series, period, today, days),
    ]
    if work.tasks:
        blocks.append(workload(work, someone))
        blocks.append(attention(work, today))
    blocks.append(_done(series, closed_rows, days))
    return blocks, work.hidden


def mine_layout(
    ctx: ToolContext,
    summary: dict[str, Any],
    projects: Iterable[int],
    readers: set[str],
    *,
    since: datetime,
    today: date,
    days: int,
    period: str,
    series: list[Any],
    closed_rows: list[Any],
) -> tuple[list[dict[str, Any]], bool]:
    """`projects` are the ones this page counts: mine, or those every reader is on."""
    projects = set(projects)
    closes = TaskActivity.objects.filter(
        CLOSE_Q,
        actor_id=ctx.user_id,
        task__team_id=ctx.team_id,
        task__project_id__in=projects,
        task__is_deleted=False,
        task__is_init_task=False,
    )
    previous = closes.filter(
        ts_created_at__gte=since - timedelta(days=days), ts_created_at__lt=since
    ).count()
    trend = pace(series, period, today, days)
    headline = {
        "type": "headline",
        "value": summary["closed_total"],
        "previous": previous,
        "days": days,
        "unit": trend["unit"],
        "points": [{"label": p["label"], "closed": p["closed"]} for p in trend["points"]],
    }
    mine = TaskMaster.objects.filter(
        team_id=ctx.team_id,
        project_id__in=projects,
        assignee_id=ctx.user_id,
        is_deleted=False,
        is_init_task=False,
        is_milestone=False,
    )
    work = Open(ctx, statuses_of(mine, rule_of(projects, ctx.team_id)), readers)
    blocks: list[dict[str, Any]] = [headline]
    if work.tasks:
        blocks.append(attention(work, today, mine=True))
        blocks.append(timeline(work, mine=True))
    # The headline above says how many; this names only the latest.
    blocks.append({"type": "done", "count": len(closed_rows[:5]), "rows": closed_rows[:5]})
    return blocks, work.hidden


def member_projects(ctx: ToolContext) -> set[int]:
    return set(
        ProjectMembers.objects.filter(
            attendee_id=ctx.user_id,
            project__team_id=ctx.team_id,
            project__is_deleted=False,
        ).values_list("project_id", flat=True)
    )
