"""What a task's comments and history say, for the one prompt (MIP-1210).

Three rules, each one a test:

1. **Only what the member can open.** A task is read here only after the same check the agent
   tools use (`task_acl_user_ids`: project member, assignee or reporter, in the schedule's
   team, not deleted), whatever the caller listed. A task that fails it is not read, not sent
   to the model and so cannot be named.
2. **Only text that cannot name something else.** Comments, and a filtered history: status,
   priority, assignee and due date changes, and a reopen. NOT the relation labels of the
   history (a parent, milestone, sprint or project title can belong to something the member
   cannot open), and NEVER the previous body a `description_edited` row keeps.
3. **Everything a person typed is fenced** (`wrap_workspace_content`): a comment, a task
   title, a username. The model is told that fenced text is data.

Deliberately not read yet: the task's linked thread and note (a separate access check each).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from origin.models.common.user_models import CustomUser
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import ToolContext, wrap_workspace_content
from origin.search_engine.agent.tools.task_enums import (
    VALID_PRIORITIES,
    VALID_STATUSES,
)
from origin.search_engine.text_extraction import extract_text

COMMENTS_PER_TASK = 4
COMMENT_CHARS = 400
EVENTS_PER_TASK = 5
EVENT_DAYS = 14

# The history kinds that can explain a state, and only these.
_EVENT_KINDS = (
    TaskActivityActionType.STATUS,
    TaskActivityActionType.PRIORITY,
    TaskActivityActionType.ASSIGNEE,
    TaskActivityActionType.DUE_DATE,
    TaskActivityActionType.REOPENED,
)
_STATUSES = VALID_STATUSES | {"Deleted"}


@dataclass(frozen=True)
class TaskContext:
    task_id: int
    display_id: str
    title: str
    status: str
    due_date: str
    comments: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    # Changes whenever anything the prompt says about the task changes: the cache key.
    fingerprint: str = ""

    @property
    def has_context(self) -> bool:
        return bool(self.comments or self.events)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _day(value: datetime | None) -> str:
    return value.date().isoformat() if value else "?"


def _enum_or_none(value: Any, allowed: set[str] | frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _event_line(row: TaskActivity, names: dict[str, str]) -> str | None:
    """One fact of the history as a short line, or None when the row says nothing safe."""
    who = names.get(str(row.actor_id or ""))
    by = f" by {wrap_workspace_content(who)}" if who else ""
    when = _day(row.ts_created_at)
    kind = row.action_type
    if kind == TaskActivityActionType.STATUS:
        old, new = _enum_or_none(row.old_value, _STATUSES), _enum_or_none(row.new_value, _STATUSES)
        return f"{when}: status {old or '?'} to {new}{by}" if new else None
    if kind == TaskActivityActionType.PRIORITY:
        old = _enum_or_none(row.old_value, VALID_PRIORITIES)
        new = _enum_or_none(row.new_value, VALID_PRIORITIES)
        return f"{when}: priority {old or '?'} to {new}{by}" if new else None
    if kind == TaskActivityActionType.DUE_DATE:
        old, new = str(row.old_value or "")[:10], str(row.new_value or "")[:10]
        if len(new) == 10 and new[4] == "-":
            return f"{when}: due date {old or 'none'} to {new}{by}"
        return f"{when}: due date removed{by}" if not new and old else None
    if kind == TaskActivityActionType.ASSIGNEE:
        to = names.get(str(row.new_value or ""))
        return (
            f"{when}: assigned to {wrap_workspace_content(to)}{by}"
            if to
            else f"{when}: unassigned{by}"
        )
    if kind == TaskActivityActionType.REOPENED:
        return f"{when}: reopened{by}"
    return None


def _open_tasks(ctx: ToolContext, task_ids: list[int]) -> list[TaskMaster]:
    """The listed tasks the member may open, in the listed order. The check is the agent
    tools': in this team, not deleted, and the member is in the task's project or is its
    assignee or reporter."""
    wanted = list(dict.fromkeys(int(t) for t in task_ids if t is not None))
    rows = {
        t.task_id: t
        for t in TaskMaster.objects.select_related("project").filter(
            task_id__in=wanted, team_id=ctx.team_id, is_deleted=False
        )
    }
    out = []
    for task_id in wanted:
        task = rows.get(task_id)
        if task is not None and ctx.user_id in task_acl_user_ids(
            task.project_id, task.assignee_id, task.reporter_id
        ):
            out.append(task)
    return out


def can_open(ctx: ToolContext, task_ids: list[int]) -> set[int]:
    """The subset of `task_ids` the member may open (see `_open_tasks`)."""
    return {t.task_id for t in _open_tasks(ctx, task_ids)}


def gather(ctx: ToolContext, task_ids: list[int], *, now: datetime) -> list[TaskContext]:
    """The context of each listed task the member may open, in the listed order. A task with
    no comment and no history that explains anything is returned with `has_context` False
    (the caller does not ask the model about it)."""
    tasks = _open_tasks(ctx, task_ids)
    if not tasks:
        return []
    ids = [t.task_id for t in tasks]

    comments: dict[int, list[TaskComments]] = {}
    for c in TaskComments.objects.filter(task_id__in=ids, is_deleted=False).order_by(
        "task_id", "-ts_sent_at", "-comment_id"
    ):
        bucket = comments.setdefault(c.task_id, [])
        if len(bucket) < COMMENTS_PER_TASK:
            bucket.append(c)

    since = now - timedelta(days=EVENT_DAYS)
    events: dict[int, list[TaskActivity]] = {}
    for a in TaskActivity.objects.filter(
        task_id__in=ids, action_type__in=_EVENT_KINDS, ts_created_at__gte=since
    ).order_by("task_id", "-ts_created_at", "-activity_id"):
        bucket = events.setdefault(a.task_id, [])
        if len(bucket) < EVENTS_PER_TASK:
            bucket.append(a)

    user_ids = {str(c.sender_id) for rows in comments.values() for c in rows if c.sender_id}
    for rows in events.values():
        for a in rows:
            user_ids.add(str(a.actor_id or ""))
            if a.action_type == TaskActivityActionType.ASSIGNEE and a.new_value:
                user_ids.add(str(a.new_value))
    user_ids.discard("")
    names = {
        str(u["id"]): u["username"] or ""
        for u in CustomUser.objects.filter(id__in=_valid_uuids(user_ids)).values("id", "username")
    }

    out = []
    for task in tasks:
        comment_lines, comment_marks = [], []
        for c in reversed(comments.get(task.task_id, [])):  # oldest first
            text = extract_text(c.comment_body)
            if not text:
                continue
            who = names.get(str(c.sender_id or ""), "")
            line = (
                f"{_day(c.ts_sent_at)} "
                f"{wrap_workspace_content(who) if who else 'someone'}: "
                f"{wrap_workspace_content(_clip(text, COMMENT_CHARS))}"
            )
            comment_lines.append(line)
            comment_marks.append(
                (c.comment_id, c.ts_updated_at.isoformat() if c.ts_updated_at else "")
            )
        event_lines, event_marks = [], []
        for a in reversed(events.get(task.task_id, [])):
            line = _event_line(a, names)
            if line:
                event_lines.append(line)
                event_marks.append(a.activity_id)
        due = task.due_date.isoformat() if task.due_date else ""
        digest = hashlib.sha256(
            repr((task.task_id, task.status, due, task.title, comment_marks, event_marks)).encode()
        ).hexdigest()[:24]
        out.append(
            TaskContext(
                task_id=task.task_id,
                display_id=task.display_id,
                title=_clip(task.title or "", 200),
                status=task.status or "",
                due_date=due,
                comments=comment_lines,
                events=event_lines,
                fingerprint=digest,
            )
        )
    return out


def _valid_uuids(values: set[str]) -> list[str]:
    import uuid  # noqa: PLC0415

    out = []
    for v in values:
        try:
            out.append(str(uuid.UUID(v)))
        except (ValueError, AttributeError, TypeError):
            continue
    return out
