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
from origin.services.history_words import HistoryWords
from origin.services.milestone_rollup import milestone_work_q
from origin.services.schedule_jobs import SHARED_KEY, Job, Report, ReportSection, register_job
from origin.services.schedule_jobs.report_kit import (
    AI_SUMMARY_PARAM,
    SUMMARY_EFFORT_DEFAULT,
    Links,
    add_narrative,
    ai_sentences_on,
    arrange,
    clip,
    counted,
    dedupe_tasks,
    item,
    more,
    plain,
    span,
    status_line,
)
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


def _report(ctx: ToolContext, params: dict[str, Any]) -> Report:
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
    # A milestone's work can sit in other projects (a task moved out, a child of its backing
    # row). In a chat other people read, only the milestone's own project is named: its readers
    # are that project's people, not the owner's (MIP-1420).
    shared = bool(params.get(SHARED_KEY))
    if shared:
        readable = {i: t for i, t in readable.items() if t.project_id == milestone.project_id}
    by_task: dict[int, dict[str, Any]] = {}
    # In a chat other people read nothing is "you": the owner is one reader of several.
    viewer = "" if shared else ctx.user_id
    words = HistoryWords(ctx, [a for a in activities if a.task_id in readable], viewer_id=viewer)
    for a in activities:
        if a.task_id in readable:
            event = briefing.activity_event(a, viewer, words)
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
    if shared:
        openable = {i: t for i, t in openable.items() if t.project_id == milestone.project_id}
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

    stands = [item(status_line(summary["total_tasks"], summary["status_breakdown"]))]
    if summary.get("overdue_count"):
        stands.append(item(f"{summary['overdue_count']} overdue"))
    if summary.get("due_date"):
        stands.append(item(f"Due {summary['due_date']}"))

    # What needs a decision comes first (the card opens its first two sections): what waits on
    # blockers, then what changed, then what is ready, and the standing counts last (MIP-1417).
    by_key: dict[str, ReportSection] = {}
    for heading, key, count_key, section_key in (
        ("Waiting on blockers", "blocked", "blocked_count", "blocked"),
        ("Ready to start", "ready_now", "ready_now_count", "ready"),
    ):
        rows = summary.get(key) or []
        if rows:
            shown = arrange([_ref(r, links) for r in rows[:MAX_LIST]], today)
            # The summary's counts include tasks the member may not open; once any was
            # dropped they would give its number away, so the "and N more" is left out.
            total = summary.get(count_key, len(rows)) if all_openable else len(shown)
            if total > len(shown):
                shown.append(more(total - len(shown), "tasks"))
            by_key[section_key] = {"heading": heading, "key": section_key, "items": shown}
    if changed_items:
        by_key["changed"] = {
            "heading": f"Changed in the last {span(hours)}",
            "key": "changed",
            "items": changed_items,
        }
    # A task is one line, in the section that matters most (a ready task that also changed is
    # listed under what changed).
    sections = dedupe_tasks(
        [by_key[k] for k in ("blocked", "changed", "ready") if k in by_key],
        ("blocked", "changed", "ready"),
    )
    news = any(s["key"] in ("blocked", "changed") for s in sections)
    sections.append({"heading": "Where it stands", "key": "where_it_stands", "items": stands})

    # The count matches the list: a changed task that is listed under "Waiting on blockers" is
    # that line, not a second one under "Changed".
    under_blockers = {
        i["task_id"] for i in (by_key.get("blocked") or {"items": []})["items"] if i.get("task_id")
    }
    changed_n = len([t for t in by_task if t not in under_blockers])
    title = summary.get("title") or "Milestone"
    line = (
        f"{counted(changed_n, 'task')} changed in the last {span(hours)}; "
        f"{summary['total_tasks']} in all."
    )
    return {
        "title": f"Milestone: {title}",
        "summary": clip(line),
        "sections": sections,
        # Nothing changed and nothing waits on a blocker: the counts alone are not news (MIP-1417).
        "quiet": not news,
        "stats": [
            {"key": key, "count": n}
            for key, n in (
                ("changed", changed_n),
                ("overdue", summary.get("overdue_count") or 0),
            )
            if n
        ],
    }


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    """The report, with a written summary on top when the member turned the AI part on."""
    return add_narrative(ctx, params, _report(ctx, params), effort_default=SUMMARY_EFFORT_DEFAULT)


def _share_project(params: dict[str, Any], team_id: str) -> int | None:
    """The project of the milestone this report is about: a chat of its people may read it
    (MIP-1420). None when the milestone is not in this team (the run then names it)."""
    milestone_id = params.get("milestone_id")
    if not isinstance(milestone_id, int) or isinstance(milestone_id, bool):
        return None
    return (
        MilestoneMaster.objects.filter(milestone_id=milestone_id, team_id=team_id, is_deleted=False)
        .values_list("project_id", flat=True)
        .first()
    )


register_job(
    Job(
        key="milestone_updates",
        title="Milestone updates",
        description=(
            "Where one milestone stands and what changed in it lately: task counts by status, "
            "what changed in the last days by anyone, what is ready to start and what waits on "
            "blockers."
        ),
        agent_note="`milestone_id` is the milestone's numeric id, from `list_milestones`.",
        run=run,
        share_project=_share_project,
        params_schema={
            "properties": {
                "milestone_id": {"type": "integer", "minimum": 1},
                "hours": {"type": "integer", "minimum": 1, "maximum": 720},
                "ai_sentences": AI_SUMMARY_PARAM,
            },
            "required": ["milestone_id"],
        },
        uses_ai=True,
        ai_when=ai_sentences_on,
        effort_default=SUMMARY_EFFORT_DEFAULT,
        cost_basis="narrative",
    )
)
