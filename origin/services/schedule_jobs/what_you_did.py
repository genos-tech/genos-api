"""Job `what_you_did` (MIP-1148): the member's day looking back, from what is already saved.

The briefing's "what you did" (MIP-1145) as a report: tasks they changed and commented on,
notes they edited, to-dos they finished, messages sent counted per chat (never read), and
the PRs on their tasks. Nothing is asked of the member and nothing is written: a day with
nothing saved says so, it is not filled in. No model, no cost.
"""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from origin.search_engine.agent.tools.base import ToolContext
from origin.services import briefing
from origin.services.schedule_jobs import Job, Report, ReportSection, register_job
from origin.services.schedule_jobs.report_kit import Links, clip, item, plain
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


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    day = params.get("day", "today")
    zone = zone_for_user_id(ctx.user_id)
    did = briefing.did_on_day(ctx, day, timezone.now(), zone, MAX_ITEMS, MAX_EVENTS)

    links = Links(ctx)
    links.preload(
        tasks=[i["task_id"] for i in did["items"] if i["type"] == "task"],
        task_notes=[
            i["note_id"] for i in did["items"] if i["type"] == "note" and i["note_type"] == "task"
        ],
    )
    sections: list[ReportSection] = []
    tasks = [i for i in did["items"] if i["type"] == "task"]
    if tasks:
        sections.append(
            {
                "heading": "Tasks",
                "items": [
                    item(
                        f"{t['display_id']} · {plain(t['title'])}: "
                        f"{_events_text(t['events'], t['events_total'])}",
                        links.task(t["task_id"]),
                    )
                    for t in tasks
                ],
            }
        )
    notes = [i for i in did["items"] if i["type"] == "note"]
    if notes:
        sections.append(
            {
                "heading": "Notes",
                "items": [
                    item(
                        f"{plain(n['title']) or 'Untitled note'}: "
                        f"{_events_text(n['events'], n['events_total'])}",
                        links.note(n["note_type"], n["note_id"]),
                    )
                    for n in notes
                ],
            }
        )
    if did["todos_done"]:
        sections.append(
            {
                "heading": "To-dos finished",
                "items": [
                    item(
                        plain(t["title"]) or "untitled", links.todo(did["local_date"], t["item_id"])
                    )
                    for t in did["todos_done"]
                ],
            }
        )
    if did["chats"]:
        sections.append(
            {
                "heading": "Messages sent",
                "items": [
                    item(
                        f"{plain(c['title']) or 'a chat'}: {c['messages']} "
                        f"message{'s' if c['messages'] != 1 else ''}",
                        links.chat(c["chat_type"], c["chat_id"]),
                    )
                    for c in did["chats"]
                ],
            }
        )

    totals = did["totals"]
    facts = [
        (totals["items"], "item", "items"),
        (totals["comments"], "comment", "comments"),
        (totals["todos_done"], "to-do finished", "to-dos finished"),
        (totals["messages"], "message sent", "messages sent"),
    ]
    said = [f"{n} {one if n == 1 else many}" for n, one, many in facts if n]
    summary = f"{', '.join(said)}." if said else f"Nothing saved in Genos for {day} yet."
    return {
        "title": f"What you did {day}, {did['local_date']}",
        "summary": clip(summary),
        "sections": sections,
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
