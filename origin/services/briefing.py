"""The member's briefing (MIP-1072): what changed, what is waiting, what to do next.

One call that answers "catch me up" and "plan my day" for the person asking, computed
LIVE from what already exists. Nothing is recorded to make it work (the ledger is
Phase 3), except one stamp per member and team: when they last asked, so "since my last
briefing" means something. The briefing is not stored, and the calendar is read from
Google each time and never kept.

**Access is the caller's own, section by section.** Where an existing tool already
answers a part (mentions, the inbox, blockers, to-dos, the calendar) the briefing CALLS
that tool, so the rule is the one rule and cannot drift. The new part, "what others
changed on my things", reads only items the caller may open (`LabelAccess` for tasks,
`readable_chats_qs` for chat, ownership for notes), and other people's activity appears
only on those.

**Lines, not dots** (Ken, 2026-11-06). What changed is grouped by ITEM and read in time
order: a comment, a reply, a status change and a merged PR on one task are one story, told
in sequence, and the items with the most going on come first. How much happened and what
kind (a comment or a reply counts for more than a field change) is what ranks the
suggestions; priority and due date are weak hints, because they go stale.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from origin.models.chat.unified_models import Message
from origin.models.common.briefing_models import MemberBriefingState
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools.base import (
    ToolContext,
    ToolError,
    wrap_workspace_content,
)
from origin.search_engine.agent.tools.get_my_blockers import GET_MY_BLOCKERS
from origin.search_engine.agent.tools.list_calendar_events import LIST_CALENDAR_EVENTS
from origin.search_engine.agent.tools.list_my_inbox import LIST_MY_INBOX
from origin.search_engine.agent.tools.list_my_mentions import LIST_MY_MENTIONS
from origin.search_engine.agent.tools.list_today_todos import LIST_TODAY_TODOS
from origin.search_engine.agent.tools.list_uncompleted_todos import LIST_UNCOMPLETED_TODOS
from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL
from origin.services import worklog
from origin.services.channel_scope import readable_chats_qs
from origin.services.user_time import zone_for_user_id

WINDOWS = ("since_last", "today", "week")

# A second call this soon after the last one repeats its window and moves nothing, so a
# retry, or a second ask in the same conversation, never comes back empty.
REUSE_WINDOW = timedelta(hours=2)
# The first briefing, and the window when the stamp is older than we trust.
FIRST_WINDOW = timedelta(hours=24)
WEEK = timedelta(days=7)
# `since` can reach back at most this far: it bounds what one call can read.
MAX_LOOKBACK = timedelta(days=31)
STALE_AFTER = timedelta(days=7)
MENTION_LOOKBACK = timedelta(days=7)

# The briefing is read by a model with a budget (about 8k tokens), so it is shaped to fit:
# the busiest items tell their story in detail, the rest get the latest word, and the
# counts say how much was left out.
MAX_CHANGED_ITEMS = 15
HOT_ITEMS = 5
EVENTS_HOT = 6
EVENTS_REST = 2
MAX_LIST = 8
MAX_TODOS = 15
WORKLOG_ENTRIES = 5
WORKLOG_TEXT_CHARS = 240
EVENT_POOL = 600
SNIPPET_CHARS = 140
SUGGESTIONS = 3

_ACTIVE_TASK_EXCLUDE = ["Closed", "Deleted"]
_COMMENT_ACTIONS = (
    TaskActivityActionType.COMMENT_ADDED,
    TaskActivityActionType.COMMENT_EDITED,
    TaskActivityActionType.COMMENT_DELETED,
)
_PR_ACTIONS = (
    TaskActivityActionType.PR_LINKED,
    TaskActivityActionType.PR_COMMENT_ADDED,
    TaskActivityActionType.PR_UNLINKED,
)

# How much one event says about an item's heat: a person writing to you counts for more
# than a field changing. Weights, not metadata.
HEAT = {
    "comment": 3,
    "reply": 3,
    "assigned_to_you": 5,
    "status": 2,
    "due_date": 2,
    "assignee": 2,
    "pr": 2,
    "note_edit": 2,
    "other": 1,
}
_KIND_OF_ACTION = {
    TaskActivityActionType.STATUS: "status",
    TaskActivityActionType.DUE_DATE: "due_date",
    TaskActivityActionType.ASSIGNEE: "assignee",
    TaskActivityActionType.PR_LINKED: "pr",
    TaskActivityActionType.PR_COMMENT_ADDED: "pr",
    TaskActivityActionType.PR_UNLINKED: "pr",
}
_FIELD_LABEL = {
    TaskActivityActionType.TITLE: "title",
    TaskActivityActionType.STATUS: "status",
    TaskActivityActionType.PRIORITY: "priority",
    TaskActivityActionType.EFFORT: "effort",
    TaskActivityActionType.ASSIGNEE: "assignee",
    TaskActivityActionType.REPORTER: "reporter",
    TaskActivityActionType.DUE_DATE: "due date",
    TaskActivityActionType.START_DATE: "start date",
    TaskActivityActionType.MILESTONE_LINK: "milestone",
    TaskActivityActionType.SPRINT_LINK: "sprint",
}


# --------------------------------------------------------------------------- #
# The window                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class Window:
    name: str
    since: datetime
    until: datetime
    first_briefing: bool = False
    reused: bool = False
    # Moves the "last briefing" stamp. Run only after the briefing is built, so a call
    # that fails does not use up the window.
    commit: Callable[[], None] | None = field(default=None, repr=False)


def _parse_since(value: Any, user_id: str, now: datetime) -> datetime:
    if not isinstance(value, str):
        raise ToolError("`since` must be an ISO date-time, such as 2026-11-06T09:00.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(
            f"`since` must be an ISO date-time, such as 2026-11-06T09:00 (got {value!r})."
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone_for_user_id(user_id))
    if parsed > now:
        raise ToolError("`since` is in the future.")
    if parsed < now - MAX_LOOKBACK:
        raise ToolError(f"`since` is more than {MAX_LOOKBACK.days} days back.")
    return parsed


def _move_stamp(ctx: ToolContext, expected_last: datetime | None, now: datetime) -> None:
    """Record this briefing, unless a concurrent call has already done it."""
    with transaction.atomic():
        state, _ = MemberBriefingState.objects.select_for_update().get_or_create(
            user_id=ctx.user_id, team_id=ctx.team_id
        )
        if state.last_briefing_at != expected_last:
            return
        state.previous_briefing_at = state.last_briefing_at
        state.last_briefing_at = now
        state.save(update_fields=["previous_briefing_at", "last_briefing_at", "ts_updated_at"])


def resolve_window(ctx: ToolContext, window: Any, since: Any, now: datetime, zone) -> Window:
    name = window if window not in (None, "") else "since_last"
    if name not in WINDOWS:
        raise ToolError(f"`window` must be one of {', '.join(WINDOWS)} (got {window!r}).")
    if since not in (None, ""):
        return Window("custom", _parse_since(since, ctx.user_id, now), now)
    if name == "today":
        midnight = datetime.combine(now.astimezone(zone).date(), datetime.min.time(), tzinfo=zone)
        return Window("today", midnight, now)
    if name == "week":
        return Window("week", now - WEEK, now)

    state = MemberBriefingState.objects.filter(user_id=ctx.user_id, team_id=ctx.team_id).first()
    last = state.last_briefing_at if state else None
    if last is None:
        return Window(
            "since_last",
            now - FIRST_WINDOW,
            now,
            first_briefing=True,
            commit=lambda: _move_stamp(ctx, None, now),
        )
    if now - last < REUSE_WINDOW:
        return Window(
            "since_last",
            state.previous_briefing_at or (last - FIRST_WINDOW),
            now,
            reused=True,
        )
    return Window("since_last", last, now, commit=lambda: _move_stamp(ctx, last, now))


# --------------------------------------------------------------------------- #
# People                                                                       #
# --------------------------------------------------------------------------- #


class _Names:
    """Usernames for the people in a briefing, asked for ONCE at the end, through the rule
    `get_team_members` applies: a name is only given for someone the caller may see.

    Every section says who it needs first (`put`); `fill` then looks them all up in one
    query and writes the names in. Nothing is looked up early, because a section built
    later could name someone the first lookup did not ask about. Someone the caller may
    not see stays "someone": a name is not given away."""

    def __init__(self, ctx: ToolContext) -> None:
        self._access = LabelAccess(ctx)
        self._wanted: set[str] = set()
        self._slots: list[tuple[dict[str, Any], str, str]] = []

    def put(self, holder: dict[str, Any], key: str, raw: Any, fallback: str = "someone") -> None:
        holder[key] = fallback
        if raw:
            self._wanted.add(str(raw))
            self._slots.append((holder, key, str(raw)))

    def fill(self) -> None:
        names = self._access.usernames(self._wanted) if self._wanted else {}
        for holder, key, raw in self._slots:
            if raw in names:
                holder[key] = wrap_workspace_content(names[raw])


def _short(value: Any, limit: int = 60) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _snippet(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= SNIPPET_CHARS else text[: SNIPPET_CHARS - 1] + "…"


# --------------------------------------------------------------------------- #
# What others changed on my things                                             #
# --------------------------------------------------------------------------- #


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _activity_event(row: TaskActivity, me: str, names: _Names) -> dict[str, Any]:
    action = row.action_type
    kind = _KIND_OF_ACTION.get(action, "other")
    label = _FIELD_LABEL.get(action)
    old, new = row.old_value, row.new_value
    if action == TaskActivityActionType.ASSIGNEE:
        if str(new) == me:
            kind = "assigned_to_you"
            summary = "assigned this to you"
        else:
            summary = "changed the assignee"
    elif label and (old is not None or new is not None):
        summary = f"{label}: {_short(old) or '—'} → {_short(new) or '—'}"
    elif action == TaskActivityActionType.CREATED:
        summary = "created it"
    elif action == TaskActivityActionType.DESCRIPTION:
        summary = "edited the description"
    elif action == TaskActivityActionType.PR_LINKED:
        title = (row.metadata or {}).get("pr_title") or (row.metadata or {}).get("title")
        summary = "linked a PR" + (f": {_short(title)}" if title else "")
    elif action == TaskActivityActionType.PR_COMMENT_ADDED:
        summary = "commented on the PR"
    elif action == TaskActivityActionType.PR_UNLINKED:
        summary = "unlinked a PR"
    elif action == TaskActivityActionType.CUSTOM_FIELD:
        summary = f"changed {_short((row.metadata or {}).get('field_name') or 'a custom field')}"
    else:
        summary = row.get_action_type_display().lower()
    if row.actor_id:
        actor, label_for_none = row.actor_id, None
    else:
        actor, label_for_none = None, ("GitHub" if action in _PR_ACTIONS else "system")
    return {
        "at": row.ts_created_at,
        "kind": kind,
        "_actor": actor,
        "_label": label_for_none,
        "summary": summary,
    }


def _heat_of(events: list[dict[str, Any]]) -> int:
    return sum(HEAT.get(e["kind"], 1) for e in events)


def _changed_tasks(
    ctx: ToolContext, win: Window, project_id: int | None, names: _Names
) -> list[dict[str, Any]]:
    me = ctx.user_id
    mine = TaskMaster.objects.filter(team_id=ctx.team_id, is_deleted=False).filter(
        Q(assignee_id=me)
        | Q(reporter_id=me)
        | Q(collaborators__id=me)
        | Q(task_comments__sender_id=me, task_comments__is_deleted=False)
    )
    if project_id is not None:
        mine = mine.filter(project_id=project_id)
    mine_ids = mine.values("task_id")

    activities = list(
        TaskActivity.objects.filter(
            task_id__in=mine_ids,
            team_id=ctx.team_id,
            ts_created_at__gte=win.since,
            ts_created_at__lte=win.until,
        )
        .exclude(actor_id=me)
        .exclude(action_type__in=_COMMENT_ACTIONS)
        # Nobody to credit for "created it": automation and imports, not news.
        .exclude(actor__isnull=True, action_type=TaskActivityActionType.CREATED)
        .order_by("-ts_created_at")[:EVENT_POOL]
    )
    comments = list(
        TaskComments.objects.filter(
            task_id__in=mine_ids,
            ts_sent_at__gte=win.since,
            ts_sent_at__lte=win.until,
            is_deleted=False,
        )
        .exclude(sender_id=me)
        .order_by("-ts_sent_at")[:EVENT_POOL]
    )
    candidate_ids = {a.task_id for a in activities} | {c.task_id for c in comments}
    readable = LabelAccess(ctx).tasks(candidate_ids)

    from origin.search_engine.text_extraction import extract_text  # noqa: PLC0415

    by_task: dict[int, list[dict[str, Any]]] = {}
    for row in activities:
        if row.task_id in readable:
            by_task.setdefault(row.task_id, []).append(_activity_event(row, me, names))
    for c in comments:
        if c.task_id in readable:
            by_task.setdefault(c.task_id, []).append(
                {
                    "at": c.ts_sent_at,
                    "kind": "comment",
                    "_actor": c.sender_id,
                    "summary": "commented",
                    "_text": _snippet(extract_text(c.comment_body)),
                }
            )
    items = []
    for task_id, events in by_task.items():
        task = readable[task_id]
        events.sort(key=lambda e: e["at"])
        items.append(
            {
                "type": "task",
                "task_id": task_id,
                "display_id": task.display_id,
                "title": wrap_workspace_content(task.title or ""),
                "status": task.status,
                "project": task.project.project_name if task.project_id else None,
                "assigned_to_me": str(task.assignee_id) == me,
                "_events": events,
            }
        )
    return items


def _changed_threads(ctx: ToolContext, win: Window, names: _Names) -> list[dict[str, Any]]:
    """Replies from other people in threads the caller started or took part in, in chats
    the caller may read (live membership, the rule of `fetch_chat_thread`)."""
    me = ctx.user_id
    readable_chat_ids = readable_chats_qs(ctx.team_id, me).values("id")
    mine = Message.objects.filter(
        sender_id=me, deleted_at__isnull=True, channel_id__in=readable_chat_ids
    )
    my_roots = mine.filter(thread_root__isnull=True, is_thread_reply=False).values("id")
    replied_roots = mine.filter(is_thread_reply=True).values("thread_root_id")
    replies = list(
        Message.objects.filter(
            is_thread_reply=True,
            deleted_at__isnull=True,
            channel_id__in=readable_chat_ids,
            ts_sent_at__gte=win.since,
            ts_sent_at__lte=win.until,
        )
        .filter(Q(thread_root_id__in=my_roots) | Q(thread_root_id__in=replied_roots))
        .exclude(sender_id=me)
        .select_related("channel")
        .order_by("-ts_sent_at")[:EVENT_POOL]
    )
    threads: dict[Any, dict[str, Any]] = {}
    for m in replies:
        thread = threads.setdefault(
            m.thread_root_id,
            {
                "type": "thread",
                "thread_id": str(m.thread_root_id),
                "chat_id": str(m.channel_id),
                "chat_type": CHAT_TYPE_LABEL.get(m.channel.kind),
                "title": wrap_workspace_content(m.channel.title or ""),
                "_events": [],
            },
        )
        thread["_events"].append(
            {
                "at": m.ts_sent_at,
                "kind": "reply",
                "_actor": m.sender_id,
                "summary": "replied",
                "_text": _snippet(m.body_text),
            }
        )
    for thread in threads.values():
        thread["_events"].sort(key=lambda e: e["at"])
    return list(threads.values())


_NOTE_KIND = {1: "personal", 2: "task", 3: "chat"}


def _changed_notes(ctx: ToolContext, win: Window, names: _Names) -> list[dict[str, Any]]:
    """Edits by other people to notes the caller owns."""
    me, team = ctx.user_id, ctx.team_id
    owned = {
        1: PersonalNoteMaster.objects.filter(owner_id=me, team_id=team),
        2: TaskNoteMaster.objects.filter(owner_id=me, team_id=team),
        3: ChatNoteMaster.objects.filter(owner_id=me, team_id=team),
    }
    scope = Q()
    for note_type, qs in owned.items():
        scope |= Q(note_type=note_type, note_id__in=qs.values("note_id"))
    versions = list(
        NoteVersionMaster.objects.filter(
            scope, team_id=team, ts_updated_at__gte=win.since, ts_updated_at__lte=win.until
        )
        .exclude(editor_id=me)
        .order_by("-ts_updated_at")[:EVENT_POOL]
    )
    notes: dict[tuple[int, int], dict[str, Any]] = {}
    for v in versions:
        key = (v.note_type, v.note_id)
        note = notes.setdefault(
            key,
            {
                "type": "note",
                "note_type": _NOTE_KIND[v.note_type],
                "note_id": v.note_id,
                "title": wrap_workspace_content(v.title or ""),
                "_events": [],
            },
        )
        note["_events"].append(
            {
                "at": v.ts_updated_at,
                "kind": "note_edit",
                "_actor": v.editor_id,
                "summary": (
                    "restored an earlier version"
                    if v.restored_from_version_no
                    else "edited the note"
                ),
            }
        )
    for note in notes.values():
        note["_events"].sort(key=lambda e: e["at"])
    return list(notes.values())


def _finish_items(items: list[dict[str, Any]], names: _Names) -> list[dict[str, Any]]:
    """Heat, order and cap: the items with the most going on first, each as a story in
    time order. The first few tell it in detail, the rest give the latest word."""
    for item in items:
        events = item.pop("_events")
        item["heat"] = _heat_of(events)
        item["events_total"] = len(events)
        item["latest_at"] = events[-1]["at"]
        item["_kinds"] = [e["kind"] for e in events]
        item["_all_events"] = events
    items.sort(key=lambda i: (-i["heat"], -i["latest_at"].timestamp()))
    items = items[:MAX_CHANGED_ITEMS]
    for rank, item in enumerate(items):
        shown = item.pop("_all_events")[-(EVENTS_HOT if rank < HOT_ITEMS else EVENTS_REST) :]
        item["events"] = []
        for e in shown:
            event = {
                "at": e["at"].isoformat(),
                "what": e["kind"] if e["kind"] != "other" else "change",
                "summary": e["summary"],
            }
            if e.get("_text"):
                event["text"] = wrap_workspace_content(e["_text"])
            if e.get("_label"):
                event["by"] = e["_label"]
            else:
                names.put(event, "by", e["_actor"])
            item["events"].append(event)
    return items


def changed_for_me(
    ctx: ToolContext, win: Window, project_id: int | None, names: _Names
) -> tuple[list[dict[str, Any]], int]:
    """What other people did on the caller's things in the window: `(items, total)`.

    With a project filter only tasks of that project are looked at: chats and notes
    belong to no project."""
    items = _changed_tasks(ctx, win, project_id, names)
    if project_id is None:
        items += _changed_threads(ctx, win, names)
        items += _changed_notes(ctx, win, names)
    total = len(items)
    return _finish_items(items, names), total


# --------------------------------------------------------------------------- #
# My tasks, agenda, waiting, stale                                             #
# --------------------------------------------------------------------------- #


def _check_project(ctx: ToolContext, project_id: Any) -> int | None:
    if project_id in (None, ""):
        return None
    try:
        number = int(project_id)
    except (TypeError, ValueError):
        raise ToolError(f"`project_id` must be a number (got {project_id!r}).")
    if not ProjectMembers.objects.filter(
        attendee_id=ctx.user_id,
        project_id=number,
        project__team_id=ctx.team_id,
        project__is_deleted=False,
    ).exists():
        raise ToolError(f"Project {number} not found.")
    return number


def _my_active_tasks(ctx: ToolContext, project_id: int | None = None):
    """The caller's open tasks: the scope of `get_my_focus_tasks`, so the briefing and
    "what should I work on?" talk about the same set."""
    member_projects = ProjectMembers.objects.filter(
        attendee_id=ctx.user_id, project__team_id=ctx.team_id, project__is_deleted=False
    ).values("project_id")
    qs = (
        TaskMaster.objects.filter(
            team_id=ctx.team_id,
            assignee_id=ctx.user_id,
            project_id__in=member_projects,
            is_deleted=False,
            is_init_task=False,
        )
        .exclude(status__in=_ACTIVE_TASK_EXCLUDE)
        .select_related("project")
    )
    return qs.filter(project_id=project_id) if project_id is not None else qs


def _task_row(task: TaskMaster, today: date) -> dict[str, Any]:
    row = {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "title": wrap_workspace_content(task.title or ""),
        "status": task.status,
    }
    if task.priority:
        row["priority"] = task.priority
    if task.due_date:
        row["due_date"] = task.due_date.isoformat()
        row["days_until_due"] = (task.due_date - today).days
    if task.project_id:
        row["project"] = task.project.project_name
    return row


def _calendar_today(ctx: ToolContext, now: datetime, zone) -> dict[str, Any]:
    """Today's events, read from Google now. Never stored."""
    start = datetime.combine(now.astimezone(zone).date(), datetime.min.time(), tzinfo=zone)
    try:
        out = LIST_CALENDAR_EVENTS.run(
            {
                "from": start.isoformat(),
                "to": (start + timedelta(days=1)).isoformat(),
                "max_results": 20,
            },
            ctx,
        )
    except ToolError as exc:
        return {"events": None, "note": str(exc)}
    except Exception:  # noqa: BLE001 - Google being down must not cost the member their briefing
        return {"events": None, "note": "The calendar could not be read right now."}
    return {"events": out.get("events", [])}


