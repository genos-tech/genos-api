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

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from origin.models.chat.todo_models import ToDoItem
from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    Message,
    ReadCursor,
)
from origin.models.common.briefing_models import MemberBriefingState
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.acl import may_read_note, task_note_acl_user_ids
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
from origin.services import member_events
from origin.services.channel_scope import chat_note_member_q, readable_chats_qs
from origin.services.user_time import zone_for_user_id

log = logging.getLogger(__name__)

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
# How many mention rows are looked at before access and "answered" are decided. A member
# is mentioned a handful of times in a week; this bounds a flood, newest first.
MENTION_POOL = 60

# The briefing is read by a model with a budget (about 8k tokens), so it is shaped to fit:
# the busiest items tell their story in detail, the rest get the latest word, and the
# counts say how much was left out.
MAX_CHANGED_ITEMS = 15
HOT_ITEMS = 5
EVENTS_HOT = 6
EVENTS_REST = 2
MAX_LIST = 8
MAX_TODOS = 15
# Answers in a chat the member wrote in (MIP-1290): at most this many chats, each told as ONE
# item (a count and the latest few words), from the newest `ANSWER_POOL` chats the member wrote
# in during `MENTION_LOOKBACK`. A chatty chat adds to its count, never to its length.
MAX_ANSWER_CHATS = 4
ANSWER_SNIPPETS = 3
ANSWER_POOL = 40
EVENT_POOL = 600
SNIPPET_CHARS = 140
SUGGESTIONS = 3
# What the member did: items per day, events told per item, chats and to-dos listed.
DID_ITEMS = {"today": 5, "yesterday": 3}
DID_EVENTS = {"today": 3, "yesterday": 1}
DID_CHATS = 3
DID_TODOS = 6
# What the member's AI app opened or changed that is saved nowhere else (MIP-1143): a few
# lines, resolved from a bounded pool of the newest markers.
DID_LOOKED = 5
LOOKED_POOL = 40
LOOKED_CHECKS = 15

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
        # No actor: the GitHub webhook or the system (a merge closing a task the assignee
        # asked to be closed that way). Not a person, so not "what others did" (MIP-1142).
        "automated": row.actor_id is None,
        "summary": summary,
    }


def activity_event(row: TaskActivity, me: str) -> dict[str, Any]:
    """One task activity row in the briefing's words ("status: Open → WIP", "linked a PR"),
    for the scheduled reports (MIP-1152): `summary`, who (`_actor` or the automation's
    `_label`) and `at`."""
    return _activity_event(row, me, None)  # type: ignore[arg-type]  # `names` is not read


def _heat_of(events: list[dict[str, Any]]) -> int:
    """Heat from what PEOPLE did. Automation echoes (a PR linked, a task closed because its
    PR merged) are consequences, usually of the member's own work, and say nothing about how
    much attention the item is getting."""
    return sum(HEAT.get(e["kind"], 1) for e in events if not e.get("automated"))


def _requested_task_ids(
    ctx: ToolContext, win: Window, project_id: int | None, held_ids: Any
) -> set[int]:
    """Tasks made from a chat thread the member STARTED, whose status changed in the window.

    The person who asked for something in a thread and the PM who made a task of it are
    two people; the task holds only the PM. Without this the requester is never told the
    task they asked for was closed (MIP-1283). Only the tasks with a status change in the
    window are looked at, so the work is bounded by the window, not by the member's history.
    The starter is the sender of the thread's root message (`TaskMaster.thread_id`), who
    has not deleted it. Whether the member may open the task is asked later, with every
    other candidate (`LabelAccess.tasks`)."""
    me = ctx.user_id
    status_rows = TaskActivity.objects.filter(
        team_id=ctx.team_id,
        action_type=TaskActivityActionType.STATUS,
        ts_created_at__gte=win.since,
        ts_created_at__lte=win.until,
    )
    tasks = (
        TaskMaster.objects.filter(
            team_id=ctx.team_id,
            is_deleted=False,
            is_init_task=False,
            task_id__in=status_rows.values("task_id"),
        )
        .exclude(thread_id__isnull=True)
        .exclude(task_id__in=held_ids)
    )
    if project_id is not None:
        tasks = tasks.filter(project_id=project_id)
    by_thread: dict[uuid.UUID, list[int]] = {}
    for task_id, thread_id in tasks.values_list("task_id", "thread_id")[:EVENT_POOL]:
        try:
            by_thread.setdefault(uuid.UUID(str(thread_id)), []).append(task_id)
        except ValueError:
            continue  # a legacy numeric thread id names no message here
    if not by_thread:
        return set()
    started = Message.objects.filter(
        id__in=list(by_thread),
        sender_id=me,
        is_thread_reply=False,
        deleted_at__isnull=True,
    ).values_list("id", flat=True)
    return {task_id for root_id in started for task_id in by_thread[root_id]}


