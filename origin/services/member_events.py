"""Notice which items a member's AI app opens or changes over MCP (MIP-1143), and the
member's own changes that nothing else saves (MIP-1085).

Markers, never text (see `models/common/member_event_models.py` for the why). This module is
the only writer and the only reader of `MemberEvent`.

**Two writers, one statement.** `record_call` notes an MCP call's arguments (above and below).
`record_activity` notes a change the member made: a collaborator, a dependency, a to-do edit
or delete, a note share. It is called from the funnel every path goes through (the services
`sync_task_collaborators`, `todo_items.save_item` / `delete_item`, `note_sharing.share_note` /
`unshare_note`, and the `TaskDependency` signals and `bulk_create`), so the app, an MCP call
and the in-app agent are all covered by one hook, not one per view. Both end in `record`.

**What is recorded, and from what.** From the call's ARGUMENTS only, after the references in
them were resolved to ids (so `PRJ-123`, `123` and a pasted URL are one marker), and only
for a call that succeeded. Which argument names the item a tool touches is written out per
tool in `SUBJECTS`, not guessed from a `_id` suffix: `list_tasks(project_id=...)` filters,
`assign_task(assignee_id=...)` sets a value, and neither opens that item. A tool that touches
no single item (a list, a search, a summary of everything, a create whose new id exists only
in the result) is in `NO_SINGLE_ITEM`, and `test_member_events` makes every catalog tool one
or the other, so a new tool forces the decision. A tool that writes records `write`, any
other `read`. Nothing of the result and no argument text is read here: only ids that pass
`_int_id` / `_uuid_text` / the fixed note-family list are kept.

**It must never make a call fail or slow it down.** `record_call` swallows every error (log
WARNING) and does one statement (`record`): the insert, the retention sweep and the check of
the member's switch are the same SQL statement.

**The switch is checked twice**: on the user row the request already loaded (no query when
it is off), and again in the statement itself, with the user row locked `FOR SHARE`. The
PATCH that turns it off takes the row `FOR UPDATE` and deletes the markers in the same
transaction, so a call that was already running when the member turned it off either wrote
before the purge (and is purged) or waits for it and then writes nothing.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta
from typing import Any

from django.db import connection, transaction
from django.utils import timezone

from origin.middleware.current_user import get_current_source, get_current_user
from origin.models.common.member_event_models import (
    CLIENT_API_KEY,
    KIND_READ,
    KIND_WRITE,
    MCP_KINDS,
    OBJECT_CHAT_THREAD,
    OBJECT_NOTE,
    OBJECT_TASK,
    SOURCE_MCP,
    MemberEvent,
)
from origin.models.common.user_models import CustomUser

log = logging.getLogger(__name__)

# Rows older than this are deleted with the next write of the same member, and never read
# even before that.
RETENTION = timedelta(days=180)
# One row per member, team, item, kind, app and bucket: see the model.
BUCKET_SECONDS = 600
# The most items one call may mark (the widest tool, `update_tasks_bulk`, takes 30).
MAX_ITEMS_PER_CALL = 30
CLIENT_MAX_CHARS = 40
_ID_MAX_DIGITS = 18

_NOTE_FAMILY = {
    "personal": "personal",
    "team": "personal",
    "shared": "personal",
    "task": "task",
    "chat": "chat",
}

# Which argument of which tool names the item the call touches.
#   "task" / "milestone" / "project": the integer id in that argument
#   "note":      `note_id` with its `note_type`
#   "chat":      `chat_id` (a UUID) with an optional `thread_id`
#   "task_rows": the `task_id` of each row of `updates`
SUBJECTS: dict[str, tuple[str, str]] = {
    # tasks
    "get_task": ("task", "task_id"),
    "get_task_blockers": ("task", "task_id"),
    "get_task_history": ("task", "task_id"),
    "get_item_line": ("task", "task_id"),
    "update_task": ("task", "task_id"),
    "assign_task": ("task", "task_id"),
    "add_comment": ("task", "task_id"),
    "update_comment": ("task", "task_id"),
    "delete_comment": ("task", "task_id"),
    "restore_task_body": ("task", "task_id"),
    "add_task_delivery": ("task", "task_id"),
    "remove_task_delivery": ("task", "task_id"),
    "update_tasks_bulk": ("task_rows", "updates"),
    # milestones and projects
    "get_milestone_summary": ("milestone", "milestone_id"),
    "manage_milestone": ("milestone", "milestone_id"),
    "delete_milestone": ("milestone", "milestone_id"),
    "get_project_summary": ("project", "project_id"),
    "manage_project": ("project", "project_id"),
    "manage_project_members": ("project", "project_id"),
    # notes
    "get_note": ("note", "note_id"),
    "get_note_history": ("note", "note_id"),
    "list_note_access": ("note", "note_id"),
    "update_note": ("note", "note_id"),
    "restore_note_version": ("note", "note_id"),
    "manage_note_sharing": ("note", "note_id"),
    "delete_note": ("note", "note_id"),
    # chat
    "get_chat_thread": ("chat", "chat_id"),
}

# Every other published tool. Each one touches no single item the call names: it lists,
# searches or summarizes, it creates something whose id only the result has, or what it
# changes is not a work item (a to-do, a calendar event, a schedule, a label, a sprint).
NO_SINGLE_ITEM = frozenset(
    {
        "list_my_tasks",
        "list_tasks",
        "list_projects",
        "list_milestones",
        "list_sprints",
        "get_sprint_summary",
        "find_task_by_delivery",
        "search_workspace",
        "list_my_chats",
        "list_my_mentions",
        "list_channel_members",
        "get_current_user",
        "get_team_members",
        "list_project_members",
        "list_project_tags",
        "list_task_dependencies",
        "list_note_folders",
        "list_uncompleted_todos",
        "list_today_todos",
        "list_calendar_events",
        "list_calendars",
        "get_my_blockers",
        "list_my_inbox",
        "get_my_throughput",
        "get_workload_distribution",
        "get_top_task_closers",
        "get_task_throughput_stats",
        "get_stale_tasks",
        "get_team_task_summary",
        "get_project_activity_ranking",
        "get_milestone_assignee_counts",
        "get_my_task_summary",
        "get_pr",
        "list_pr_reviews",
        "list_pr_comments",
        "list_pr_files",
        "list_pr_commits",
        "update_task_dependencies",
        "create_task",
        "create_task_plan",
        "create_note",
        "create_todo_item",
        "update_todo_item",
        "delete_todo_item",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
        "manage_sprint",
        "delete_sprint",
        "add_project_tag",
        "manage_project_labels",
        "approve_join_request",
        "decline_join_request",
        "manage_note_folder",
        "favorite_note",
        "delete_note_folder",
        "get_my_briefing",
        "list_my_schedules",
        "create_my_schedule",
        "update_my_schedule",
        "delete_my_schedule",
        "run_my_schedule",
    }
)


# --------------------------------------------------------------------------- #
# Reading a call's arguments                                                   #
# --------------------------------------------------------------------------- #


def _int_id(value: Any) -> str | None:
    """A positive integer id as text, from an int or digits only; anything else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        if len(value) <= _ID_MAX_DIGITS and int(value) > 0:
            return str(int(value))
    return None