def agenda(ctx: ToolContext, now: datetime, zone, project_id: int | None) -> dict[str, Any]:
    today = now.astimezone(zone).date()
    due = list(
        _my_active_tasks(ctx, project_id)
        .filter(due_date__lte=today)
        .order_by("due_date", "task_id")
    )
    overdue = [t for t in due if t.due_date < today]
    due_today = [t for t in due if t.due_date == today]
    todos = LIST_TODAY_TODOS.run({}, ctx)
    earlier = [
        i
        for i in LIST_UNCOMPLETED_TODOS.run({"date_range_days": 14}, ctx).get("items", [])
        if i.get("local_date") and i["local_date"] < today.isoformat()
    ]
    return {
        "local_date": today.isoformat(),
        "due_today": [_task_row(t, today) for t in due_today[:MAX_LIST]],
        "due_today_count": len(due_today),
        "overdue": [_task_row(t, today) for t in overdue[:MAX_LIST]],
        "overdue_count": len(overdue),
        "todos_today": [
            {**i, "title": wrap_workspace_content(i.get("title") or "")}
            for i in todos.get("items", [])[:MAX_TODOS]
        ],
        "todos_unfinished_earlier": [
            {**i, "title": wrap_workspace_content(i.get("title") or "")}
            for i in earlier[:MAX_TODOS]
        ],
        "calendar": _calendar_today(ctx, now, zone),
    }