def _changed_tasks(
    ctx: ToolContext, win: Window, project_id: int | None, names: _Names, automated_only: list
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
    # Tasks made from a thread the member started: they hear the status changes (the close
    # above all), not the work, which stays with the people who hold the task.
    requested = _requested_task_ids(ctx, win, project_id, mine_ids)

    activities = list(
        TaskActivity.objects.filter(
            Q(task_id__in=mine_ids)
            | Q(task_id__in=requested, action_type=TaskActivityActionType.STATUS),
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
        if all(e.get("automated") for e in events):
            # Only automation touched it: a PR linked, a merge closing it. Not news from
            # people, and mostly the member's own PRs echoed back. Counted, not listed.
            automated_only.append(task_id)
            continue
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
                **(
                    {"why": "you started the chat thread this task was made from"}
                    if task_id in requested
                    else {}
                ),
                "_events": events,
            }
        )
    return items


def _changed_threads(ctx: ToolContext, win: Window, names: _Names) -> list[dict[str, Any]]:
    """Replies from other people in threads the caller started or took part in, in chats
    the caller may read (live membership, the rule of `fetch_chat_thread`). The mirror of a
    task comment is not a reply: the task item tells it."""
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
        # A task comment is mirrored into the project chat as a thread reply. The task item
        # already tells it (with the commenter's text and the task's status): listing the
        # mirror too would count one comment twice and inflate the heat. `item_lines` skips
        # the mirror the same way.
        .exclude(metadata__has_key="taskCommentId")
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


# Task notes of tasks the member holds (not their own) are read through the note access rule,
# one lookup per note, so the notes checked in one briefing are capped.
HELD_NOTES_CHECKED = 40


def answers_in_chats(
    ctx: ToolContext, now: datetime, project_id: int | None, names: _Names
) -> dict[str, Any]:
    """What other people wrote, at the top level, in chats the caller wrote in and has not
    answered or read since (MIP-1290).

    In a flat chat an answer is an ordinary message: not a thread reply (`_changed_threads`) and
    not an @mention (`waiting_on_me`), so nothing else in the briefing says it came. Per chat the
    caller can read (`readable_chats_qs`: live membership by `chat_member_q`, so a chat they left
    or a project chat they are not on says nothing), the news is the other people's top-level
    messages beyond the caller's own newest top-level message there, and beyond their main
    timeline `ReadCursor` (the rule the app's unread count uses: what they have seen is not news).
    The caller must have written there within `MENTION_LOOKBACK`: a chat they spoke in long ago is
    not a question they are waiting on.

    Capped so a chatty chat cannot take the briefing over: one item per chat, the newest
    `MAX_ANSWER_CHATS` of them, each with its count and its latest `ANSWER_SNIPPETS` messages.
    Task cards and the app's own posts are not answers. Chats belong to no project, so a project
    filter leaves this empty, as it does for threads."""
    empty = {"chats": [], "total": 0, "truncated": False}
    if project_id is not None:
        return empty
    me = ctx.user_id
    readable = readable_chats_qs(ctx.team_id, me).values("id")
    top_level = Message.objects.filter(
        is_thread_reply=False, deleted_at__isnull=True, channel_id__in=readable
    )
    wrote = {
        row["channel_id"]: row
        for row in top_level.filter(sender_id=me, ts_sent_at__gte=now - MENTION_LOOKBACK)
        .values("channel_id")
        .annotate(last_seq=Max("seq"), last_at=Max("ts_sent_at"))
        .order_by("-last_at")[:ANSWER_POOL]
    }
    if not wrote:
        return empty
    floor = {c: row["last_seq"] for c, row in wrote.items()}
    for channel_id, read_seq in ReadCursor.objects.filter(
        user_id=me, channel_id__in=list(wrote), thread_root__isnull=True
    ).values_list("channel_id", "last_read_message__seq"):
        if read_seq is not None and read_seq > floor[channel_id]:
            floor[channel_id] = read_seq

    others = (
        top_level.filter(ts_sent_at__lte=now, task_id__isnull=True)
        .exclude(sender_id=me)
        .exclude(sender__is_system_user=True)
    )
    news = Q()
    for channel_id, above in floor.items():
        news |= Q(channel_id=channel_id, seq__gt=above)
    counted = list(
        others.filter(news)
        .values("channel_id")
        .annotate(n=Count("id"), latest_at=Max("ts_sent_at"))
        .order_by("-latest_at")
    )
    chats = []
    shown = counted[:MAX_ANSWER_CHATS]
    channels = Channel.objects.in_bulk([r["channel_id"] for r in shown])
    for row in shown:
        channel = channels[row["channel_id"]]
        latest = list(
            others.filter(channel_id=channel.id, seq__gt=floor[channel.id]).order_by("-seq")[
                :ANSWER_SNIPPETS
            ]
        )
        latest.reverse()
        said = []
        for m in latest:
            line = {
                "at": m.ts_sent_at.isoformat(),
                "text": wrap_workspace_content(_snippet(m.body_text)),
            }
            names.put(line, "from", m.sender_id)
            said.append(line)
        chats.append(
            {
                "chat_id": str(channel.id),
                "chat_type": CHAT_TYPE_LABEL.get(channel.kind),
                "title": wrap_workspace_content(channel.title or ""),
                "you_wrote_at": wrote[channel.id]["last_at"].isoformat(),
                "count": row["n"],
                "latest_at": row["latest_at"].isoformat(),
                "messages": said,
                "earlier_not_shown": row["n"] - len(said),
            }
        )
    return {"chats": chats, "total": len(counted), "truncated": len(counted) > len(chats)}


def _changed_notes(ctx: ToolContext, win: Window, names: _Names) -> list[dict[str, Any]]:
    """Edits by other people to notes the caller owns, and to the task notes of the tasks the
    caller holds (assignee, reporter or collaborator): the PM who handed a task over hears
    about the summary the developer wrote in the task's note.

    A task note the caller does not own is shown only when the caller can open it now (the
    rule `get_note` applies, per note)."""
    me, team = ctx.user_id, ctx.team_id
    # A task note's author who left its project is a stranger to it (MIP-1511): the notes they
    # wrote and can still open, through the project or a grant (the set rule `_may_read_note`
    # applies to a task note).
    own_task_notes = TaskNoteMaster.objects.filter(owner_id=me, team_id=team).filter(
        Q(project_id__in=ProjectMembers.objects.filter(attendee_id=me).values("project_id"))
        | Q(
            note_id__in=NotePermissionMaster.objects.filter(user_id=me, note_type=2).values(
                "note_id"
            )
        )
    )
    owned = {
        1: PersonalNoteMaster.objects.filter(owner_id=me, team_id=team),
        2: own_task_notes,
        # A chat note has no author exception (MIP-1492): the ones the caller wrote and can
        # still open, through their chat or a grant, by the rule `get_effective_role` applies.
        3: ChatNoteMaster.objects.filter(owner_id=me, team_id=team).filter(
            chat_note_member_q(me)
            | Q(
                note_id__in=NotePermissionMaster.objects.filter(user_id=me, note_type=3).values(
                    "note_id"
                )
            )
        ),
    }
    held_tasks = TaskMaster.objects.filter(team_id=team, is_deleted=False).filter(
        Q(assignee_id=me) | Q(reporter_id=me) | Q(collaborators__id=me)
    )
    held_notes = TaskNoteMaster.objects.filter(
        team_id=team, task_id__in=held_tasks.values("task_id")
    )
    scope = Q()
    for note_type, qs in owned.items():
        scope |= Q(note_type=note_type, note_id__in=qs.values("note_id"))
    scope |= Q(note_type=2, note_id__in=held_notes.values("note_id"))
    versions = list(
        NoteVersionMaster.objects.filter(
            scope, team_id=team, ts_updated_at__gte=win.since, ts_updated_at__lte=win.until
        )
        .exclude(editor_id=me)
        .order_by("-ts_updated_at")[:EVENT_POOL]
    )
    # Task notes the caller does not own (or no longer can open as their author): asked once per
    # note, newest activity first.
    foreign = {
        n.note_id: n
        for n in TaskNoteMaster.objects.filter(
            note_id__in={v.note_id for v in versions if v.note_type == 2}
        ).exclude(note_id__in=own_task_notes.values("note_id"))
    }
    may_read: dict[int, bool] = {}
    notes: dict[tuple[int, int], dict[str, Any]] = {}
    for v in versions:
        if v.note_type == 2 and v.note_id in foreign:
            if v.note_id not in may_read:
                may_read[v.note_id] = len(may_read) < HELD_NOTES_CHECKED and _may_read_note(
                    ctx, 2, foreign[v.note_id]
                )
            if not may_read[v.note_id]:
                continue
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
        item["_kinds"] = [e["kind"] for e in events if not e.get("automated")]
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
            if e.get("automated"):
                event["automated"] = True
            if e.get("_label"):
                event["by"] = e["_label"]
            else:
                names.put(event, "by", e["_actor"])
            item["events"].append(event)
    return items


def changed_for_me(
    ctx: ToolContext, win: Window, project_id: int | None, names: _Names
) -> tuple[list[dict[str, Any]], int, int]:
    """What other people did on the caller's things in the window:
    `(items, total, automated_only)`.

    `automated_only` is how many of the caller's tasks were touched ONLY by automation (a PR
    linked, a merge closing the task): not listed, because nobody did anything, but not
    hidden either.

    With a project filter only tasks of that project are looked at: chats and notes
    belong to no project."""
    automated_only: list[int] = []
    items = _changed_tasks(ctx, win, project_id, names, automated_only)
    if project_id is None:
        items += _changed_threads(ctx, win, names)
        items += _changed_notes(ctx, win, names)
    total = len(items)
    return _finish_items(items, names), total, len(automated_only)


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
    """The caller's open tasks: the scope of `get_my_focus_tasks`, minus the ones that read as
    closed because an ancestor is (MIP-1332).

    A person often closes a parent task or a milestone and never its sub-tasks, meaning "all
    of it is done". `task_ancestry` is the one statement of that rule (the one `get_task` and
    `list_tasks` already show as `effective_status`), so a briefing or a report never nags about
    such a task. The in-app `get_my_focus_tasks` still lists them (what the in-app model sees
    waits for the December eval group)."""
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
    qs = qs.filter(project_id=project_id) if project_id is not None else qs
    from origin.services.task_ancestry import effective_statuses  # noqa: PLC0415

    closed_above = [
        task_id
        for task_id, found in effective_statuses(
            list(qs.values_list("task_id", flat=True)), ctx.team_id
        ).items()
        if found.closed_by_ancestor_id is not None
    ]
    return qs.exclude(task_id__in=closed_above) if closed_above else qs


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
    except Exception as exc:  # noqa: BLE001 - Google being down must not cost the member their briefing
        # MIP-1333: this used to say nothing, so a deployment that could not refresh the Google
        # token (a cron service without the OAuth client variables) looked like a calendar that
        # was merely "down". The type and a short message make the cause findable in the logs.
        log.warning(
            "briefing_calendar_unreadable user=%s error=%s detail=%s",
            ctx.user_id,
            type(exc).__name__,
            str(exc)[:160],
        )
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


def _chat_mentions(ctx: ToolContext, now: datetime) -> list[dict[str, Any]]:
    """@mentions of the caller in chat that the caller has not replied to since.

    Direct @user and @group mentions alike: a group is expanded to its members when the
    message is sent, so each member has a `MessageMention` row. Read through the
    existing tool, so the access rule is its own.

    A reply is any message of theirs in the same chat and the same thread (or, for a
    mention outside a thread, the same chat, top level) sent after the mention."""
    mentions = LIST_MY_MENTIONS.run(
        {"since": (now - MENTION_LOOKBACK).isoformat(), "limit": 30}, ctx
    ).get("mentions", [])
    if not mentions:
        return []
    # A task's card in its project chat (the thread root that carries the task) restates the
    # assignee and the reporter as @mentions. Nobody is asking anything there, and the
    # assignment is already in the task's own history, so it does not wait on anyone
    # (MIP-1141). A reply in the card's thread, and a deliberate @mention of oneself, stay.
    cards = {
        str(pk)
        for pk in Message.objects.filter(
            id__in=[m["message_id"] for m in mentions],
            task_id__isnull=False,
            is_thread_reply=False,
        ).values_list("id", flat=True)
    }
    mentions = [m for m in mentions if m["message_id"] not in cards]
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
            "source": "chat",
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


_NOTE_SURFACES = {6: 1, 7: 2, 8: 3}  # `Activity.surface_type` -> note type
_NOTE_NAMES = {1: "personal", 2: "task", 3: "chat"}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _may_read_note(ctx: ToolContext, note_type: int, note: Any) -> bool:
    """Can the caller open this note now? The rule `get_note` applies, per note family.

    Personal and chat notes ask `may_read_note` (the owner, or a role on the note: an explicit
    grant, a Team Notes folder, a chat note's chat by `channel_scope.chat_note_member_q`): one
    rule with the per-note resolver, where the chat branch used to be the index's set of user
    ids and disagreed with it in four edge cases (MIP-1474). A task note keeps the set rule
    (project members, grants: its author is one of the members, or no longer a reader, MIP-1511),
    which `test_timeline_note_rule` pins equal to it: asking
    `may_read_note` would cost a query more per line, and `test_item_lines_pin` counts them."""
    if str(note.team_id or "") != str(ctx.team_id):
        return False
    if note_type != 2:
        return may_read_note(
            user_id=ctx.user_id,
            note_type_code=note_type,
            note_id=note.note_id,
            owner_id=note.owner_id,
        )
    allowed = task_note_acl_user_ids(
        owner_id=note.owner_id, project_id=note.project_id, note_id=note.note_id
    )
    return str(ctx.user_id) in allowed


def _activity_mentions(
    ctx: ToolContext, now: datetime, names: _Names, skip_messages: set[str]
) -> list[dict[str, Any]]:
    """Mentions `list_my_mentions` cannot see: in a task comment, a task body or a note.

    Every one of them leaves an `Activity(MENTION)` row for the person mentioned, whoever
    wrote it (the app, a connected client, the agent) and whether it named them or a group
    they are in (groups are expanded to members when the text is saved). That row is the
    durable record, so it is the source; the text is not read again.

    Only what the caller can open NOW is shown, by the rule of each kind. "Answered" is
    the simplest thing that can be seen: for a comment or a task body, the caller has
    commented on that task or changed it since; for a note, they have edited it since, or
    opened the feed entry (a reply in a note's own comment thread is not visible from here).
    """
    rows = list(
        Activity.objects.filter(
            recipient_id=ctx.user_id,
            team_id=ctx.team_id,
            activity_type=ActivityType.MENTION,
            ts_created_at__gte=now - MENTION_LOOKBACK,
        )
        .select_related("message", "message__thread_root")
        .order_by("-ts_created_at")[:MENTION_POOL]
    )
    comment_rows, body_rows, note_rows = [], [], []
    for row in rows:
        meta = row.meta if isinstance(row.meta, dict) else {}
        if row.surface_type == 5:
            task_id = _int_or_none(meta.get("taskId"))
            if task_id is not None:
                body_rows.append((row, task_id))
        elif row.surface_type in _NOTE_SURFACES:
            note_id = _int_or_none(meta.get("noteId"))
            if note_id is not None:
                note_rows.append((row, _NOTE_SURFACES[row.surface_type], note_id))
        elif row.message_id and str(row.message_id) not in skip_messages:
            message = row.message
            comment_id = _int_or_none((message.metadata or {}).get("taskCommentId"))
            if comment_id is None or message.deleted_at is not None:
                continue  # an ordinary chat mention: `list_my_mentions` has it
            root = message.thread_root
            task_id = message.task_id or (root.task_id if root is not None else None)
            if task_id is not None:
                comment_rows.append((row, task_id, comment_id))

    entries: list[dict[str, Any]] = []
    if comment_rows or body_rows:
        task_ids = {t for _, t, _c in comment_rows} | {t for _, t in body_rows}
        readable = LabelAccess(ctx).tasks(task_ids)
        oldest = min(r.ts_created_at for r, *_ in comment_rows + body_rows)
        # When the caller last did anything on each task, after the oldest mention.
        touched: dict[int, datetime] = {}
        for task_id, at in TaskComments.objects.filter(
            task_id__in=readable, sender_id=ctx.user_id, is_deleted=False, ts_sent_at__gte=oldest
        ).values_list("task_id", "ts_sent_at"):
            touched[task_id] = max(at, touched.get(task_id, at))
        for task_id, at in TaskActivity.objects.filter(
            task_id__in=readable, actor_id=ctx.user_id, ts_created_at__gte=oldest
        ).values_list("task_id", "ts_created_at"):
            touched[task_id] = max(at, touched.get(task_id, at))

        def open_on(task_id: int, at: datetime) -> Any:
            task = readable.get(task_id)
            return task if task is not None and touched.get(task_id, at) <= at else None

        from origin.search_engine.text_extraction import extract_text  # noqa: PLC0415

        bodies = {
            (c.task_id, c.comment_id): c
            for c in TaskComments.objects.filter(
                task_id__in=readable,
                comment_id__in={c for _r, _t, c in comment_rows},
                is_deleted=False,
            )
        }
        for row, task_id, comment_id in comment_rows:
            task, comment = open_on(task_id, row.ts_created_at), bodies.get((task_id, comment_id))
            if task is None or comment is None:
                continue
            entries.append(
                {
                    "source": "task_comment",
                    "at": row.ts_created_at.isoformat(),
                    "task_id": task_id,
                    "display_id": task.display_id,
                    "title": wrap_workspace_content(task.title or ""),
                    "comment_id": comment_id,
                    "text": wrap_workspace_content(_snippet(extract_text(comment.comment_body))),
                    "_actor": row.actor_id,
                }
            )
        for row, task_id in body_rows:
            task = open_on(task_id, row.ts_created_at)
            if task is None:
                continue
            entries.append(
                {
                    "source": "task",
                    "at": row.ts_created_at.isoformat(),
                    "task_id": task_id,
                    "display_id": task.display_id,
                    "title": wrap_workspace_content(task.title or ""),
                    "_actor": row.actor_id,
                }
            )

    if note_rows:
        models = {1: PersonalNoteMaster, 2: TaskNoteMaster, 3: ChatNoteMaster}
        notes = {
            (kind, n.note_id): n
            for kind, model in models.items()
            for n in model.objects.filter(
                note_id__in={nid for _r, k, nid in note_rows if k == kind}, team_id=ctx.team_id
            )
        }
        oldest = min(r.ts_created_at for r, _k, _n in note_rows)
        edited: dict[tuple[int, int], datetime] = {}
        for kind, note_id, at in NoteVersionMaster.objects.filter(
            team_id=ctx.team_id,
            editor_id=ctx.user_id,
            note_id__in={nid for _r, _k, nid in note_rows},
            ts_updated_at__gte=oldest,
        ).values_list("note_type", "note_id", "ts_updated_at"):
            edited[(kind, note_id)] = max(at, edited.get((kind, note_id), at))
        for row, kind, note_id in note_rows:
            note = notes.get((kind, note_id))
            if (
                note is None
                or row.is_read
                or edited.get((kind, note_id), row.ts_created_at) > row.ts_created_at
            ):
                continue
            if not _may_read_note(ctx, kind, note):
                continue
            entries.append(
                {
                    "source": "note",
                    "at": row.ts_created_at.isoformat(),
                    "note_type": _NOTE_NAMES[kind],
                    "note_id": note_id,
                    "title": wrap_workspace_content(note.title or ""),
                    "_actor": row.actor_id,
                }
            )

    entries.sort(key=lambda e: e["at"], reverse=True)
    entries = entries[:MAX_LIST]
    for entry in entries:
        names.put(entry, "from", entry.pop("_actor"))
    return entries


def _unanswered_mentions(ctx: ToolContext, now: datetime, names: _Names) -> list[dict[str, Any]]:
    """Every @mention of the caller that still waits on them, newest first: in chat, in a
    task comment, in a task body, in a note. Each entry says its `source`."""
    chat = _chat_mentions(ctx, now)
    activity = _activity_mentions(ctx, now, names, {str(m["message_id"]) for m in chat})
    merged = sorted(
        chat + activity,
        key=lambda e: datetime.fromisoformat(str(e["at"]).replace("Z", "+00:00")),
        reverse=True,
    )
    return merged[:MAX_LIST]


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
    return {"requests": requests, "mentions": _unanswered_mentions(ctx, now, names)}


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
    return {"blocked_tasks": blocked}


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


# --------------------------------------------------------------------------- #
# What to do next                                                              #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# What the member did                                                          #
# --------------------------------------------------------------------------- #


def _local_day_bounds(now: datetime, zone) -> tuple[datetime, datetime, datetime]:
    """UTC instants of the start of yesterday and of today in the member's zone, and `now`."""
    local_today = now.astimezone(zone).date()
    start_today = datetime.combine(local_today, datetime.min.time(), tzinfo=zone)
    start_yesterday = datetime.combine(
        local_today - timedelta(days=1), datetime.min.time(), tzinfo=zone
    )
    return start_yesterday, start_today, now


def _did_event(at: datetime, zone, what: str, summary: str, **extra: Any) -> dict[str, Any]:
    """One thing done, with the time of day in the member's own zone ("14:05"): the day is
    named once for the whole section, so the date is not repeated on every event."""
    event = {"at": at.astimezone(zone).strftime("%H:%M"), "what": what, "summary": summary}
    event.update({k: v for k, v in extra.items() if v})
    return event


def _looked_at(
    ctx: ToolContext,
    since: datetime,
    until: datetime,
    zone,
    changed: set[tuple[str, str]],
    limit: int,
) -> list[dict[str, Any]]:
    """What the member's connected AI app opened or changed in `[since, until)` that is NOT
    among the items already told as changed (MIP-1143): a few lines, newest first.

    The markers (`MemberEvent`) hold references only. The title is read here, now, through the
    rule the item's own tool applies (`LabelAccess`, `_may_read_note`): an item the member can
    no longer open is left out, not named from memory. Only the member's own markers are read.
    `changed` holds the `("task", "12")` / `("note", "personal:3")` keys of the day's changed
    items. A chat thread is noted but not told here. An item both opened and changed is told
    once, as changed."""
    markers = member_events.recent(ctx.user_id, ctx.team_id, since, until, LOOKED_POOL)
    if not markers or limit <= 0:
        return []
    grouped: dict[tuple[str, str], list[Any]] = {}
    for m in markers:  # newest first, so the first of a group is its latest
        grouped.setdefault((m.object_type, m.object_id), []).append(m)
    access = LabelAccess(ctx)
    readable_tasks = access.tasks(
        int(oid) for (otype, oid) in grouped if otype == "task" and oid.isdigit()
    )
    out: list[dict[str, Any]] = []
    for checked, ((otype, oid), rows) in enumerate(grouped.items()):
        if len(out) >= limit or checked >= LOOKED_CHECKS:
            break
        if (otype, oid) in changed:
            continue
        entry: dict[str, Any] | None = None
        if otype == "task" and oid.isdigit() and int(oid) in readable_tasks:
            task = readable_tasks[int(oid)]
            entry = {
                "type": "task",
                "task_id": task.task_id,
                "display_id": task.display_id,
                "title": wrap_workspace_content(task.title or ""),
            }
        elif otype == "note":
            family, _, raw = oid.partition(":")
            title = access.note_title(family, raw) if raw.isdigit() else None
            if title is not None:
                entry = {
                    "type": "note",
                    "note_type": family,
                    "note_id": int(raw),
                    "title": wrap_workspace_content(title or ""),
                }
        elif otype == "project" and oid.isdigit():
            name = access.project_name(oid)
            if name is not None:
                entry = {
                    "type": "project",
                    "project_id": int(oid),
                    "title": wrap_workspace_content(name),
                }
        elif otype == "milestone" and oid.isdigit():
            milestone = access.milestone(oid)
            if milestone is not None and ("task", str(milestone.task_id)) not in changed:
                entry = {
                    "type": "milestone",
                    "milestone_id": milestone.milestone_id,
                    "project_id": milestone.project_id,
                    "title": wrap_workspace_content(milestone.title or ""),
                }
        if entry is None:
            continue
        newest = rows[0]
        app = newest.client
        entry["kind"] = "changed" if any(r.kind == "write" for r in rows) else "opened"
        entry["app"] = app if app == "api_key" else wrap_workspace_content(app)
        entry["at"] = newest.ts.astimezone(zone).strftime("%H:%M")
        out.append(entry)
    return out


def _chat_name(me: str, row: dict[str, Any]) -> str:
    """The chat's name as the member sees it (MIP-1333). A DM has no shared title (its "name" is
    the other person), so an empty title is resolved the way search and the agent's source chips
    do; anything it cannot resolve stays empty and the report says "a chat"."""
    title = row["channel__title"] or ""
    if title:
        return title
    from origin.search_engine.friendly_titles import friendly_chat_title  # noqa: PLC0415

    try:
        return (
            friendly_chat_title(
                str(me), CHAT_TYPE_LABEL.get(row["channel__kind"]), row["channel_id"]
            )
            or ""
        )
    except Exception:  # noqa: BLE001 - a name is a nicety, never a failed briefing
        log.warning("briefing_chat_name_failed channel=%s", row.get("channel_id"))
        return ""


def _did_in(
    ctx: ToolContext,
    since: datetime,
    until: datetime,
    zone,
    names: _Names,
    max_items: int,
    max_events: int,
    max_looked: int = 0,
) -> dict[str, Any]:
    """What the caller did in `[since, until)`, from what is already saved: their changes to
    tasks, comments, edits to notes, to-dos finished and messages sent, grouped by item, in
    time order. Only what they can open now. Chats are counted, never read. The PR activity
    and the merge that closes a task, on tasks assigned to them, are theirs too (automated,
    flagged): that is where the caller's own PRs belong (MIP-1142).

    Compact on purpose (the briefing's whole budget is about 32,000 characters): the busiest
    items first, the latest few events of each, and `events_total` for the rest."""
    me, team = ctx.user_id, ctx.team_id
    window = {"ts_created_at__gte": since, "ts_created_at__lt": until}

    acts = list(
        TaskActivity.objects.filter(team_id=team, actor_id=me, **window)
        .exclude(action_type__in=_COMMENT_ACTIONS)
        .order_by("-ts_created_at")[:EVENT_POOL]
    )
    automation = list(
        TaskActivity.objects.filter(
            team_id=team, actor__isnull=True, task__assignee_id=me, **window
        )
        .filter(
            Q(action_type__in=_PR_ACTIONS)
            | Q(action_type=TaskActivityActionType.STATUS, metadata__closedByPrMerge=True)
        )
        .order_by("-ts_created_at")[:EVENT_POOL]
    )
    comments = list(
        TaskComments.objects.filter(
            sender_id=me,
            is_deleted=False,
            task__team_id=team,
            ts_sent_at__gte=since,
            ts_sent_at__lt=until,
        ).order_by("-ts_sent_at")[:EVENT_POOL]
    )
    readable = LabelAccess(ctx).tasks(
        {a.task_id for a in acts + automation} | {c.task_id for c in comments}
    )
    by_task: dict[int, list[tuple[datetime, dict[str, Any]]]] = {}
    for row in acts + automation:
        if row.task_id in readable:
            event = _activity_event(row, me, names)
            via = (row.metadata or {}).get("via")
            via_kind = via.get("kind") if isinstance(via, dict) else None
            by_task.setdefault(row.task_id, []).append(
                (
                    row.ts_created_at,
                    _did_event(
                        row.ts_created_at,
                        zone,
                        event["kind"] if event["kind"] != "other" else "change",
                        event["summary"],
                        automated=True if event.get("automated") else None,
                        by=event.get("_label") if event.get("automated") else None,
                        via=str(via_kind)[:30] if via_kind not in (None, "app") else None,
                    ),
                )
            )
    for c in comments:
        if c.task_id in readable:
            by_task.setdefault(c.task_id, []).append(
                (c.ts_sent_at, _did_event(c.ts_sent_at, zone, "comment", "commented"))
            )
    items: list[tuple[int, datetime, dict[str, Any]]] = []
    for task_id, events in by_task.items():
        task = readable[task_id]
        events.sort(key=lambda e: e[0])
        items.append(
            (
                len(events),
                events[-1][0],
                {
                    "type": "task",
                    "task_id": task_id,
                    "display_id": task.display_id,
                    "title": wrap_workspace_content(task.title or ""),
                    "status": task.status,
                    "events_total": len(events),
                    "events": [e for _at, e in events[-max_events:]],
                },
            )
        )

    versions = list(
        NoteVersionMaster.objects.filter(
            team_id=team, editor_id=me, ts_updated_at__gte=since, ts_updated_at__lt=until
        ).order_by("-ts_updated_at")[:EVENT_POOL]
    )
    note_models = {1: PersonalNoteMaster, 2: TaskNoteMaster, 3: ChatNoteMaster}
    notes = {
        (kind, n.note_id): n
        for kind, model in note_models.items()
        for n in model.objects.filter(
            note_id__in={v.note_id for v in versions if v.note_type == kind}, team_id=team
        )
    }
    by_note: dict[tuple[int, int], list[Any]] = {}
    for v in versions:
        note = notes.get((v.note_type, v.note_id))
        if note is not None and _may_read_note(ctx, v.note_type, note):
            by_note.setdefault((v.note_type, v.note_id), []).append(v)
    for (kind, note_id), rows in by_note.items():
        rows.sort(key=lambda v: v.ts_updated_at)
        events = [
            _did_event(
                v.ts_updated_at,
                zone,
                "note_edit",
                "restored an earlier version" if v.restored_from_version_no else "edited the note",
            )
            for v in rows
        ]
        items.append(
            (
                len(events),
                rows[-1].ts_updated_at,
                {
                    "type": "note",
                    "note_type": _NOTE_KIND[kind],
                    "note_id": note_id,
                    "title": wrap_workspace_content(notes[(kind, note_id)].title or ""),
                    "events_total": len(events),
                    "events": events[-max_events:],
                },
            )
        )

    items.sort(key=lambda i: (i[0], i[1]), reverse=True)

    finished = ToDoItem.objects.filter(
        group__user_id=me,
        group__team_id=team,
        is_completed=True,
        ts_completed_at__gte=since,
        ts_completed_at__lt=until,
    )
    todos = list(finished.order_by("-ts_completed_at")[:DID_TODOS])

    # Chats are counted, never read. A task comment is mirrored into the project chat as a
    # message and is already counted as a comment; a task's card is not something said.
    sent = (
        Message.objects.filter(
            sender_id=me,
            deleted_at__isnull=True,
            task_id__isnull=True,
            channel_id__in=readable_chats_qs(team, me).values("id"),
            ts_sent_at__gte=since,
            ts_sent_at__lt=until,
        )
        .exclude(metadata__has_key="taskCommentId")
        .values("channel_id", "channel__title", "channel__kind")
        .annotate(n=Count("id"))
        .order_by("-n")
    )
    sent = list(sent)
    chats = [
        {
            "chat_id": str(row["channel_id"]),
            "chat_type": CHAT_TYPE_LABEL.get(row["channel__kind"]),
            "title": wrap_workspace_content(_chat_name(me, row)),
            "messages": row["n"],
        }
        for row in sent[:DID_CHATS]
    ]
    did = {
        "totals": {
            "items": len(items),
            "comments": len(comments),
            "todos_done": finished.count(),
            "messages": sum(row["n"] for row in sent),
        },
        "items": [i for _n, _at, i in items[:max_items]],
        "items_truncated": len(items) > max_items,
        "todos_done": [
            {"item_id": t.item_id, "title": wrap_workspace_content(t.title or "")} for t in todos
        ],
        "chats": chats,
    }
    if max_looked > 0:
        # What their AI app opened or changed that no saved history already tells (MIP-1143).
        # Only asked for by the callers that show it; the key is absent otherwise.
        told = {
            (
                i["type"],
                str(i["task_id"]) if i["type"] == "task" else f"{i['note_type']}:{i['note_id']}",
            )
            for _n, _at, i in items
        }
        did["looked_at"] = _looked_at(ctx, since, until, zone, told, max_looked)
    return did


def what_you_did(ctx: ToolContext, now: datetime, zone, names: _Names) -> dict[str, Any]:
    """What the member did today and yesterday (their own time zone), from what is already
    saved: nothing is asked of them and nothing new is stored (MIP-1145). Today in detail,
    yesterday as the gist."""
    start_yesterday, start_today, until = _local_day_bounds(now, zone)
    return {
        "today": {
            "local_date": start_today.date().isoformat(),
            **_did_in(
                ctx,
                start_today,
                until + timedelta(seconds=1),
                zone,
                names,
                DID_ITEMS["today"],
                DID_EVENTS["today"],
                DID_LOOKED,
            ),
        },
        "yesterday": {
            "local_date": start_yesterday.date().isoformat(),
            **_did_in(
                ctx,
                start_yesterday,
                start_today,
                zone,
                names,
                DID_ITEMS["yesterday"],
                DID_EVENTS["yesterday"],
            ),
        },
    }


def did_between(
    ctx: ToolContext,
    since: datetime,
    until: datetime,
    max_items: int,
    max_events: int,
    zone,
    max_looked: int = 0,
) -> dict[str, Any]:
    """What the member did in `[since, until)`, with the caps the caller chooses. A scheduled
    report has more room than the briefing, which is sized for a model's context (MIP-1148,
    MIP-1152). People are named once, after everything that needs a name has said so."""
    names = _Names(ctx)
    did = {
        "local_date": since.astimezone(zone).date().isoformat(),
        **_did_in(ctx, since, until, zone, names, max_items, max_events, max_looked),
    }
    names.fill()
    return did


def did_on_day(
    ctx: ToolContext,
    day: str,
    now: datetime,
    zone,
    max_items: int,
    max_events: int,
    max_looked: int = 0,
) -> dict[str, Any]:
    """`what_you_did` for ONE day ("today" or "yesterday", the member's own zone)."""
    if day not in ("today", "yesterday"):
        raise ValueError(f"day must be 'today' or 'yesterday', not {day!r}")
    start_yesterday, start_today, until = _local_day_bounds(now, zone)
    start, end = (
        (start_today, until + timedelta(seconds=1))
        if day == "today"
        else (start_yesterday, start_today)
    )
    return did_between(ctx, start, end, max_items, max_events, zone, max_looked)


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


def _suggestion_pool(
    ctx: ToolContext, items: list[dict[str, Any]], project_id: int | None
) -> list[TaskMaster]:
    """The member's own open tasks a suggestion may be made from: the newest hundred, and any
    task that changed for them and is assigned to them."""
    activity = {i["task_id"]: i for i in items if i["type"] == "task"}
    pool = list(_my_active_tasks(ctx, project_id).order_by("-ts_updated_at")[:100])
    pooled = {t.task_id for t in pool}
    missing = [tid for tid, i in activity.items() if i["assigned_to_me"] and tid not in pooled]
    if missing:
        pool += list(_my_active_tasks(ctx, project_id).filter(task_id__in=missing))
    return pool


def important_next(
    ctx: ToolContext,
    now: datetime,
    zone,
    items: list[dict[str, Any]],
    project_id: int | None,
    week: Any,
) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None, set[int]]:
    """`(top, importance block, excluded)` (MIP-1087): the top few ranked by what the member's
    activity says matters to them (`services/importance.py`: priority, due date and status are
    only weak hints, and where activity disagrees with them it is a suggestion, never a change).

    `top` is None for a member without a recorded week: they get the plain `suggested_next`,
    minus the tasks in `excluded` (what they marked not important). Never raises: a failure here
    is logged and the briefing falls back to the plain ranking, as it did before this existed."""
    from origin.services import importance  # noqa: PLC0415 - it reads item_lines, which reads this module

    try:
        prefs = importance.load(ctx)
        if not importance.engaged(week):
            return None, importance.briefing_block(prefs, []), importance.excluded(prefs)
        pool = _suggestion_pool(ctx, items, project_id)
        scored, live = importance.rank(ctx, now, prefs, week, pool, project_id)
        top = scored[: importance.TOP_BRIEFING]
        people = importance.people_counts(ctx, now, [s.task for s in top])
        today = now.astimezone(zone).date()
        shown = [importance.shaped(s, _task_row(s.task, today), people) for s in top]
        found = importance.suggestions(ctx, now, prefs, week, pool, scored, live)
        return shown, importance.briefing_block(prefs, found), importance.excluded(prefs)
    except Exception:  # noqa: BLE001 - what matters to the member never fails a briefing
        log.warning("Importance ranking failed; using the plain ranking", exc_info=True)
        return None, None, set()


