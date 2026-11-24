"""Job `ai_dashboard` (MIP-2353): one living HTML page per dashboard, kept up to date by a schedule.

"Every Monday 08:00: the dashboard of milestone X for the last month." The job reads numbers the
same way the other report jobs do (the agent's own summary tools and the member's access rules) and
returns a normal `Report` plus a `dashboard` (`services/dashboard_page`). The delivery step
(`services/schedule_dashboard_note.deliver_note`) renders the page, saves it as ONE HTML note and
replaces that note on every later run (decision D2), then the report is filed or posted as any
other with a link to the note.

* `kind` is `milestone`, `project` or `mine` (my progress). `period` is the data window
  (week 7 days, month 30, quarter 91, year 365 days): a window on the existing frequencies, not a
  new recurrence (D4).
* The plain page needs no model. `ai_sentences` (off by default, an opt-in as everywhere) adds
  ONE short summary written by the model; the page is never model-written (D3).
* What the page names is limited to what EVERY reader of the saved note may open (MIP-2354): the
  runner hands the job the note's readers (`AUDIENCE_KEY`: the place's people, as the note sits
  now). A project or milestone dashboard is refused when a reader is off the project; "my
  progress" counts only projects every reader is on; every task title goes through `visible_tasks`.
  Checked when the schedule is saved and before every run. A refusal names nobody.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from django.db.models import Case, Count, When
from django.db.models.functions import TruncDay, TruncWeek
from django.utils import timezone

from origin.models.common.schedule_models import MemberSchedule
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.get_milestone_summary import GET_MILESTONE_SUMMARY
from origin.search_engine.agent.tools.get_my_throughput import GET_MY_THROUGHPUT
from origin.search_engine.agent.tools.get_project_summary import GET_PROJECT_SUMMARY
from origin.search_engine.agent.tools.project_gone import live_tasks_q
from origin.search_engine.agent.tools.task_closes import CLOSE_Q, CREATED_Q, THROUGHPUT_Q
from origin.services import schedule_audience
from origin.services.dashboard_page import Dashboard, DashPoint
from origin.services.milestone_rollup import milestone_work_q
from origin.services.report_ai.llm import DASHBOARD_PURPOSE
from origin.services.schedule_jobs import AUDIENCE_KEY, Job, Report, register_job
from origin.services.schedule_jobs.report_kit import (
    AI_SUMMARY_PARAM,
    SUMMARY_EFFORT_DEFAULT,
    add_narrative,
    ai_sentences_on,
    clip,
    item,
    plain,
)
from origin.services.user_time import zone_for_user_id

KINDS = ("milestone", "project", "mine")
PERIOD_DAYS = {"week": 7, "month": 30, "quarter": 91, "year": 365}
MAX_FOCUS_CHARS = 200
MAX_LIST = 10
# A member keeps at most this many dashboard schedules (MIP-2359). Each run saves a note, and an AI
# one spends credits; a schedule runs at most once a day (the recurrence has nothing finer).
MAX_DASHBOARD_SCHEDULES = 10
_FINGERPRINT_KEYS = ("title", "kind", "stats", "breakdown", "series", "lists", "focus", "note")
# A week or a month is read per day, a quarter or a year per week.
_DAILY = ("week", "month")


def _public(message: str):
    from origin.services.schedules import ScheduleRunError  # noqa: PLC0415 (import cycle)

    return ScheduleRunError(message)


def audience(ctx: ToolContext, params: dict[str, Any]) -> set[str]:
    """Everyone who reads the saved page: the owner and the readers the runner found
    (`AUDIENCE_KEY`). Without them (a direct call) the owner alone."""
    return {str(ctx.user_id), *(str(i) for i in params.get(AUDIENCE_KEY) or [])}


def visible_tasks(
    ctx: ToolContext, task_ids: Iterable[int], readers: set[str] | None = None
) -> dict[int, Any]:
    """The tasks of `task_ids` the page may NAME: the ones EVERY reader of the note may open
    (the owner alone when `readers` is not given). Every task title, count of listed rows and
    link on the page goes through here, nowhere else."""
    ids = list(task_ids)
    found = LabelAccess(ctx).tasks(ids)
    for reader in sorted((readers or set()) - {str(ctx.user_id)}):
        if not found:
            break
        theirs = LabelAccess(ToolContext(team_id=ctx.team_id, user_id=reader)).tasks(list(found))
        found = {i: t for i, t in found.items() if i in theirs}
    return found


def audience_problem(params: dict[str, Any], readers: set[str], team_id: Any) -> str | None:
    """Why a dashboard with these `params` may not be saved where `readers` read it; None when
    it may. The sentences name nobody."""
    if len(readers) > schedule_audience.MAX_NOTE_READERS:
        return schedule_audience.NOTE_TOO_MANY
    if len(readers) > 1 and params.get("kind") in ("project", "milestone"):
        project_id = _share_project(params, str(team_id))
        if project_id is not None and schedule_audience.outside_project(readers, project_id):
            return NOT_EVERYONE_ON_PROJECT
    return None


NOT_EVERYONE_ON_PROJECT = (
    "Not everyone who can read the place you chose for this page is on that project, so not "
    "everyone could open what it shows. Save it in a place only the project's people read."
)


def _period_name(period: str) -> str:
    return {"week": "week", "month": "month", "quarter": "quarter", "year": "year"}[period]


def _series(task_ids, since, period: str) -> list[DashPoint]:
    """Closed and created tasks per day (week, month) or per week (quarter, year)."""
    trunc = TruncDay if period in _DAILY else TruncWeek
    rows = (
        TaskActivity.objects.filter(THROUGHPUT_Q, task_id__in=task_ids, ts_created_at__gte=since)
        .annotate(bucket_start=trunc("ts_created_at"))
        .values("bucket_start")
        .annotate(
            created=Count(Case(When(CREATED_Q, then=1))),
            closed=Count(Case(When(CLOSE_Q, then=1))),
        )
        .order_by("bucket_start")
    )
    return [
        {
            "label": r["bucket_start"].date().isoformat(),
            "closed": r["closed"],
            "created": r["created"],
        }
        for r in rows
        if r["bucket_start"] is not None
    ]


def _recently_closed(
    ctx: ToolContext, task_ids, since, readers: set[str] | None = None
) -> list[str]:
    rows = list(
        TaskActivity.objects.filter(CLOSE_Q, task_id__in=task_ids, ts_created_at__gte=since)
        .order_by("-ts_created_at")
        .values_list("task_id", flat=True)[: MAX_LIST * 3]
    )
    seen: list[int] = []
    for task_id in rows:
        if task_id not in seen:
            seen.append(task_id)
    readable = visible_tasks(ctx, seen, readers)
    shown = [readable[i] for i in seen if i in readable][:MAX_LIST]
    return [f"{t.display_id} · {plain(t.title) or 'untitled'}" for t in shown]


def _focus(params: dict[str, Any]) -> str:
    return " ".join(str(params.get("focus") or "").split())[:MAX_FOCUS_CHARS]


def _stand(summary: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    breakdown = [
        {"label": s, "count": n} for s, n in (summary.get("status_breakdown") or {}).items() if n
    ]
    total = summary.get("total_tasks") or 0
    closed = (summary.get("status_breakdown") or {}).get("Closed", 0)
    stats = [{"label": "tasks", "value": str(total)}]
    if total:
        stats.append({"label": "closed", "value": f"{round(100 * closed / total)}%"})
    stats.append({"label": "overdue", "value": str(summary.get("overdue_count") or 0)})
    return stats, breakdown


def _mine(ctx: ToolContext, days: int, bucket: str, readers: set[str]) -> dict[str, Any]:
    """ "My progress". Alone: the agent tool's own numbers. With other readers of the page: the same
    numbers over the projects EVERY reader is on, so no other project's work is counted or named."""
    if readers <= {str(ctx.user_id)}:
        return GET_MY_THROUGHPUT.run({"days": days, "bucket": bucket}, ctx)
    projects = schedule_audience.common_project_ids(readers, ctx.team_id)
    trunc = TruncDay if bucket == "day" else TruncWeek
    base = TaskActivity.objects.filter(
        THROUGHPUT_Q,
        ts_created_at__gte=timezone.now() - timedelta(days=days),
        actor_id=ctx.user_id,
        task__team_id=ctx.team_id,
        task__project_id__in=projects,
        task__is_deleted=False,
        task__is_init_task=False,
    )
    created, closed = Count(Case(When(CREATED_Q, then=1))), Count(Case(When(CLOSE_Q, then=1)))
    totals = base.aggregate(created=created, closed=closed)
    buckets = [
        {
            "bucket_start": r["b"].date().isoformat() if r["b"] is not None else None,
            "created": r["created"] or 0,
            "closed": r["closed"] or 0,
        }
        for r in base.annotate(b=trunc("ts_created_at"))
        .values("b")
        .annotate(created=created, closed=closed)
        .order_by("b")
    ]
    ids: list[int] = []
    for task_id in (
        base.filter(CLOSE_Q)
        .order_by("-ts_created_at")
        .values_list("task_id", flat=True)[: MAX_LIST * 3]
    ):
        if task_id not in ids:
            ids.append(task_id)
    readable = visible_tasks(ctx, ids, readers)
    return {
        "created_total": totals["created"] or 0,
        "closed_total": totals["closed"] or 0,
        "net": (totals["created"] or 0) - (totals["closed"] or 0),
        "buckets": buckets,
        "recently_closed": [
            {"display_id": readable[i].display_id, "title": readable[i].title or ""}
            for i in ids
            if i in readable
        ][:MAX_LIST],
    }