def _unanswered_mentions(ctx: ToolContext, now: datetime) -> list[dict[str, Any]]:
    """@mentions of the caller in chat that the caller has not replied to since.

    A reply is any message of theirs in the same chat and the same thread (or, for a
    mention outside a thread, the same chat, top level) sent after the mention."""
    mentions = LIST_MY_MENTIONS.run(
        {"since": (now - MENTION_LOOKBACK).isoformat(), "limit": 30}, ctx
    ).get("mentions", [])
    if not mentions:
        return []
    chat_ids = {m["chat_id"] for m in mentions}
    oldest = min(m["ts"] for m in mentions if m.get("ts"))
    mine = list(
        Message.objects.filter(
            sender_id=ctx.user_id,
            deleted_at__isnull=True,
            channel_id__in=chat_ids,
            ts_sent_at__gte=oldest,
        ).values("channel_id", "thread_root_id", "ts_sent_at")
    )

    def answered(mention: dict[str, Any]) -> bool:
        thread = mention.get("thread_id")
        for m in mine:
            if str(m["channel_id"]) != mention["chat_id"]:
                continue
            if (str(m["thread_root_id"]) if m["thread_root_id"] else None) != thread:
                continue
            if m["ts_sent_at"].isoformat() > mention["ts"]:
                return True
        return False

    return [
        {
            "message_id": m["message_id"],
            "chat_id": m["chat_id"],
            "thread_id": m.get("thread_id"),
            "chat_type": m.get("chat_type_label"),
            "chat_title": m.get("channel_title"),
            "from": m.get("sender_username") or "someone",
            "text": m.get("text"),
            "at": m.get("ts"),
        }
        for m in mentions
        if not answered(m)
    ][:MAX_LIST]


