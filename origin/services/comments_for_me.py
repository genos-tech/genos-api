"""Task and milestone comments the member has not answered (MIP-2485).

"Which comments am I sitting on?" cannot be answered from the other reads: the unread digest drops
a comment the moment it is seen, and reading tasks one by one does not fit one run. So this hands
over, as DATA, every comment by someone else that the member has neither replied after nor reacted
to, on the open tasks and milestones that involve them. It does NOT decide whether a comment asks
anything: the reader judges that from the text.

The mechanical rule, per task:

  * Involved: assignee (of the task, or any assignee of the milestone it backs), reporter,
    collaborator, or @mentioned (in a comment or the description).
  * Open: not Closed or Deleted, by the ancestor rule too (`ancestor_rule`).
  * Waiting: someone else's comment, newer than the member's last comment on the task (or their
    last reply in the task's project-chat thread), that the member has not reacted to.
  * A comment posted AS the member by an AI app or an API key does not count as their reply: an
    agent's status note would otherwise hide a question they never saw. It is handed over
    (`your_ai_app_comment`) so the reader can judge.

Access is `readable_task_q` (the rule the other reads use), so being mentioned or named a
collaborator alone never opens a task. Read-only: nothing is marked read. All text and
names are fenced. An answer is bounded in size (`ANSWER_CHARS`) and `next_offset` counts what was
really returned. The number of queries does not grow with the number of tasks or comments.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType, Message, MessageReaction
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskCommentReactionFact, TaskComments, TaskMaster
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools import ancestor_rule
from origin.search_engine.agent.tools.base import ToolContext, ToolError, wrap_workspace_content
from origin.search_engine.text_extraction import extract_text
from origin.services import app_links
from origin.services.item_lines import readable_task_q

DEFAULT_DAYS = 30
MAX_DAYS = 90
DEFAULT_LIMIT = 25
MAX_LIMIT = 50
# Comments by others looked at in one call, newest first; `stats.complete` says when it hit.
POOL = 3000
# Waiting comments returned per task (the oldest one, then the newest), and the text of each.
COMMENTS_PER_TASK = 8
TEXT_CHARS = 700
# The size of the tasks of one answer, as indented JSON: under the MCP result cap (24,000), which
# would otherwise drop tasks that `next_offset` had already counted.
ANSWER_CHARS = 18_000
# How an AI app's or an API key's write is marked on the comment's history row.
_AI_APP_KINDS = ("api_key", "oauth")
# The status that surfaces as `SURFACE_TASK_BODY` activity rows (an @mention in a description).
_SURFACE_TASK_BODY = 5
_NOT_OPEN = ("Closed", "Deleted")


def _int_arg(value: Any, name: str, default: int, low: int, high: int) -> int:
    if value in (None, ""):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"`{name}` must be an integer (got {value!r}).")
    return max(low, min(n, high))


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _comment_mentions(ctx: ToolContext):
    """The task ids of comments that @mention the member, as a subquery. A task's own card mentions
    its assignee, so only comment copies count among the messages."""
    return Activity.objects.filter(
        recipient_id=ctx.user_id,
        team_id=ctx.team_id,
        activity_type=ActivityType.MENTION,
        message__metadata__has_key="taskCommentId",
        message__task_id__isnull=False,
    ).values("message__task_id")


def _description_mentions(ctx: ToolContext) -> set[int]:
    ids: set[int] = set()
    for raw in Activity.objects.filter(
        recipient_id=ctx.user_id,
        team_id=ctx.team_id,
        activity_type=ActivityType.MENTION,
        surface_type=_SURFACE_TASK_BODY,
    ).values_list("meta__taskId", flat=True):
        try:
            ids.add(int(raw))
        except (TypeError, ValueError):
            continue
    return ids


def _milestone_task_ids(ctx: ToolContext):
    """The backing tasks of the milestones the member is any assignee of: only the first assignee
    is copied onto the task itself."""
    return MilestoneAssignees.objects.filter(
        user_id=ctx.user_id, milestone__is_deleted=False, milestone__task_id__isnull=False
    ).values("milestone__task_id")


def _open_involved_tasks(ctx: ToolContext, described: set[int], task_id: int | None):
    collaborating = TaskMaster.collaborators.through.objects.filter(
        customuser_id=ctx.user_id
    ).values("taskmaster_id")
    qs = (
        TaskMaster.objects.filter(readable_task_q(ctx), is_init_task=False)
        .filter(
            Q(assignee_id=ctx.user_id)
            | Q(reporter_id=ctx.user_id)
            | Q(task_id__in=collaborating)
            | Q(task_id__in=_milestone_task_ids(ctx))
            | Q(task_id__in=_comment_mentions(ctx))
            | Q(task_id__in=described)
        )
        .exclude(status__in=_NOT_OPEN)
    )
    if task_id is not None:
        qs = qs.filter(task_id=task_id)
    if ancestor_rule.enabled():
        projects = set(qs.values_list("project_id", flat=True).distinct())
        not_open = ancestor_rule.rule_for_projects(projects, ctx.team_id).not_open
        if not_open:
            qs = qs.exclude(task_id__in=sorted(not_open))
    return qs


def _ai_app_comments(ctx: ToolContext, task_ids: list[int]) -> set[tuple[int, int]]:
    """`(task_id, comment_id)` of the member's comments whose latest write came from an AI app or
    an API key (the newest history row of a comment decides, as the app's own chip does)."""
    rows = (
        TaskActivity.objects.filter(
            task_id__in=task_ids,
            actor_id=ctx.user_id,
            action_type__in=(
                TaskActivityActionType.COMMENT_ADDED,
                TaskActivityActionType.COMMENT_EDITED,
            ),
        )
        .order_by("activity_id")
        .values_list("task_id", "metadata__commentId", "metadata__via__kind")
    )
    latest = {(task_id, comment_id): kind for task_id, comment_id, kind in rows}
    return {key for key, kind in latest.items() if kind in _AI_APP_KINDS}


def _my_comments(ctx: ToolContext, task_ids: list[int]):
    """The member's own words on each task: when they last answered (a comment they did not leave
    to an AI app, or a reply typed in the task's project-chat thread, which is not stored as a
    comment), their latest comment id, and their AI app's latest comment id."""
    by_app = _ai_app_comments(ctx, task_ids)
    answered: dict[int, datetime] = {}
    own: dict[int, int] = {}
    app: dict[int, int] = {}
    for task_id, comment_id, at in (
        TaskComments.objects.filter(task_id__in=task_ids, sender_id=ctx.user_id, is_deleted=False)
        .order_by("ts_sent_at", "comment_id")
        .values_list("task_id", "comment_id", "ts_sent_at")
    ):
        if (task_id, comment_id) in by_app:
            app[task_id] = comment_id
        else:
            own[task_id] = comment_id
            answered[task_id] = at
    in_thread = (
        Message.objects.filter(
            thread_root__task_id__in=task_ids,
            sender_id=ctx.user_id,
            is_thread_reply=True,
            deleted_at__isnull=True,
        )
        .exclude(metadata__has_key="taskCommentId")
        .values_list("thread_root__task_id", "ts_sent_at")
    )
    for task_id, at in in_thread:
        if task_id not in answered or at > answered[task_id]:
            answered[task_id] = at
    return answered, own, app


def _my_reactions(ctx: ToolContext, task_ids: list[int]) -> set[tuple[int, int]]:
    """`(task_id, comment_id)` the member reacted to, from the task or from the comment's copy in
    the project chat."""
    out = set(
        TaskCommentReactionFact.objects.filter(
            task_id__in=task_ids, sender_id=ctx.user_id
        ).values_list("task_id", "comment_id")
    )
    mirrored = MessageReaction.objects.filter(
        user_id=ctx.user_id,
        message__task_id__in=task_ids,
        message__metadata__has_key="taskCommentId",
    ).values_list("message__task_id", "message__metadata__taskCommentId")
    for task_id, comment_id in mirrored:
        try:
            out.add((task_id, int(comment_id)))
        except (TypeError, ValueError):
            continue
    return out


def _comment_flags(ctx: ToolContext, task_ids: list[int]):
    """`(task_id, comment_id)` sets: still unread, and mentioning the member."""
    unread: set[tuple[int, int]] = set()
    mentions: set[tuple[int, int]] = set()
    rows = Activity.objects.filter(
        Q(is_read=False) | Q(activity_type=ActivityType.MENTION),
        recipient_id=ctx.user_id,
        team_id=ctx.team_id,
        message__task_id__in=task_ids,
        message__metadata__has_key="taskCommentId",
    ).values_list(
        "message__task_id", "message__metadata__taskCommentId", "is_read", "activity_type"
    )
    for task_id, comment_id, is_read, kind in rows:
        try:
            key = (task_id, int(comment_id))
        except (TypeError, ValueError):
            continue
        # A reaction on the member's own message also files a row; it is not the comment unread.
        if kind == ActivityType.REACTION:
            continue
        if not is_read:
            unread.add(key)
        if kind == ActivityType.MENTION:
            mentions.add(key)
    return unread, mentions


def build_comments_for_me(
    ctx: ToolContext,
    *,
    days: Any = None,
    limit: Any = None,
    offset: Any = None,
    task_id: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The open tasks with comments waiting on the caller, the newest waiting comment first."""
    now = now or timezone.now()
    window = _int_arg(days, "days", DEFAULT_DAYS, 1, MAX_DAYS)
    cap = _int_arg(limit, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)
    skip = _int_arg(offset, "offset", 0, 0, 10_000)
    only = None
    if task_id not in (None, ""):
        try:
            only = int(task_id)
        except (TypeError, ValueError):
            raise ToolError(f"`task_id` must be a task's numeric id (got {task_id!r}).")
    since = now - timedelta(days=window)

    described = _description_mentions(ctx)
    tasks_qs = _open_involved_tasks(ctx, described, only)
    pool = list(
        TaskComments.objects.filter(
            task_id__in=tasks_qs.values("task_id"),
            is_deleted=False,
            ts_sent_at__gte=since,
            ts_sent_at__lte=now,
        )
        .exclude(sender_id=ctx.user_id)
        .order_by("-ts_sent_at", "-comment_id")
        .values("task_id", "comment_id", "sender_id", "ts_sent_at")[: POOL + 1]
    )
    complete = len(pool) <= POOL
    pool = pool[:POOL]

    candidates = sorted({r["task_id"] for r in pool})
    replied, own, app = _my_comments(ctx, candidates) if candidates else ({}, {}, {})
    reacted = _my_reactions(ctx, candidates) if candidates else set()
    waiting: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in reversed(pool):  # oldest first, as a conversation reads
        last = replied.get(r["task_id"])
        if last is not None and r["ts_sent_at"] <= last:
            continue
        if (r["task_id"], r["comment_id"]) in reacted:
            continue
        waiting[r["task_id"]].append(r)

    ordered = sorted(waiting, key=lambda t: (waiting[t][-1]["ts_sent_at"], t), reverse=True)
    page = ordered[skip : skip + cap]
    # The oldest is usually the question itself; the rest of the room goes to the newest.
    shown = {
        t: rows if len(rows) <= COMMENTS_PER_TASK else [rows[0], *rows[1 - COMMENTS_PER_TASK :]]
        for t, rows in ((t, waiting[t]) for t in page)
    }

    items = _fit(_render(ctx, page, shown, waiting, replied, own, app, described))
    total = sum(len(rows) for rows in waiting.values())
    result: dict[str, Any] = {
        "window": {"since": since.isoformat(), "until": now.isoformat(), "days": window},
        "stats": {"tasks": len(ordered), "comments": total, "complete": complete},
        "tasks": items,
        "__summary__": (
            f"{total} comment(s) waiting on {len(ordered)} task(s)."
            if ordered
            else "No comments waiting in this window."
        ),
    }
    if items and skip + len(items) < len(ordered):
        result["next_offset"] = skip + len(items)
    return result


def _fit(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """As many tasks as fit `ANSWER_CHARS`, at least one."""
    used = 0
    for i, item in enumerate(items):
        used += len(json.dumps(item, indent=2, ensure_ascii=False, default=str))
        if used > ANSWER_CHARS and i:
            return items[:i]
    return items


def _render(ctx, page, shown, waiting, replied, own, app, described) -> list[dict[str, Any]]:
    """The returned tasks, with text and names read for what is returned only."""
    if not page:
        return []
    tasks = {
        t.task_id: t
        for t in TaskMaster.objects.select_related("project").filter(task_id__in=page)
    }
    collaborating = set(
        TaskMaster.collaborators.through.objects.filter(
            customuser_id=ctx.user_id, taskmaster_id__in=page
        ).values_list("taskmaster_id", flat=True)
    )
    milestones = dict(
        MilestoneMaster.objects.filter(task_id__in=page, is_deleted=False).values_list(
            "task_id", "milestone_id"
        )
    )
    of_milestone = set(
        _milestone_task_ids(ctx)
        .filter(milestone__task_id__in=page)
        .values_list("milestone__task_id", flat=True)
    )
    mentioned = described | set(
        _comment_mentions(ctx)
        .filter(message__task_id__in=page)
        .values_list("message__task_id", flat=True)
    )
    wanted = Q()
    for task_id, rows in shown.items():
        ids = [r["comment_id"] for r in rows]
        ids += [c for c in (own.get(task_id), app.get(task_id)) if c is not None]
        wanted |= Q(task_id=task_id, comment_id__in=ids)
    bodies = {
        (task_id, comment_id): body
        for task_id, comment_id, body in TaskComments.objects.filter(wanted).values_list(
            "task_id", "comment_id", "comment_body"
        )
    }
    unread, mentions = _comment_flags(ctx, page)
    names = LabelAccess(ctx).usernames(
        {str(r["sender_id"]) for rows in shown.values() for r in rows if r["sender_id"]}
    )
    mcp = ctx.surface == "mcp"

    out = []
    for task_id in page:
        task = tasks[task_id]
        roles = [
            role
            for role, has in (
                ("assignee", str(task.assignee_id) == ctx.user_id or task_id in of_milestone),
                ("reporter", str(task.reporter_id) == ctx.user_id),
                ("collaborator", task_id in collaborating),
                ("mentioned", task_id in mentioned),
            )
            if has
        ]
        comments = []
        for r in shown[task_id]:
            key = (task_id, r["comment_id"])
            sender = str(r["sender_id"]) if r["sender_id"] else None
            comment: dict[str, Any] = {
                "comment_id": r["comment_id"],
                "at": r["ts_sent_at"].isoformat(),
                "by": wrap_workspace_content(names[sender]) if sender in names else "someone",
                "text": wrap_workspace_content(
                    _clip(extract_text(bodies.get(key)), TEXT_CHARS)
                    or "(no text: an attachment or image)"
                ),
            }
            if key in mentions:
                comment["mentions_you"] = True
            if key in unread:
                comment["unread"] = True
            if mcp and task.project_id:
                comment["url"] = app_links.task_url(task.project_id, task_id, r["comment_id"])
            comments.append(comment)
        row: dict[str, Any] = {
            "task_id": task_id,
            "display_id": task.display_id,
            "title": wrap_workspace_content(task.title or ""),
            "project_id": task.project_id,
            "project": (
                wrap_workspace_content(task.project.project_name) if task.project_id else None
            ),
            "your_roles": roles,
            "comments": comments,
            "comments_omitted": len(waiting[task_id]) - len(comments),
        }
        if task_id in milestones:
            row["milestone_id"] = milestones[task_id]
        if task_id in replied:
            row["your_last_reply_at"] = replied[task_id].isoformat()
        for key, ids in (("your_last_comment", own), ("your_ai_app_comment", app)):
            said = extract_text(bodies.get((task_id, ids.get(task_id))))
            if said:
                row[key] = wrap_workspace_content(_clip(said, 300))
        if mcp and task.project_id:
            row["url"] = app_links.task_url(task.project_id, task_id)
        out.append(row)
    return out
