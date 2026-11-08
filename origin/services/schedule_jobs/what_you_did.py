"""Job `what_you_did` (MIP-1148): the member's day looking back, from what is already saved.

The briefing's "what you did" (MIP-1145) as a report: tasks they changed and commented on,
notes they edited, to-dos they finished, messages sent counted per chat (never read), and
the PRs on their tasks. Nothing is asked of the member and nothing is written: a day with
nothing saved says so, it is not filled in. No model, no cost.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from django.utils import timezone

from origin.search_engine.agent.tools.base import ToolContext
from origin.services import briefing
from origin.services.schedule_jobs import Job, Report, ReportSection, register_job
from origin.services.schedule_jobs.report_kit import Links, arrange, clip, item, plain
from origin.services.user_time import zone_for_user_id

# A report has more room than the briefing, which is sized for a model's context.
MAX_ITEMS = 15
MAX_EVENTS = 4


def _events_text(events: list[dict[str, Any]], total: int) -> str:
    parts = []
    for e in events:
        said = e.get("summary") or e.get("what") or "changed"
        by = f" ({e['by']})" if e.get("automated") and e.get("by") else ""
        parts.append(f"{e.get('at', '')} {said}{by}".strip())
    text = "; ".join(parts)
    return f"{text}; and {total - len(events)} earlier" if total > len(events) else text


def _looked_item(entry: dict[str, Any], links: Links):
    """One line: what the member's AI app opened or changed, and which app, with a link."""
    kind = entry["type"]
    title = plain(entry["title"]) or f"Untitled {kind}"
    app = plain(entry["app"])
    app_text = "your AI app" if app == "api_key" else app
    verb = "changed" if entry["kind"] == "changed" else "opened"
    if kind == "task":
        return links.task_item(
            f"{entry['display_id']} · {title}: {verb} by {app_text} at {entry['at']}",
            entry["task_id"],
        )
    if kind == "note":
        link = links.note(entry["note_type"], entry["note_id"])
    elif kind == "milestone":
        link = links.milestone(entry["project_id"], entry["milestone_id"])
    else:
        link = links.project(entry["project_id"])
    return item(
        f"{title}: {verb} by {app_text} at {entry['at']}",
        link,
        kind if kind in ("note", "milestone", "project") else None,
    )


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    day = params.get("day", "today")
    zone = zone_for_user_id(ctx.user_id)
    did = briefing.did_on_day(
        ctx, day, timezone.now(), zone, MAX_ITEMS, MAX_EVENTS, briefing.DID_LOOKED
    )
    looked = did.get("looked_at", [])

    today = date.fromisoformat(did["local_date"])
    links = Links(ctx)
    shown = did["items"] + looked
    links.preload(
        tasks=[i["task_id"] for i in shown if i["type"] == "task"],
        task_notes=[
            i["note_id"] for i in shown if i["type"] == "note" and i["note_type"] == "task"
        ],
    )
    sections: list[ReportSection] = []
    tasks = [i for i in did["items"] if i["type"] == "task"]
    if tasks:
        sections.append(
            {
                "heading": "Tasks",
                "key": "tasks",
                "items": arrange(
                    [
                        links.task_item(
                            f"{t['display_id']} · {plain(t['title'])}: "
                            f"{_events_text(t['events'], t['events_total'])}",
                            t["task_id"],
                            _events_text(t["events"], t["events_total"]),
                        )
                        for t in tasks
                    ],
                    today,
                ),
            }
        )
    notes = [i for i in did["items"] if i["type"] == "note"]
    if notes:
        sections.append(
            {
                "heading": "Notes",
                "key": "notes",
                "items": [
                    item(
                        f"{plain(n['title']) or 'Untitled note'}: "
                        f"{_events_text(n['events'], n['events_total'])}",
                        links.note(n["note_type"], n["note_id"]),
                        "note",
                    )
                    for n in notes
                ],
            }
        )
    if did["todos_done"]:
        sections.append(
            {
                "heading": "To-dos finished",
                "key": "todos_done",
                "items": [
                    item(
                        plain(t["title"]) or "untitled",
                        links.todo(did["local_date"], t["item_id"]),
                        "todo",
                    )
                    for t in did["todos_done"]
                ],
            }
        )
    if did["chats"]:
        sections.append(
            {
                "heading": "Messages sent",
                "key": "messages",
                "items": [
                    item(
                        f"{plain(c['title']) or 'a chat'}: {c['messages']} "
                        f"message{'s' if c['messages'] != 1 else ''}",
                        links.chat(c["chat_type"], c["chat_id"]),
                        "chat",
                    )
                    for c in did["chats"]
                ],
            }
        )

    if looked:
        sections.append(
            {
                "heading": "Looked at with your AI app",
                "key": "looked_at",
                "items": arrange([_looked_item(i, links) for i in looked], today),
            }
        )

    totals = did["totals"]
    facts = [
        (totals["items"], "item", "items"),
        (totals["comments"], "comment", "comments"),
        (totals["todos_done"], "to-do finished", "to-dos finished"),
        (totals["messages"], "message sent", "messages sent"),
        (len(looked), "item looked at with your AI app", "items looked at with your AI app"),
    ]
    said = [f"{n} {one if n == 1 else many}" for n, one, many in facts if n]
    summary = f"{', '.join(said)}." if said else f"Nothing saved in Genos for {day} yet."
    return {
        "title": f"What you did {day}, {did['local_date']}",
        "summary": clip(summary),
        "sections": sections,
        "stats": [
            {"key": key, "count": n}
            for key, n in (
                ("items", totals["items"]),
                ("comments", totals["comments"]),
                ("todos_done", totals["todos_done"]),
                ("messages", totals["messages"]),
                ("looked_at", len(looked)),
            )
            if n
        ],
    }


register_job(
    Job(
        key="what_you_did",
        title="What you did",
        description=(
            "What you did today or yesterday, from what Genos already saved: tasks you "
            "changed and commented on, notes you edited, to-dos you finished, and how many "
            "messages you sent in which chats. Nothing to type in."
        ),
        run=run,
        params_schema={"properties": {"day": {"type": "string", "enum": ["today", "yesterday"]}}},
    )
)