def waiting_on_me(ctx: ToolContext, now: datetime, names: _Names) -> dict[str, Any]:
    inbox = LIST_MY_INBOX.run({"category": "requests", "limit": MAX_LIST}, ctx).get("items", [])
    requests = []
    for item in inbox:
        if item.get("request_status") != "pending":
            continue
        body = item.get("item_body")
        request = {
            "item_id": item["item_id"],
            "type": item.get("item_type_label"),
            "at": item.get("ts_created_at"),
            "target": item.get("target"),
            "text": wrap_workspace_content(_snippet(body))
            if isinstance(body, str) and body
            else None,
        }
        names.put(request, "from", item.get("sender_id"))
        requests.append(request)
    return {"requests": requests, "mentions": _unanswered_mentions(ctx, now)}


def waiting_on_others(ctx: ToolContext, names: _Names) -> dict[str, Any]:
    blockers = GET_MY_BLOCKERS.run({}, ctx).get("blocked_on_me", [])
    blocked = []
    for row in blockers[:MAX_LIST]:
        waiting_for = []
        for e in row.get("blocked_by", [])[:3]:
            edge = {
                "display_id": e["display_id"],
                "title": wrap_workspace_content(e.get("title") or ""),
                "status": e.get("status"),
            }
            names.put(edge, "assignee", e.get("assignee_id"), fallback="nobody yet")
            waiting_for.append(edge)
        blocked.append(
            {
                "task_id": row["task_id"],
                "display_id": row["display_id"],
                "title": wrap_workspace_content(row.get("title") or ""),
                "waiting_for": waiting_for,
            }
        )
    return {
        "blocked_tasks": blocked,
        "worklog_waiting": [
            worklog.compact_item(i) for i in worklog.open_items(ctx, ("waiting",), MAX_LIST)
        ],
    }