def _uuid_text(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


_THREAD_RE = re.compile(r"^[0-9A-Za-z-]{1,40}$")


def subjects(tool_name: str, args: dict[str, Any]) -> list[tuple[str, str]]:
    """`(object_type, object_id)` for each item this call touched, from its (resolved)
    arguments. Empty for a tool in `NO_SINGLE_ITEM`, and for arguments that do not name one.
    Duplicates removed, at most `MAX_ITEMS_PER_CALL`."""
    spec = SUBJECTS.get(tool_name)
    if spec is None or not isinstance(args, dict):
        return []
    how, key = spec
    found: list[tuple[str, str]] = []
    if how in ("task", "milestone", "project"):
        ident = _int_id(args.get(key))
        if ident:
            found.append((how, ident))
    elif how == "task_rows":
        rows = args.get(key)
        if isinstance(rows, list):
            for row in rows[:MAX_ITEMS_PER_CALL]:
                ident = _int_id(row.get("task_id")) if isinstance(row, dict) else None
                if ident:
                    found.append((OBJECT_TASK, ident))
    elif how == "note":
        ident = _int_id(args.get(key))
        family = _NOTE_FAMILY.get(str(args.get("note_type") or "").lower().strip())
        if ident and family:
            found.append((OBJECT_NOTE, f"{family}:{ident}"))
    elif how == "chat":
        chat = _uuid_text(args.get(key))
        if chat:
            thread = args.get("thread_id")
            if isinstance(thread, str) and _THREAD_RE.match(thread):
                chat = f"{chat}:{thread}"
            found.append((OBJECT_CHAT_THREAD, chat))
    seen: set[tuple[str, str]] = set()
    unique = []
    for item in found:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique[:MAX_ITEMS_PER_CALL]


def kind_of(write: bool) -> str:
    return KIND_WRITE if write else KIND_READ


def client_name(credential: Any) -> str:
    """The connected app's short name, or `api_key` for a key (or when it has none)."""
    if credential is None or getattr(credential, "kind", CLIENT_API_KEY) == CLIENT_API_KEY:
        return CLIENT_API_KEY
    try:
        name = " ".join(str(credential.label or "").split())
    except Exception:  # noqa: BLE001 - a label we cannot read is no reason to lose the marker
        name = ""
    return name[:CLIENT_MAX_CHARS] or CLIENT_API_KEY


# --------------------------------------------------------------------------- #
# Writing                                                                      #
# --------------------------------------------------------------------------- #

_TABLE = MemberEvent._meta.db_table
_USER_TABLE = CustomUser._meta.db_table


def _bucket(moment: datetime) -> int:
    return int(moment.timestamp() // BUCKET_SECONDS)


def record(
    *,
    user_id: str,
    team_id: str,
    items: Iterable[tuple[str, ...]],
    kind: str,
    client: str,
    source: str = SOURCE_MCP,
    now: datetime | None = None,
) -> None:
    """Note that `user_id`'s app touched `items`, in ONE statement.

    An item is `(object_type, object_id)` or, for an activity event, `(object_type,
    object_id, other_id)`; the other end is part of the dedupe key.

    The statement (a) deletes the member's rows older than `RETENTION`, (b) inserts one row per
    item, or, when the member already has the marker in this bucket, bumps its `ts` and
    `count`, and (c) inserts nothing at all when the member's switch is off (the user row is
    read in the statement and locked `FOR SHARE`). A raise from here is the caller's to catch.
    """
    now = now or timezone.now()
    padded = [(*item, "")[:3] for item in items]
    unique = list(dict.fromkeys(padded))[:MAX_ITEMS_PER_CALL]
    if not unique:
        return
    values = ", ".join(["(%s, %s, %s)"] * len(unique))
    params: list[Any] = [str(user_id), now - RETENTION]
    params += [str(team_id), kind, source, client, now, _bucket(now)]
    for object_type, object_id, other_id in unique:
        params += [object_type, object_id, other_id]
    params.append(str(user_id))
    sql = f"""
        WITH swept AS (
            DELETE FROM {_TABLE} WHERE user_id = %s AND ts < %s
        )
        INSERT INTO {_TABLE}
            (user_id, team_id, kind, source, client, ts, bucket, count,
             object_type, object_id, other_id)
        SELECT u.id, %s, %s, %s, %s, %s, %s, 1, v.object_type, v.object_id, v.other_id
        FROM {_USER_TABLE} u
        CROSS JOIN (VALUES {values}) AS v(object_type, object_id, other_id)
        WHERE u.id = %s AND u.member_events_enabled
        FOR SHARE OF u
        ON CONFLICT (user_id, team_id, object_type, object_id, kind, other_id, client, source, bucket)
        DO UPDATE SET ts = EXCLUDED.ts, count = {_TABLE}.count + 1
    """  # noqa: S608 - table names are ours, every value is a parameter
    with connection.cursor() as cursor:
        cursor.execute(sql, params)


def record_call(
    *,
    user,
    team_id: str,
    tool_name: str,
    write: bool,
    args: dict[str, Any],
    credential: Any = None,
) -> None:
    """Mark what a successful MCP call touched. NEVER raises, and never leaves the request's
    transaction broken: a failure is logged at WARNING and the call goes on as if nothing
    had been recorded.

    `user` is the request's user, already loaded: when its switch is off (the common
    opted-out case) this does no query at all. The statement checks the switch again, live."""
    try:
        if user is None or not user.member_events_enabled:
            return
        items = subjects(tool_name, args)
        if not items:
            return
        _record_safely(
            user_id=str(user.id),
            team_id=str(team_id),
            items=items,
            kind=kind_of(write),
            client=client_name(credential),
        )
    except Exception:  # noqa: BLE001 - recording is never a reason for a call to fail
        log.warning("MCP use marker not recorded for %s", tool_name, exc_info=True)


def _record_safely(**kwargs: Any) -> None:
    if connection.in_atomic_block:
        # Inside someone's transaction a database error would leave it aborted for the
        # rest of the request, so the statement gets its own savepoint. In autocommit
        # (a request here) there is nothing to protect and it costs no extra statement.
        with transaction.atomic():
            record(**kwargs)
    else:
        record(**kwargs)


# --------------------------------------------------------------------------- #
# The member's own changes that nothing else saves (MIP-1085)                  #
# --------------------------------------------------------------------------- #

_unrecorded: ContextVar[bool] = ContextVar("member_events_unrecorded", default=False)


@contextmanager
def not_recorded() -> Iterator[None]:
    """Note nothing from `record_activity` inside the block.

    For work Genos does for a member rather than the member doing it: the seeders that put
    example data into a new workspace run inside the member's request, so without this the
    member would be credited with a dependency they never made."""
    token = _unrecorded.set(True)
    try:
        yield
    finally:
        _unrecorded.reset(token)


def activity_source() -> str:
    """How the change reached Genos: `app`, `agent`, `api_key`, ... (the words a task history
    row's `via` uses), `system` when nothing in this thread says."""
    via = get_current_source() or {}
    return str(via.get("kind") or "system")[:16]


def acting_user_id() -> str | None:
    """The member whose request or approved agent write this is, or None when no one is acting
    (a command, a webhook, a seeder): then there is no member to describe."""
    user = get_current_user()
    return str(user.id) if user is not None else None


def recording_activity_for(user_id: str | None) -> bool:
    """False when nothing could be written for `user_id`: nobody, the block `not_recorded`, or
    the member's switch is off on the user row this request already holds (no query). A True
    is not a promise: the statement checks the switch again, under lock."""
    if not user_id or _unrecorded.get():
        return False
    current = get_current_user()
    if current is not None and str(current.id) == str(user_id):
        return bool(current.member_events_enabled)
    return True


def record_activity(
    *,
    user_id: str | None,
    team_id: str | None,
    kind: str,
    items: Iterable[tuple[str, str, str]],
    now: datetime | None = None,
) -> None:
    """Note that the member made a change, one row per `(object_type, object_id, other_id)`.

    NEVER raises and never breaks the caller's transaction (a failure is logged at WARNING).
    Writes nothing when there is no member or no team, inside `not_recorded()`, or when the
    member's switch is off. References only: the caller passes ids, never a title."""
    try:
        if not team_id or not recording_activity_for(user_id):
            return
        wanted = [(str(t), str(i), str(o)) for t, i, o in items]
        source = activity_source()
        for start in range(0, len(wanted), MAX_ITEMS_PER_CALL):
            _record_safely(
                user_id=str(user_id),
                team_id=str(team_id),
                items=wanted[start : start + MAX_ITEMS_PER_CALL],
                kind=kind,
                client="",
                source=source,
                now=now,
            )
    except Exception:  # noqa: BLE001 - recording is never a reason for a change to fail
        log.warning("Member event %s not recorded", kind, exc_info=True)


def note_dependencies(dependencies: Iterable[Any], kind: str) -> None:
    """Note, for the acting member, that these `TaskDependency` edges were added or removed.

    The row is on the BLOCKED task (`object_id`) and `other_id` is the blocker, so "task 5 is
    blocked by task 3" reads from task 5's own line. The team is the edge's own, or, when the
    column is empty (it is SET_NULL), the blocked task's. Never raises."""
    try:
        actor = acting_user_id()
        if not recording_activity_for(actor):
            return
        deps = list(dependencies)
        teams: dict[int, Any] = {}
        missing = {d.blocked_task_id for d in deps if d.team_id is None}
        if missing:
            from origin.models.task.task_models import TaskMaster  # noqa: PLC0415

            teams = dict(
                TaskMaster.objects.filter(task_id__in=missing).values_list("task_id", "team_id")
            )
        by_team: dict[str, list[tuple[str, str, str]]] = {}
        for dep in deps:
            team_id = dep.team_id if dep.team_id is not None else teams.get(dep.blocked_task_id)
            if team_id is None:
                continue
            by_team.setdefault(str(team_id), []).append(
                (OBJECT_TASK, str(dep.blocked_task_id), str(dep.blocker_task_id))
            )
        for team_id, items in by_team.items():
            record_activity(user_id=actor, team_id=team_id, kind=kind, items=items)
    except Exception:  # noqa: BLE001 - recording is never a reason for a change to fail
        log.warning("Dependency event %s not recorded", kind, exc_info=True)


# --------------------------------------------------------------------------- #
# Reading and deleting                                                         #
# --------------------------------------------------------------------------- #


def recent(
    user_id: str,
    team_id: str,
    since: datetime,
    until: datetime,
    limit: int = 60,
    kinds: tuple[str, ...] | None = MCP_KINDS,
):
    """The member's own rows in `[since, until)` for one team, newest first, never older
    than the retention. ALWAYS filtered by the member: nothing here can return another's.

    `kinds` defaults to the AI-app markers (`read`, `write`): the briefing tells those as
    "opened / changed with your AI app" and must never tell an activity event that way. Ask
    for `ACTIVITY_KINDS` to read the member's own changes, or `None` for every kind there is
    (what an item's line reads: it tells each kind by its own name)."""
    floor = timezone.now() - RETENTION
    rows = MemberEvent.objects.filter(
        user_id=user_id, team_id=team_id, ts__gte=max(since, floor), ts__lt=until
    )
    if kinds is not None:
        rows = rows.filter(kind__in=kinds)
    return list(rows.order_by("-ts", "-event_id")[:limit])


def purge(user_id: str) -> int:
    """Delete every marker of this member, in one statement."""
    deleted, _ = MemberEvent.objects.filter(user_id=user_id).delete()
    return deleted
