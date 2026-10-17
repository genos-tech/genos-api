"""`get_task_history` tool — one task's change history, newest first.

Reads `TaskActivity`, which the task signals already write for every
change (`origin/signals/task_signals.py`). Each entry says what changed,
from what to what, who did it, when, and **how it arrived** — the app,
the in-app agent, or an API key (named), so an unattended MCP write can
be told apart from a UI edit by the same person. Changes nobody made by
hand are labelled too: a PR-merge auto-close (no actor, the webhook is
unauthenticated) and the dependency-derived Open↔Blocked flip.

A description edit is recorded without a diff (the body lives on the task
itself), so it reads "description edited" with no old/new values.

ACL contract: same gate as `fetch_task` — tenant guard on
`ctx.team_id`, then `task_acl_user_ids` (project member, assignee or
reporter).
"""

from __future__ import annotations

from typing import Any

from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    wrap_workspace_content,
)

_DEFAULT_LIMIT = 30
_MAX_LIMIT = 100

# Relation-id fields: the raw values are primary keys, so prefer the
# label the signal stored alongside them in `metadata`.
_LABELLED_FIELDS = {"parent_task_id", "milestone_id", "sprint_id"}
# Rows whose old/new values are user-authored text. Wrapped so the model
# treats them as data, never as instructions.
_FREE_TEXT_ACTIONS = {TaskActivityActionType.TITLE, TaskActivityActionType.CREATED}
# Rows whose old/new values are raw user ids, shown as usernames.
_USER_ACTIONS = {TaskActivityActionType.ASSIGNEE, TaskActivityActionType.REPORTER}


def _via(row: TaskActivity) -> dict[str, Any]:
    """How the change arrived. `metadata.via` is stamped by `_record`;
    rows written before it existed, and system writes, have none."""
    meta = row.metadata or {}
    if meta.get("closedByPrMerge"):
        return {"kind": "pr_merge", "pr_url": meta.get("prUrl")}
    if meta.get("derivedFromDependencies"):
        return {"kind": "dependencies"}
    if row.action_type in (
        TaskActivityActionType.PR_LINKED,
        TaskActivityActionType.PR_COMMENT_ADDED,
    ):
        return {"kind": "github", "github_user": meta.get("github_username")}
    via = meta.get("via")
    if isinstance(via, dict) and via.get("kind"):
        return via
    return {"kind": "unknown"}


def _value(row: TaskActivity, side: str, usernames: dict[str, str]) -> Any:
    meta = row.metadata or {}
    raw = row.old_value if side == "old" else row.new_value
    if row.field_name in _LABELLED_FIELDS and raw is not None:
        if display_id := meta.get(f"{side}DisplayId"):
            return display_id
        # A title / sprint name — user-authored, so wrapped like one.
        if label := meta.get(f"{side}Label"):
            return wrap_workspace_content(label)
    if row.action_type in _USER_ACTIONS and raw is not None:
        return usernames.get(str(raw), raw)
    if isinstance(raw, str) and row.action_type in _FREE_TEXT_ACTIONS:
        return wrap_workspace_content(raw)
    if isinstance(raw, dict) and row.action_type == TaskActivityActionType.CREATED:
        return {
            k: wrap_workspace_content(v) if k == "title" and isinstance(v, str) else v
            for k, v in raw.items()
        }
    return raw


def _entry(row: TaskActivity, usernames: dict[str, str]) -> dict[str, Any]:
    actor = row.actor
    entry: dict[str, Any] = {
        "at": row.ts_created_at.isoformat() if row.ts_created_at else None,
        "action": row.action_type,
        "field": row.field_name,
        "old_value": _value(row, "old", usernames),
        "new_value": _value(row, "new", usernames),
        "actor": (
            {"user_id": str(actor.id), "username": actor.username} if actor is not None else None
        ),
        "via": _via(row),
    }
    if row.action_type in (
        TaskActivityActionType.COMMENT_ADDED,
        TaskActivityActionType.COMMENT_EDITED,
        TaskActivityActionType.COMMENT_DELETED,
    ):
        entry["comment_id"] = (row.metadata or {}).get("commentId")
    if row.action_type == TaskActivityActionType.PR_LINKED:
        entry["pr_url"] = (row.metadata or {}).get("pr_url")
    return entry


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_task_id = args.get("task_id")
    try:
        task_id = int(raw_task_id)
    except (TypeError, ValueError):
        raise ToolError(f"`task_id` must be an integer (got {raw_task_id!r}).")

    raw_limit = args.get("limit", _DEFAULT_LIMIT)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        raise ToolError(f"`limit` must be an integer (got {raw_limit!r}).")
    limit = max(1, min(limit, _MAX_LIMIT))

    try:
        task = TaskMaster.objects.select_related("project").get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        raise ToolError(f"Task {task_id} not found.")
    # Tenant guard first — never leak data across teams.
    if str(getattr(task, "team_id", "") or "") != ctx.team_id:
        raise ToolError("Not authorized: task is in a different team.")
    allowed = task_acl_user_ids(
        getattr(task, "project_id", None),
        getattr(task, "assignee_id", None),
        getattr(task, "reporter_id", None),
    )
    if ctx.user_id not in allowed:
        raise ToolError(f"Not authorized to view task {task_id}.")

    # A deleted task's history is still readable — "who deleted this" is
    # exactly the question an audit read exists for.
    rows = list(
        TaskActivity.objects.filter(task_id=task_id)
        .select_related("actor")
        .order_by("-ts_created_at", "-activity_id")[: limit + 1]
    )
    has_more = len(rows) > limit
    rows = rows[:limit]

    from origin.models.common.user_models import CustomUser  # noqa: PLC0415

    user_ids = {
        str(v)
        for r in rows
        if r.action_type in _USER_ACTIONS
        for v in (r.old_value, r.new_value)
        if v
    }
    usernames = (
        {
            str(uid): name
            for uid, name in CustomUser.objects.filter(id__in=user_ids).values_list(
                "id", "username"
            )
        }
        if user_ids
        else {}
    )
    history = [_entry(r, usernames) for r in rows]

    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "history": history,
        "returned_count": len(history),
        "has_more": has_more,
        "__summary__": (
            f"{len(history)} change(s) on {task.display_id}, newest first"
            + (" (older ones not shown — raise `limit`)" if has_more else "")
        ),
    }


GET_TASK_HISTORY = Tool(
    name="get_task_history",
    description=(
        "One task's change history, newest first: each entry's action and "
        "field, old and new value, who made the change, when, and `via` — "
        "how it arrived (`app`, `agent` for Spotlight, `api_key` with the "
        "key's name, `pr_merge`, `dependencies` for an automatic Blocked "
        "flip, `github` for a linked PR or PR comment, or `unknown` for "
        "older rows). Use it for 'who changed this', "
        "'when did this close', or 'why is this Blocked'. A description edit "
        "is listed without its old/new text."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {"type": "INTEGER", "description": "The task's numeric id."},
            "limit": {
                "type": "INTEGER",
                "description": f"Max entries (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT}).",
            },
        },
        "required": ["task_id"],
    },
    run=_run,
)