def stale_mine(
    ctx: ToolContext, now: datetime, zone, project_id: int | None
) -> list[dict[str, Any]]:
    """My open tasks nobody has touched for a week. "Pending" is parked on purpose, and
    "Blocked" is already under waiting, so neither is nagged about here."""
    today = now.astimezone(zone).date()
    rows = (
        _my_active_tasks(ctx, project_id)
        .exclude(status__in=["Pending", "Blocked"])
        .filter(ts_updated_at__lte=now - STALE_AFTER)
        .order_by("ts_updated_at")[:MAX_LIST]
    )
    return [{**_task_row(t, today), "days_idle": (now - t.ts_updated_at).days} for t in rows]


def _compact_entry(entry: dict[str, Any]) -> dict[str, Any]:
    out = {
        "entry_id": entry["entry_id"],
        "local_date": entry["local_date"],
        "text": entry["text"],
        "items": [worklog.compact_item(i) for i in entry["items"]],
    }
    if entry.get("text_truncated"):
        out["text_truncated"] = True
    return out


def worklog_section(ctx: ToolContext) -> dict[str, Any]:
    today = worklog.list_entries(
        ctx, window="today", limit=WORKLOG_ENTRIES, max_text=WORKLOG_TEXT_CHARS
    )
    yesterday = worklog.list_entries(
        ctx, window="yesterday", limit=WORKLOG_ENTRIES, max_text=WORKLOG_TEXT_CHARS
    )
    return {
        "today": [_compact_entry(e) for e in today["entries"]],
        "yesterday": [_compact_entry(e) for e in yesterday["entries"]],
        "open_next": [
            worklog.compact_item(i) for i in worklog.open_items(ctx, ("next",), MAX_LIST)
        ],
    }