def _report(ctx: ToolContext, params: dict[str, Any]) -> Report:
    kind, period = params.get("kind"), params.get("period", "month")
    readers = audience(ctx, params)
    if (why := audience_problem(params, readers, ctx.team_id)) is not None:
        raise _public(why)
    days = PERIOD_DAYS[period]
    now = timezone.now()
    since = now - timedelta(days=days)
    today: date = now.astimezone(zone_for_user_id(ctx.user_id)).date()
    focus = _focus(params)
    lists: list[dict[str, Any]] = []
    note = ""

    try:
        if kind == "milestone":
            if params.get("milestone_id") is None:
                raise _public("This dashboard needs a milestone: choose one in the schedule.")
            summary = GET_MILESTONE_SUMMARY.run({"milestone_id": params["milestone_id"]}, ctx)
            milestone = MilestoneMaster.objects.get(milestone_id=params["milestone_id"])
            work = TaskMaster.objects.filter(milestone_work_q(milestone)).values("task_id")
            name = summary.get("title") or "Milestone"
            title = f"Milestone dashboard: {name}"
            stats, breakdown = _stand(summary)
            if summary.get("due_date"):
                stats.append({"label": "due", "value": str(summary["due_date"])})
            listed = [*summary.get("blocked", []), *summary.get("ready_now", [])]
            readable = visible_tasks(ctx, [r["task_id"] for r in listed], readers)
            if len(readable) < len(listed):
                note = "Some tasks are not named here because not everyone can open them."
            for heading, key in (
                ("Waiting on blockers", "blocked"),
                ("Ready to start", "ready_now"),
            ):
                rows = [r for r in summary.get(key, []) if r["task_id"] in readable]
                if rows:
                    lists.append(
                        {
                            "heading": heading,
                            "rows": [
                                f"{r.get('display_id')} · {plain(r.get('title')) or 'untitled'}"
                                for r in rows[:MAX_LIST]
                            ],
                        }
                    )
        elif kind == "project":
            if params.get("project_id") is None:
                raise _public("This dashboard needs a project: choose one in the schedule.")
            summary = GET_PROJECT_SUMMARY.run({"project_id": params["project_id"]}, ctx)
            work = TaskMaster.objects.filter(
                live_tasks_q(),
                team_id=ctx.team_id,
                project_id=summary["project_id"],
                is_deleted=False,
                is_init_task=False,
                is_milestone=False,
            ).values("task_id")
            title = f"Project dashboard: {summary.get('project_name') or 'Project'}"
            stats, breakdown = _stand(summary)
        else:
            summary = _mine(ctx, days, "day" if period in _DAILY else "week", readers)
            if len(readers) > 1:
                note = "Only projects everyone who reads this page is on are counted here."
            work = None
            title = "My progress"
            stats = [
                {"label": "closed", "value": str(summary["closed_total"])},
                {"label": "created", "value": str(summary["created_total"])},
                {"label": "net", "value": f"{summary['net']:+d}"},
            ]
            breakdown = []
    except ToolError as exc:
        # The tools' sentences name no content ("not found", "not a member of that project").
        raise _public(str(exc)[:200]) from None

    if work is not None:
        series = _series(work, since, period)
        closed_rows = _recently_closed(ctx, work, since, readers)
        stats.append(
            {
                "label": f"closed this {_period_name(period)}",
                "value": str(sum(p["closed"] for p in series)),
            }
        )
    else:
        series = [
            {"label": b["bucket_start"], "closed": b["closed"], "created": b["created"]}
            for b in summary.get("buckets", [])
            if b.get("bucket_start")
        ]
        closed_rows = [
            f"{r['display_id']} · {plain(r['title']) or 'untitled'}"
            for r in summary.get("recently_closed", [])
        ]
    if closed_rows:
        lists.append({"heading": "Recently closed", "rows": closed_rows})

    subtitle = f"Last {days} days, as of {today.isoformat()}"
    dashboard: Dashboard = {
        "title": title,
        "kind": kind,
        "subtitle": subtitle,
        "stats": stats,
        "series": series,
        "lists": lists,
    }
    if breakdown:
        dashboard["breakdown"] = breakdown
    if focus:
        dashboard["focus"] = focus
    if note:
        dashboard["note"] = note

    # The report the Inbox or the chat shows: a short statement and the same numbers, so a reader
    # that never opens the page still learns where things stand.
    line = ", ".join(f"{s['value']} {s['label']}" for s in stats[:4])
    sections = [
        {
            "heading": "Numbers",
            "key": "where_it_stands",
            "items": [item(f"{s['value']} {s['label']}") for s in stats],
        }
    ]
    for block in lists:
        sections.append(
            {"heading": block["heading"], "key": "items", "items": [item(r) for r in block["rows"]]}
        )
    return {
        "title": title,
        "summary": clip(f"{subtitle}: {line}."),
        "sections": sections,
        "stats": [],
        "dashboard": dashboard,
    }