def suggested_next(
    ctx: ToolContext,
    now: datetime,
    zone,
    win: Window,
    items: list[dict[str, Any]],
    project_id: int | None,
    exclude: set[int] | frozenset[int] = frozenset(),
) -> list[dict[str, Any]]:
    """The top few things to look at, with why. `exclude` is the tasks the member marked not
    important (MIP-1087): left out, and nothing else about the ranking changes.

    Ranked by what is going on, not by what the fields say: how much happened on a task
    and what kind (comments count for more than field changes), whether it is due or
    overdue, and only then its priority, as a weak hint. Every reason shown is a signal
    that really fired, so the member can disagree with it."""
    today = now.astimezone(zone).date()
    activity = {i["task_id"]: i for i in items if i["type"] == "task"}
    pool = [t for t in _suggestion_pool(ctx, items, project_id) if t.task_id not in exclude]

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

    items, total_changed, automated_only = changed_for_me(ctx, win, project, names)
    chat_answers = answers_in_chats(ctx, now, project, names)
    me = CustomUser.objects.filter(id=ctx.user_id).first()
    team = TeamMaster.objects.filter(team_id=ctx.team_id).first()
    agenda_section = agenda(ctx, now, zone, project)
    waiting_me = waiting_on_me(ctx, now, names)
    waiting_others = waiting_on_others(ctx, names)
    # The per-day table (MIP-1084): brought up to yesterday and read ONCE, before anything is
    # ranked, so the first briefing ranks with the same week the second one does.
    from origin.services import item_activity  # noqa: PLC0415 - it reads item_lines, which reads this module

    activity, week = item_activity.briefing_data(ctx, now)
    ranked, importance_block, excluded = important_next(ctx, now, zone, items, project, week)
    suggestions = (
        ranked
        if ranked is not None
        else suggested_next(ctx, now, zone, win, items, project, excluded)
    )
    did = what_you_did(ctx, now, zone, names)

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
            "automated_only": automated_only,
            "project_filter": project,
            "chat_answers": chat_answers,
        },
        "waiting_on_me": waiting_me,
        "waiting_on_others": waiting_others,
        "stale_mine": stale_mine(ctx, now, zone, project),
        "what_you_did": did,
        "activity": activity,
        "suggested_next": suggestions,
        "importance": importance_block,
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