# --------------------------------------------------------------------------- #
# What to do next                                                              #
# --------------------------------------------------------------------------- #

_PRIORITY_HINT = {"Critical": 6, "High": 4, "Normal": 2}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _activity_reason(kinds: list[str], since_text: str) -> str:
    comments = sum(1 for k in kinds if k in ("comment", "reply"))
    others = len(kinds) - comments
    parts = []
    if comments:
        parts.append(_plural(comments, "comment"))
    if others:
        parts.append(_plural(others, "change"))
    return f"{' and '.join(parts)} from others since {since_text}"


def suggested_next(
    ctx: ToolContext,
    now: datetime,
    zone,
    win: Window,
    items: list[dict[str, Any]],
    project_id: int | None,
) -> list[dict[str, Any]]:
    """The top few things to look at, with why.

    Ranked by what is going on, not by what the fields say: how much happened on a task
    and what kind (comments count for more than field changes), whether it is due or
    overdue, and only then its priority, as a weak hint. Every reason shown is a signal
    that really fired, so the member can disagree with it."""
    today = now.astimezone(zone).date()
    activity = {i["task_id"]: i for i in items if i["type"] == "task"}
    pool = list(_my_active_tasks(ctx, project_id).order_by("-ts_updated_at")[:100])
    pooled = {t.task_id for t in pool}
    missing = [tid for tid, i in activity.items() if i["assigned_to_me"] and tid not in pooled]
    if missing:
        pool += list(_my_active_tasks(ctx, project_id).filter(task_id__in=missing))

    since_text = win.since.astimezone(zone).strftime("%b %-d %H:%M")
    scored = []
    for task in pool:
        score, reasons = 0, []
        if task.due_date is not None:
            days = (task.due_date - today).days
            if days < 0:
                score += 30 + min(-days, 10)
                reasons.append(f"overdue by {_plural(-days, 'day')}")
            elif days == 0:
                score += 25
                reasons.append("due today")
            elif days == 1:
                score += 8
                reasons.append("due tomorrow")
        seen = activity.get(task.task_id)
        if seen:
            score += min(seen["heat"] * 4, 40)
            reasons.append(_activity_reason(seen["_kinds"], since_text))
        if task.status == "Blocked":
            score -= 10
            reasons.append("currently Blocked: clear what blocks it first")
        hint = _PRIORITY_HINT.get(task.priority or "", 0)
        if hint and score > 0:
            score += hint
            reasons.append(f"priority {task.priority}")
        if score > 0:
            scored.append((score, task, reasons))
    scored.sort(key=lambda s: (-s[0], -(s[1].ts_updated_at.timestamp())))
    return [
        {**_task_row(task, today), "score": score, "reasons": reasons}
        for score, task, reasons in scored[:SUGGESTIONS]
    ]


