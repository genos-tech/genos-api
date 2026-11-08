"""Job `milestone_updates` (MIP-1152): where one milestone stands and what changed in it.

"13:00 every Wednesday: milestone X updates". It reads the milestone through the same tool the
agent uses (`get_milestone_summary`), so the access rule is that tool's: the member must be in
the milestone's project and team, and a milestone they cannot open fails the run in words
rather than saying nothing. The changes are everything done on the milestone's tasks in the
window, by anyone, on tasks the member may open. No model, no cost, nothing written.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.get_milestone_summary import GET_MILESTONE_SUMMARY
from origin.services import briefing
from origin.services.milestone_rollup import milestone_work_q
from origin.services.schedule_jobs import Job, Report, ReportSection, register_job
from origin.services.schedule_jobs.report_kit import Links, arrange, clip, item, more, plain
from origin.services.user_time import zone_for_user_id

DEFAULT_HOURS = 168
MAX_CHANGED = 15
MAX_LIST = 8
_POOL = 400


def _public(message: str):
    from origin.services.schedules import ScheduleRunError  # noqa: PLC0415 (import cycle)

    return ScheduleRunError(message)


def _ref(row: dict[str, Any], links: Links) -> dict[str, Any]:
    text = f"{row.get('display_id')} · {plain(row.get('title')) or 'untitled'}"
    if row.get("status"):
        text += f" ({row['status']})"
    return links.task_item(text, row.get("task_id"))


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    milestone_id = params.get("milestone_id")
    if milestone_id is None:
        raise _public("This report needs a milestone: choose one in the schedule.")
    try:
        summary = GET_MILESTONE_SUMMARY.run({"milestone_id": milestone_id}, ctx)
    except ToolError as exc:
        # The tool's sentences name no content ("not found", "not a member of that project").
        raise _public(str(exc)[:200]) from None
    hours = int(params.get("hours", DEFAULT_HOURS))
    since = timezone.now() - timedelta(hours=hours)

    milestone = MilestoneMaster.objects.get(milestone_id=milestone_id)
    task_ids = TaskMaster.objects.filter(milestone_work_q(milestone)).values("task_id")
    activities = list(
        TaskActivity.objects.filter(task_id__in=task_ids, ts_created_at__gte=since)
        .exclude(
            action_type__in=(
                TaskActivityActionType.COMMENT_ADDED,
                TaskActivityActionType.COMMENT_EDITED,
                TaskActivityActionType.COMMENT_DELETED,
            )
        )
        # Nobody to credit for "created it": automation and imports, not news (as in the
        # briefing).
        .exclude(actor__isnull=True, action_type=TaskActivityActionType.CREATED)
        .order_by("-ts_created_at")[:_POOL]
    )
    comments = list(
        TaskComments.objects.filter(
            task_id__in=task_ids, ts_sent_at__gte=since, is_deleted=False
        ).order_by("-ts_sent_at")[:_POOL]
    )
    readable = LabelAccess(ctx).tasks(
        {a.task_id for a in activities} | {c.task_id for c in comments}
    )
    by_task: dict[int, dict[str, Any]] = {}
    for a in activities:
        if a.task_id in readable:
            event = briefing.activity_event(a, ctx.user_id)
            slot = by_task.setdefault(
                a.task_id, {"at": a.ts_created_at, "n": 0, "what": "", "who": None, "label": None}
            )
            slot["n"] += 1
            if not slot["what"]:
                slot.update(what=event["summary"], who=a.actor_id, label=event["_label"])
    for c in comments:
        if c.task_id in readable:
            slot = by_task.setdefault(
                c.task_id, {"at": c.ts_sent_at, "n": 0, "what": "", "who": None}
            )
            slot["n"] += 1
            if not slot["what"] or c.ts_sent_at > slot["at"]:
                slot.update(
                    what="commented",
                    who=c.sender_id,
                    label=None,
                    at=max(slot["at"], c.ts_sent_at),
                )
    names = LabelAccess(ctx).usernames({s["who"] for s in by_task.values() if s["who"]})

    # The summary tool names the milestone's ready and blocked tasks; a task the member may not
    # open must not be named in their report, whatever project it sits in.
    listed = [*summary.get("ready_now", []), *summary.get("blocked", [])]
    openable = LabelAccess(ctx).tasks(r["task_id"] for r in listed)
    all_openable = all(r["task_id"] in openable for r in listed)
    summary["ready_now"] = [r for r in summary.get("ready_now", []) if r["task_id"] in openable]
    summary["blocked"] = [r for r in summary.get("blocked", []) if r["task_id"] in openable]

    today = timezone.now().astimezone(zone_for_user_id(ctx.user_id)).date()
    links = Links(ctx)
    links.preload(
        tasks=[
            *by_task,
            *(r["task_id"] for r in summary.get("ready_now", [])),
            *(r["task_id"] for r in summary.get("blocked", [])),
        ]
    )
    ranked = sorted(by_task.items(), key=lambda kv: kv[1]["at"], reverse=True)
    changed_items = []
    for task_id, slot in ranked[:MAX_CHANGED]:
        task = readable[task_id]
        who = names.get(str(slot["who"]), "someone") if slot["who"] else (slot["label"] or "system")
        said = f"{slot['what']} by {who}"
        if slot["n"] > 1:
            said += f" (+{slot['n'] - 1} more)"
        changed_items.append(
            links.task_item(
                f"{task.display_id} · {task.title or 'untitled'}: {said}", task_id, said
            )
        )
    changed_items = arrange(changed_items, today)
    if len(ranked) > MAX_CHANGED:
        changed_items.append(more(len(ranked) - MAX_CHANGED, "changed tasks"))

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
    if summary.get("due_date"):
        stands.append(item(f"Due {summary['due_date']}"))

    sections: list[ReportSection] = [
        {"heading": "Where it stands", "key": "where_it_stands", "items": stands}
    ]
    if changed_items:
        sections.append(
            {
                "heading": f"Changed in the last {hours} hours",
                "key": "changed",
                "items": changed_items,
            }
        )
    for heading, key, count_key, section_key in (
        ("Ready to start", "ready_now", "ready_now_count", "ready"),
        ("Waiting on blockers", "blocked", "blocked_count", "blocked"),
    ):
        rows = summary.get(key) or []
        if rows:
            shown = arrange([_ref(r, links) for r in rows[:MAX_LIST]], today)
            # The summary's counts include tasks the member may not open; once any was
            # dropped they would give its number away, so the "and N more" is left out.
            total = summary.get(count_key, len(rows)) if all_openable else len(shown)
            if total > len(shown):
                shown.append(more(total - len(shown), "tasks"))
            sections.append({"heading": heading, "key": section_key, "items": shown})

    title = summary.get("title") or "Milestone"
    line = f"{len(by_task)} task(s) changed in the last {hours} hours; {summary['total_tasks']} in all."
    return {
        "title": f"Milestone: {title}",
        "summary": clip(line),
        "sections": sections,
        "stats": [
            {"key": key, "count": n}
            for key, n in (
                ("changed", len(by_task)),
                ("overdue", summary.get("overdue_count") or 0),
            )
            if n
        ],
    }


register_job(
    Job(
        key="milestone_updates",
        title="Milestone updates",
        description=(
            "Where one milestone stands and what changed in it lately: task counts by status, "
            "what changed in the last days by anyone, what is ready to start and what waits on "
            "blockers. Choose the milestone by its id."
        ),
        run=run,
        params_schema={
            "properties": {
                "milestone_id": {"type": "integer", "minimum": 1},
                "hours": {"type": "integer", "minimum": 1, "maximum": 720},
            },
            "required": ["milestone_id"],
        },
    )
)