def data_fingerprint(dashboard: Dashboard) -> str:
    """A hash of what the page SHOWS (numbers, series, lists, the focus), not of the date line.

    The summary is cached on it (MIP-2359): a dashboard whose data did not move since the last run
    reuses the last summary and makes no model call. The title is in it (another milestone is
    another page); the subtitle is not (it holds today's date)."""
    shown = {k: dashboard.get(k) for k in _FINGERPRINT_KEYS}
    raw = json.dumps(shown, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    """The report and the page's data, with a written summary when the member turned AI on."""
    report = _report(ctx, params)
    report = add_narrative(
        ctx,
        params,
        report,
        effort_default=SUMMARY_EFFORT_DEFAULT,
        purpose=DASHBOARD_PURPOSE,
        fingerprint=data_fingerprint(report["dashboard"]) if ai_sentences_on(params) else "",
    )
    if report.get("narrative"):
        report["dashboard"]["narrative"] = report["narrative"]
    return report


def _share_project(params: dict[str, Any], team_id: str) -> int | None:
    """The one project the dashboard is about, for a chat of its people (MIP-1420). `mine` has none."""
    if params.get("kind") == "project":
        project_id = params.get("project_id")
        return (
            project_id if isinstance(project_id, int) and not isinstance(project_id, bool) else None
        )
    milestone_id = params.get("milestone_id")
    if params.get("kind") != "milestone" or not isinstance(milestone_id, int):
        return None
    return (
        MilestoneMaster.objects.filter(milestone_id=milestone_id, team_id=team_id, is_deleted=False)
        .values_list("project_id", flat=True)
        .first()
    )


def _check_params(ctx: ToolContext, params: dict[str, Any], schedule_id: Any) -> dict[str, Any]:
    """On save: the plan allows HTML notes (Free is not offered this job), and the parameters fit
    the kind. A schedule that cannot make its page is told so now, not at its first run."""
    from origin.services import html_note_limits  # noqa: PLC0415
    from origin.services.schedules import ScheduleError  # noqa: PLC0415 (import cycle)

    try:
        html_note_limits.limits_for(ctx.user_id)
    except html_note_limits.HtmlNotesNotOnPlan as not_on_plan:
        raise ScheduleError(
            f"{not_on_plan} Dashboards are saved as HTML notes, so this report needs a Core plan."
        ) from None
    kind = params.get("kind")
    needs = {"milestone": "milestone_id", "project": "project_id"}
    for other_kind, key in needs.items():
        if kind == other_kind and params.get(key) is None:
            raise ScheduleError(f"A {kind} dashboard needs `{key}`.")
        if kind != other_kind and params.get(key) is not None:
            raise ScheduleError(f"`{key}` is only for a {other_kind} dashboard.")
    # Counted inside the member's row lock the caller holds, so two saves at once cannot both pass.
    others = MemberSchedule.objects.filter(user_id=ctx.user_id, job="ai_dashboard")
    # An edit of a dashboard the member already has is never refused for the count.
    editing_own = schedule_id is not None and others.filter(pk=schedule_id).exists()
    if schedule_id is not None:
        others = others.exclude(pk=schedule_id)
    if not editing_own and others.count() >= MAX_DASHBOARD_SCHEDULES:
        raise ScheduleError(
            f"You can keep {MAX_DASHBOARD_SCHEDULES} dashboard schedules, and you have that many. "
            "Delete one you no longer need first."
        )
    kept = {k: v for k, v in params.items() if v not in (None, "")}
    save_to, folder_id = kept.get("save_to", "me"), kept.get("folder_id")
    if (save_to == "me") != (folder_id is None):
        raise ScheduleError(
            "`folder_id` goes with `save_to` of `personal_folder` or `team_folder`."
        )
    # Who will read the page, asked now so the member is told at once (and again before every run).
    readers, folder_of_note = _readers_at_save(ctx, kept, schedule_id)
    if folder_of_note is not _NO_NOTE and folder_of_note != folder_id:
        old = MemberSchedule.objects.filter(pk=schedule_id).values_list("params", flat=True).first()
        if (old or {}).get("save_to", "me") != save_to or (old or {}).get("folder_id") != folder_id:
            raise ScheduleError(
                "This dashboard's note is already saved in another place. Delete the schedule to "
                "save a new page somewhere else, or move the note itself."
            )
    if (why := audience_problem(kept, readers, ctx.team_id)) is not None:
        raise ScheduleError(why)
    return kept


_NO_NOTE = object()


def _readers_at_save(
    ctx: ToolContext, params: dict[str, Any], schedule_id: Any
) -> tuple[set[str], Any]:
    """Who reads the page, and the folder its note sits in now (`_NO_NOTE` before the first run)."""
    from origin.models.note.personal_note_models import PersonalNoteMaster  # noqa: PLC0415
    from origin.services.schedules import ScheduleError  # noqa: PLC0415 (import cycle)

    output = (
        MemberSchedule.objects.filter(pk=schedule_id).values_list("output", flat=True).first()
        if schedule_id
        else None
    ) or {}
    note_id = output.get("note_id") if output.get("target") == "note" else None
    try:
        if isinstance(note_id, int):
            folder = (
                PersonalNoteMaster.objects.filter(note_id=note_id)
                .values_list("folder_id", flat=True)
                .first()
            )
            exists = PersonalNoteMaster.objects.filter(note_id=note_id).exists()
            if exists:
                found = schedule_audience.saved_note_readers(ctx.user_id, ctx.team_id, note_id)
                return found, folder
        return (
            schedule_audience.note_readers(ctx.user_id, ctx.team_id, note_target(params)),
            _NO_NOTE,
        )
    except schedule_audience.NoteTargetError as exc:
        raise ScheduleError(str(exc)) from None


def note_target(params: dict[str, Any]) -> dict[str, Any]:
    """The place the page is saved: my notes (`me`), or one of my personal folders, or a team
    folder."""
    return {"kind": params.get("save_to") or "me", "id": params.get("folder_id")}


def placement(params: dict[str, Any]) -> dict[str, Any]:
    """The `save_page` arguments that put a NEW note in its place."""
    kind = params.get("save_to") or "me"
    if kind == "team_folder":
        return {"note_type": "team", "folder_id": params.get("folder_id")}
    if kind == "personal_folder":
        return {"note_type": "personal", "folder_id": params.get("folder_id")}
    return {"note_type": "personal"}


register_job(
    Job(
        key="ai_dashboard",
        title="Dashboard page",
        description=(
            "One living dashboard page for a milestone, a project or your own progress: counts by "
            "status, what was closed and created over the last week, month, quarter or year, and "
            "what is ready or blocked. The page is saved as one note and replaced each run."
        ),
        agent_note=(
            "`kind` is `milestone`, `project` or `mine`; give `milestone_id` (from `list_milestones`) "
            "or `project_id` (from `list_projects`) with it. `period` is a data window, not how "
            "often it runs. The note is made by the server on the first run."
        ),
        run=run,
        share_project=_share_project,
        params_schema={
            "properties": {
                "kind": {"type": "string", "enum": list(KINDS)},
                "milestone_id": {"type": "integer", "minimum": 1},
                "project_id": {"type": "integer", "minimum": 1},
                "period": {"type": "string", "enum": list(PERIOD_DAYS)},
                "focus": {"type": "string"},
                "save_to": {"type": "string", "enum": ["me", "personal_folder", "team_folder"]},
                "folder_id": {"type": "integer", "minimum": 1},
                "ai_sentences": AI_SUMMARY_PARAM,
            },
            "required": ["kind"],
        },
        check_params=_check_params,
        uses_ai=True,
        ai_when=ai_sentences_on,
        effort_default=SUMMARY_EFFORT_DEFAULT,
        cost_basis="narrative",
        delivers_note=True,
        note_target=note_target,
    )
)