# --------------------------------------------------------------------------- #
# The briefing                                                                 #
# --------------------------------------------------------------------------- #


def build_briefing(
    ctx: ToolContext,
    *,
    window: Any = None,
    since: Any = None,
    project_id: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or timezone.now()
    zone = zone_for_user_id(ctx.user_id)
    project = _check_project(ctx, project_id)
    win = resolve_window(ctx, window, since, now, zone)
    names = _Names(ctx)

    items, total_changed = changed_for_me(ctx, win, project, names)
    me = CustomUser.objects.filter(id=ctx.user_id).first()
    team = TeamMaster.objects.filter(team_id=ctx.team_id).first()
    agenda_section = agenda(ctx, now, zone, project)
    waiting_me = waiting_on_me(ctx, now, names)
    waiting_others = waiting_on_others(ctx, names)
    suggestions = suggested_next(ctx, now, zone, win, items, project)

    # People are named in one batch, after every section has said who it needs.
    for item in items:
        item.pop("_kinds", None)
        item["latest_at"] = item["latest_at"].isoformat()

    payload = {
        "window": {
            "name": win.name,
            "since": win.since.isoformat(),
            "until": win.until.isoformat(),
            "first_briefing": win.first_briefing,
            "reused_last_window": win.reused,
            "time_zone": zone.key,
        },
        "me": {
            "user_id": ctx.user_id,
            "username": wrap_workspace_content(me.username) if me else None,
            "role": wrap_workspace_content(me.role) if me and me.role else None,
            "team_id": ctx.team_id,
            "team": wrap_workspace_content(team.team_name) if team else None,
            "local_time": now.astimezone(zone).isoformat(timespec="minutes"),
        },
        "agenda": agenda_section,
        "changed_for_me": {
            "items": items,
            "total": total_changed,
            "truncated": total_changed > len(items),
            "project_filter": project,
        },
        "waiting_on_me": waiting_me,
        "waiting_on_others": waiting_others,
        "stale_mine": stale_mine(ctx, now, zone, project),
        "worklog": worklog_section(ctx),
        "suggested_next": suggestions,
    }
    names.fill()
    asked = waiting_me["requests"], waiting_me["mentions"]
    payload["__summary__"] = (
        f"Briefing since {win.since.astimezone(zone).strftime('%b %-d %H:%M')}: "
        f"{total_changed} item(s) changed, "
        f"{len(asked[0]) + len(asked[1])} waiting on you, "
        f"{agenda_section['overdue_count']} overdue."
    )
    if win.commit:
        win.commit()
    return payload
