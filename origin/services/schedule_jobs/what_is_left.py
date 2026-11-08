"""Job `what_is_left` (MIP-1152): what is still open from the past days, for a Monday morning.

"09:00 every Monday: what is left from last week". It looks back `days` (default 7) from the
start of the member's day and reads only what they themselves did and what is already saved
(the briefing's "what you did", MIP-1145): the tasks they worked on that are still not
closed, the ones they closed, and the to-dos they left unfinished. Nothing is asked of them
and nothing is written; no model, no cost.

Like `what_to_do_today` it does not repeat itself (MIP-1211, `services/report_memory.py`): a
still-open task whose state did not change since the member's last opened "what is left" is held
back and counted. "Closed after your work" and the to-dos are records, never held back.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.list_uncompleted_todos import LIST_UNCOMPLETED_TODOS
from origin.services import briefing, report_memory
from origin.services.schedule_jobs import Job, Report, ReportSection, register_job
from origin.services.schedule_jobs.report_kit import Links, arrange, clip, item, more, plain
from origin.services.user_time import zone_for_user_id

DEFAULT_DAYS = 7
# A week of a busy member: more than the briefing's five, fewer than a wall of text.
MAX_TASKS = 30
MAX_TODOS = 15
_FINISHED = {"Closed"}
_GONE = {"Closed", "Deleted"}


def _latest(events: list[dict[str, Any]]) -> str:
    if not events:
        return ""
    last = events[-1]
    return str(last.get("summary") or last.get("what") or "")


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    now = timezone.now()
    zone = zone_for_user_id(ctx.user_id)
    days = int(params.get("days", DEFAULT_DAYS))
    start_today = datetime.combine(now.astimezone(zone).date(), datetime.min.time(), tzinfo=zone)
    since = start_today - timedelta(days=days)
    did = briefing.did_between(ctx, since, now + timedelta(seconds=1), MAX_TASKS, 2, zone)

    tasks = [i for i in did["items"] if i["type"] == "task"]
    links = Links(ctx)
    links.preload(tasks=[t["task_id"] for t in tasks])

    today_date = now.astimezone(zone).date()
    still_open = [t for t in tasks if t["status"] not in _GONE]
    finished = [t for t in tasks if t["status"] in _FINISHED]

    verdict = report_memory.judge(
        ctx,
        "what_is_left",
        [t["task_id"] for t in tasks],
        links.facts,
        today=today_date,
        now=now,
        start_with=set(),
        changed=set(),
        holdable={t["task_id"] for t in still_open},
    )
    links.why = verdict.why
    links.can_answer = verdict.can_answer
    open_listed = [t for t in still_open if t["task_id"] not in verdict.held]

    def line(t: dict[str, Any]) -> dict[str, Any]:
        text = f"{t['display_id']} · {plain(t['title']) or 'untitled'} ({t['status']})"
        said = _latest(t["events"])
        return links.task_item(f"{text}: {said}" if said else text, t["task_id"], said)

    todos = LIST_UNCOMPLETED_TODOS.run({"date_range_days": days}, ctx).get("items", [])
    today = now.astimezone(zone).date().isoformat()
    left_todos = [t for t in todos if t.get("local_date") and t["local_date"] < today]

    sections: list[ReportSection] = []
    if open_listed:
        sections.append(
            {
                "heading": "Still open from last time",
                "key": "still_open",
                "items": arrange([line(t) for t in open_listed], today_date),
            }
        )
    if finished:
        sections.append(
            {
                "heading": "Closed after your work",
                "key": "closed",
                "items": arrange([line(t) for t in finished], today_date),
            }
        )
    if left_todos:
        shown = [
            item(
                f"{plain(t.get('title')) or 'untitled'} (from {t['local_date']})",
                links.todo(t["local_date"], t.get("item_id")),
                "todo",
            )
            for t in left_todos[:MAX_TODOS]
        ]
        if len(left_todos) > MAX_TODOS:
            shown.append(more(len(left_todos) - MAX_TODOS, "to-dos"))
        sections.append({"heading": "To-dos left unfinished", "key": "todos_left", "items": shown})

    facts = [
        (len(still_open), "task still open"),
        (len(finished), "task closed"),
        (len(left_todos), "to-do left"),
    ]
    said = [f"{n} {label}{'' if n == 1 else 's'}" for n, label in facts if n]
    summary = (
        f"{', '.join(said)} from the last {days} days."
        if said
        else f"Nothing saved in Genos for the last {days} days."
    )
    if verdict.same:
        summary += f" {verdict.same} unchanged since your last report."
    if verdict.quieted:
        summary += f" {verdict.quieted} quieted by you."
    return {
        "title": f"What is left from the last {days} days, {today}",
        "summary": clip(summary),
        "sections": sections,
        "stats": [
            {"key": key, "count": n}
            for key, n in (
                ("still_open", len(still_open)),
                ("closed", len(finished)),
                ("todos_left", len(left_todos)),
                (report_memory.STAT_STILL_SAME, verdict.same),
                (report_memory.STAT_QUIETED, verdict.quieted),
            )
            if n
        ],
        "seen": verdict.seen,
    }


register_job(
    Job(
        key="what_is_left",
        title="What is left from last week",
        description=(
            "What is still open from the last days, from what you did and what Genos already "
            "saved: tasks you worked on that are not closed, tasks closed after your work, and "
            "to-dos left unfinished. Good for Monday morning."
        ),
        run=run,
        params_schema={"properties": {"days": {"type": "integer", "minimum": 1, "maximum": 31}}},
    )
)
