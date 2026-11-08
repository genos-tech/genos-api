"""Job `what_to_do_today` (MIP-1148): the member's day laid out from what is already saved.

It is the briefing (`services.briefing`) read as a list a person can act on: the same
sections, the same access rules, so "what should I do today" says the same thing in the
app, in Claude and in the morning report. No model, no cost, and nothing is written. It
does NOT move the member's "since your last briefing" marker (it asks for a fixed look-back
window, never `since_last`), so a scheduled report does not use up what their own Claude
would have told them.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.utils import timezone

from origin.search_engine.agent.tools.base import ToolContext
from origin.services import briefing
from origin.services.schedule_jobs import (
    Job,
    Report,
    ReportSection,
    register_job,
)
from origin.services.schedule_jobs.report_kit import (
    Links,
    arrange,
    clip,
    in_order,
    item,
    more,
    plain,
)

DEFAULT_CHANGED_HOURS = 24

# What each section is, for the card (MIP-1208). "Changed in the last N hours" carries N.
_SECTION_KEYS = {
    "Start with": "start_with",
    "Overdue": "overdue",
    "Due today": "due_today",
    "To-dos for today": "todos_today",
    "To-dos left over from earlier": "todos_earlier",
    "On your calendar": "calendar",
    "Waiting on you": "waiting_on_you",
    "You are waiting on others": "waiting_on_others",
    "Gone quiet": "gone_quiet",
}


# Sections whose order is the content: not grouped by project.
_KEEP_ORDER = ("Start with",)


def _task_text(row: dict[str, Any]) -> str:
    text = f"{row.get('display_id') or 'Task'} · {plain(row.get('title')) or 'untitled'}"
    details = []
    days = row.get("days_until_due")
    if isinstance(days, int):
        if days < 0:
            details.append(f"{-days} day{'s' if days != -1 else ''} late")
        elif days == 0:
            details.append("due today")
        else:
            details.append(f"due in {days} day{'s' if days != 1 else ''}")
    if row.get("priority"):
        details.append(str(row["priority"]))
    if row.get("status"):
        details.append(str(row["status"]))
    return f"{text} ({', '.join(details)})" if details else text


def _clock(value: Any, zone: ZoneInfo) -> str:
    try:
        return (
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            .astimezone(zone)
            .strftime("%H:%M")
        )
    except ValueError:
        return ""


def _calendar_items(calendar: dict[str, Any], zone: ZoneInfo) -> list[dict[str, Any]]:
    out = []
    for event in calendar.get("events") or []:
        title = plain(event.get("summary")) or "Busy"
        when = "All day" if event.get("all_day") else _clock(event.get("start"), zone)
        link = event.get("html_link")
        out.append(
            item(
                f"{when} · {title}" if when else title,
                link if isinstance(link, str) and link.startswith("https://") else None,
                "event",
            )
        )
    return out


def _mention_item(entry: dict[str, Any], links: Links) -> dict[str, Any]:
    who = plain(entry.get("from")) or "someone"
    snippet = plain(entry.get("text"))
    source = entry.get("source")
    if source == "chat":
        where = plain(entry.get("chat_title")) or "a chat"
        link = links.chat(entry.get("chat_type"), entry.get("chat_id"), entry.get("thread_id"))
    elif source in ("task", "task_comment"):
        where = f"{entry.get('display_id') or 'a task'} {plain(entry.get('title'))}".strip()
        text = f"{who} mentioned you in {where}"
        return links.task_item(
            f"{text}: {snippet}" if snippet else text,
            entry.get("task_id"),
            f"{who} mentioned you: {snippet}" if snippet else f"{who} mentioned you",
        )
    elif source == "note":
        where = plain(entry.get("title")) or "a note"
        link = links.note(entry.get("note_type"), entry.get("note_id"))
    else:
        where, link = "Genos", None
    text = f"{who} mentioned you in {where}"
    return item(f"{text}: {snippet}" if snippet else text, link, "mention")


def _changed_item(row: dict[str, Any], links: Links) -> dict[str, Any]:
    kind = row.get("type")
    title = plain(row.get("title")) or "untitled"
    if kind == "task":
        label, link = f"{row.get('display_id') or 'Task'} · {title}", None
    elif kind == "note":
        label, link = f"Note · {title}", links.note(row.get("note_type"), row.get("note_id"))
    else:
        label = f"Thread in {title}"
        link = links.chat(row.get("chat_type"), row.get("chat_id"), row.get("thread_id"))
    events = row.get("events") or []
    latest = events[-1] if events else {}
    by = plain(latest.get("by")) or "someone"
    what = latest.get("summary") or latest.get("what") or "changed"
    said = f"{what} by {by}"
    if (row.get("events_total") or 0) > 1:
        said += f" (+{row['events_total'] - 1} more)"
    text = f"{label}: {said}"
    if kind == "task":
        return links.task_item(text, row.get("task_id"), said)
    return item(text, link, "note" if kind == "note" else "chat")


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    now = timezone.now()
    hours = int(params.get("changed_hours", DEFAULT_CHANGED_HOURS))
    data = briefing.build_briefing(
        ctx,
        since=(now - timedelta(hours=hours)).isoformat(),
        project_id=params.get("project_id"),
        now=now,
    )
    zone = ZoneInfo(data["window"]["time_zone"])
    agenda, waiting, others = data["agenda"], data["waiting_on_me"], data["waiting_on_others"]
    changed = data["changed_for_me"]

    links = Links(ctx)
    waiting_on_notes = [m for m in waiting["mentions"] if m.get("source") == "note"]
    links.preload(
        tasks=[
            *(t["task_id"] for t in data["suggested_next"]),
            *(t["task_id"] for t in agenda["overdue"]),
            *(t["task_id"] for t in agenda["due_today"]),
            *(t["task_id"] for t in data["stale_mine"]),
            *(t["task_id"] for t in others["blocked_tasks"]),
            *(c["task_id"] for c in changed["items"] if c["type"] == "task"),
            *(m.get("task_id") for m in waiting["mentions"]),
        ],
        task_notes=[
            *(m["note_id"] for m in waiting_on_notes if m.get("note_type") == "task"),
            *(c["note_id"] for c in changed["items"] if c.get("note_type") == "task"),
        ],
    )

    def tasks(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [links.task_item(_task_text(r), r["task_id"]) for r in rows]

    today = date.fromisoformat(agenda["local_date"])

    sections: list[ReportSection] = []

    def add(
        heading: str, items: list[dict[str, Any]], total: int | None = None, noun: str = ""
    ) -> None:
        if not items:
            return
        # Tasks grouped by project and sorted inside each group (MIP-1207); anything else keeps
        # its order. "Start with" keeps the briefing's rank order instead (MIP-1236): the
        # ranking is the point of that section. The "and N more" line is added after, so it
        # stays last.
        shown = (in_order if heading in _KEEP_ORDER else arrange)(list(items), today)
        if total is not None and total > len(shown):
            shown.append(more(total - len(shown), noun))
        section: ReportSection = {"heading": heading, "items": shown}
        key = _SECTION_KEYS.get(heading) or ("changed" if heading.startswith("Changed in") else "")
        if key:
            section["key"] = key
        sections.append(section)

    start = [
        links.task_item(
            f"{_task_text(t)}: {'; '.join(t.get('reasons') or [])}"
            if t.get("reasons")
            else _task_text(t),
            t["task_id"],
            "; ".join(t.get("reasons") or []),
        )
        for t in data["suggested_next"]
    ]
    add("Start with", start)
    add("Overdue", tasks(agenda["overdue"]), agenda["overdue_count"], "overdue tasks")
    add("Due today", tasks(agenda["due_today"]), agenda["due_today_count"], "tasks due today")

    todo_date = agenda["local_date"]
    add(
        "To-dos for today",
        [
            item(
                plain(t.get("title")) or "untitled",
                links.todo(todo_date, t.get("item_id")),
                "todo",
            )
            for t in agenda["todos_today"]
            if not t.get("is_completed")
        ],
    )
    add(
        "To-dos left over from earlier",
        [
            item(
                f"{plain(t.get('title')) or 'untitled'} (from {t.get('local_date')})",
                links.todo(t.get("local_date"), t.get("item_id")),
                "todo",
            )
            for t in agenda["todos_unfinished_earlier"]
        ],
    )
    calendar = agenda["calendar"]
    if calendar.get("events") is None and calendar.get("note"):
        # Not "no meetings": the calendar could not be read, and the member must know.
        add("On your calendar", [item(f"Not shown: {calendar['note']}")])
    else:
        add("On your calendar", _calendar_items(calendar, zone))

    requests = [
        item(
            f"{r.get('type') or 'Request'} from {plain(r.get('from')) or 'someone'}"
            + (f": {plain(r.get('text'))}" if r.get("text") else ""),
            kind="request",
        )
        for r in waiting["requests"]
    ]
    waiting_items = requests + [_mention_item(m, links) for m in waiting["mentions"]]
    # The briefing lists at most MAX_LIST of each and does not count the rest, so a full
    # list may be a partial one: say so instead of letting the overflow go unseen.
    waiting_capped = (
        len(waiting["requests"]) >= briefing.MAX_LIST
        or len(waiting["mentions"]) >= briefing.MAX_LIST
    )
    if waiting_capped:
        waiting_items.append(item("There may be more: open Genos to see them all."))
    add("Waiting on you", waiting_items)

    add(
        f"Changed in the last {hours} hours",
        [_changed_item(c, links) for c in changed["items"]],
        changed["total"],
        "changed items",
    )
    add(
        "You are waiting on others",
        [
            links.task_item(
                f"{t.get('display_id')} · {plain(t.get('title'))}: waiting for "
                + ", ".join(
                    f"{e['display_id']} ({plain(e.get('assignee')) or 'nobody yet'})"
                    for e in t.get("waiting_for") or []
                ),
                t["task_id"],
                "waiting for "
                + ", ".join(
                    f"{e['display_id']} ({plain(e.get('assignee')) or 'nobody yet'})"
                    for e in t.get("waiting_for") or []
                ),
            )
            for t in others["blocked_tasks"]
        ],
    )
    add(
        "Gone quiet",
        [
            links.task_item(
                f"{_task_text(t)}: untouched for {t['days_idle']} days",
                t["task_id"],
                f"untouched for {t['days_idle']} days",
            )
            for t in data["stale_mine"]
        ],
    )

    counts = [
        (agenda["overdue_count"], "overdue"),
        (agenda["due_today_count"], "due today"),
        (len(waiting["requests"]) + len(waiting["mentions"]), "waiting on you"),
    ]
    said = [
        f"{n}{'+' if label == 'waiting on you' and waiting_capped else ''} {label}"
        for n, label in counts
        if n
    ]
    summary = f"{', '.join(said)}." if said else "Nothing is overdue, due today or waiting on you."
    return {
        "title": f"What to do today, {agenda['local_date']}",
        "summary": clip(summary),
        "sections": sections,
        "stats": [
            {"key": key, "count": n}
            for key, n in (
                ("overdue", agenda["overdue_count"]),
                ("due_today", agenda["due_today_count"]),
                ("waiting_on_you", len(waiting["requests"]) + len(waiting["mentions"])),
                ("waiting_on_others", len(others["blocked_tasks"])),
                ("gone_quiet", len(data["stale_mine"])),
            )
            if n
        ],
    }


register_job(
    Job(
        key="what_to_do_today",
        title="What to do today",
        description=(
            "Your day from what is already in Genos: what to start with, what is overdue or "
            "due today, your to-dos and calendar, what waits on you, what changed lately, "
            "and what has gone quiet."
        ),
        run=run,
        params_schema={
            "properties": {
                "changed_hours": {"type": "integer", "minimum": 1, "maximum": 168},
                "project_id": {"type": "integer", "minimum": 1},
            }
        },
    )
)
