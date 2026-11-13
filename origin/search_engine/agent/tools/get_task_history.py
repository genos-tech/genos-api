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

A custom-field change is one entry per field: `field` is the field's name
as it was when the change was made, and the values are resolved — option
labels for a tag field, the username for a member field.

Which `old_value` / `new_value` are fenced (MIP-867): the text a person typed
— a title, a username, an attachment's file name, a custom-field value, a tag,
a relation's label, and a status / priority / effort outside the product's own
set. What the model passes back to a write tool stays bare: ids, display ids,
dates, booleans and the known enum values. The model-side fence can't tell the
two apart by key, so it is done here (`model_fence.FENCED_IN_TOOL`).

ACL contract: same gate as `fetch_task` — tenant guard on
`ctx.team_id`, then `task_acl_user_ids` (project member, assignee or
reporter).
"""

from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings

from origin.models.common.api_key_models import ApiKey
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolForbidden,
    wrap_workspace_content,
)
from origin.search_engine.agent.tools.task_enums import (
    VALID_EFFORTS,
    VALID_PRIORITIES,
    VALID_STATUSES,
)

_DEFAULT_LIMIT = 30
_MAX_LIMIT = 100

# Relation-id fields: the raw values are primary keys, so prefer the
# label the signal stored alongside them in `metadata`.
_LABELLED_FIELDS = {"parent_task_id", "milestone_id", "sprint_id", "project_id"}
# Rows whose old/new values are user-authored text. Wrapped so the model
# treats them as data, never as instructions.
_FREE_TEXT_ACTIONS = {TaskActivityActionType.TITLE, TaskActivityActionType.CREATED}
# Rows whose old/new values are raw user ids, shown as usernames. A username
# is typed by its owner (up to 50 characters), so it is wrapped; an id that
# resolves to no one stays a bare id.
_USER_ACTIONS = {TaskActivityActionType.ASSIGNEE, TaskActivityActionType.REPORTER}
# Rows whose old/new value is the uploaded file's name, typed by the uploader.
_FILENAME_ACTIONS = {
    TaskActivityActionType.ATTACHMENT_ADDED,
    TaskActivityActionType.ATTACHMENT_REMOVED,
}
# Rows whose values are enums the model passes back to `update_task` /
# `create_task` verbatim, so they stay bare. But the column is a plain
# CharField and the REST API saves a value outside the set (see the note in
# `update_task`), so a member can put free text here: anything outside the
# product's own vocabulary is wrapped like any other user text.
_ENUM_VOCABULARY = {
    TaskActivityActionType.STATUS: VALID_STATUSES | {"Deleted"},
    TaskActivityActionType.PRIORITY: VALID_PRIORITIES,
    TaskActivityActionType.EFFORT: VALID_EFFORTS,
}


def _enum_or_fenced(action_type: str, value: Any) -> Any:
    """`value` as it is when it is one of the action's known enum values (the
    model passes those back), wrapped when it is any other string."""
    if isinstance(value, str) and value not in _ENUM_VOCABULARY[action_type]:
        return wrap_workspace_content(value)
    return value


# The kinds of `via` the in-app model has always been shown. A new kind of credential
# (a connected app, MIP-1078) is stored as its own kind, but anything that changes what
# this tool returns changes what the model sees, and that waits for the December eval:
# until then it is shown as the nearest thing it already knows, an API key.
_SHOWN_VIA_KINDS = frozenset({"app", "agent", "api_key", "pr_merge", "dependencies", "github"})


def _shown_via(via: dict[str, Any]) -> dict[str, Any]:
    kind = via.get("kind")
    if kind in _SHOWN_VIA_KINDS:
        return via
    if kind == "oauth":
        # The name a client gave itself when it registered: not ours, so fenced like any
        # other text a stranger wrote, and short.
        name = str(via.get("client_name") or "connected app")[:60]
        return {
            "kind": "api_key",
            "key_id": via.get("connection_id"),
            "key_name": wrap_workspace_content(name),
            "key_prefix": None,
        }
    return {"kind": "unknown"}


#: MIP-1090 (default off): which client made a change, and nothing else about the credential.
CLIENT_FLAG = "AGENT_CLIENT_IN_TASK_HISTORY"

#: The sentence the flag adds to the tool's description (`tool_flag_text`).
DESCRIPTION_ADDITION = (
    " `via.kind` says which kind of client made a change: `app` (the Genos UI), `agent`, "
    "`api_key` or `connected_app` (an AI assistant the member signed in to). `via.client` names "
    "the key or the app, and is present only when it is the caller's OWN; for anyone else's you "
    "get the kind and no name."
)


def client_enabled() -> bool:
    """Read at call time, so an eval arm or `override_settings` can flip it."""
    return bool(settings.SEARCH_ENGINE.get(CLIENT_FLAG))


def _uuid_text(raw: Any) -> str | None:
    try:
        return str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        return None


def _own_credentials(rows: list[TaskActivity], ctx: ToolContext) -> set[tuple[str, str]]:
    """`{("api_key", id), ("oauth", id)}`: among the API keys and connected apps these rows
    name, the ones the CALLER owns, which are the only ones whose name is shown. One query
    per kind present on the page, none when the page has neither."""
    wanted: dict[str, set[str]] = {"api_key": set(), "oauth": set()}
    for r in rows:
        via = (r.metadata or {}).get("via")
        if not isinstance(via, dict) or via.get("kind") not in wanted:
            continue
        ident = _uuid_text(via.get("key_id" if via["kind"] == "api_key" else "connection_id"))
        if ident:
            wanted[via["kind"]].add(ident)
    own: set[tuple[str, str]] = set()
    if wanted["api_key"]:
        own |= {
            ("api_key", str(i))
            for i in ApiKey.objects.filter(
                id__in=wanted["api_key"], user_id=ctx.user_id
            ).values_list("id", flat=True)
        }
    if wanted["oauth"]:
        own |= {
            ("oauth", str(i))
            for i in McpConnection.objects.filter(
                id__in=wanted["oauth"], user_id=ctx.user_id
            ).values_list("id", flat=True)
        }
    return own


def _client_via(via: dict[str, Any], own: set[tuple[str, str]]) -> dict[str, Any]:
    """`via` as the flag shows it (owner decision on MIP-1090): the KIND for everyone, and the
    NAME of the key or the app only to the member who owns it. Never a key id, a key prefix, a
    connection id or an undo's run id.

    * `app` / `agent`: the kind alone (the `undo_of` an app row may carry is left out).
    * `api_key`: the kind; `client` (the key's name, typed by its owner, fenced) only when the
      caller owns the key, checked by id against `ApiKey.user`.
    * `oauth`: `connected_app`; `client` (the name the app registered under, 60 characters,
      fenced) only when the caller owns the connection (`McpConnection.user`).
    * every other kind: as it was shown before the flag."""
    kind = via.get("kind")
    if kind in ("app", "agent"):
        return {"kind": kind}
    if kind in ("api_key", "oauth"):
        out: dict[str, Any] = {"kind": "api_key" if kind == "api_key" else "connected_app"}
        ident = _uuid_text(via.get("key_id" if kind == "api_key" else "connection_id"))
        name = via.get("key_name" if kind == "api_key" else "client_name")
        if ident and (kind, ident) in own and isinstance(name, str) and name:
            out["client"] = wrap_workspace_content(name[:60])
        return out
    return _shown_via(via)


def _via(row: TaskActivity, own: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """How the change arrived. `metadata.via` is stamped by `_record`;
    rows written before it existed, and system writes, have none.

    `own` is None with the flag off: `via` is then shown exactly as it always was."""
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
        return _shown_via(via) if own is None else _client_via(via, own)
    return {"kind": "unknown"}


def _custom_field_value(row: TaskActivity, side: str) -> Any:
    """The resolved value the signal snapshotted. Every custom value is
    user-authored — free text, a date typed in, option labels, a
    username — so each string is wrapped."""
    meta = row.metadata or {}
    raw = row.old_value if side == "old" else row.new_value
    label = meta.get(f"{side}Label", raw)
    if label is None:
        return None
    if isinstance(label, list):
        return [wrap_workspace_content(str(item)) for item in label]
    return wrap_workspace_content(str(label))


def _value(row: TaskActivity, side: str, usernames: dict[str, str]) -> Any:
    meta = row.metadata or {}
    raw = row.old_value if side == "old" else row.new_value
    # A description row may carry the whole PREVIOUS body (MIP-738), kept so it
    # can be restored. It is never handed to the model: it is a document, and
    # one that was replaced, possibly because it held an injection. The entry
    # says the body changed and that a previous one is `restorable`, nothing
    # more.
    if row.action_type == TaskActivityActionType.DESCRIPTION:
        return None
    if row.action_type == TaskActivityActionType.CUSTOM_FIELD:
        return _custom_field_value(row, side)
    if row.field_name in _LABELLED_FIELDS and raw is not None:
        if display_id := meta.get(f"{side}DisplayId"):
            return display_id
        # A title / sprint name — user-authored, so wrapped like one.
        if label := meta.get(f"{side}Label"):
            return wrap_workspace_content(label)
    if row.action_type in _USER_ACTIONS and raw is not None:
        username = usernames.get(str(raw))
        return wrap_workspace_content(username) if username else raw
    if isinstance(raw, str) and row.action_type in _FREE_TEXT_ACTIONS:
        return wrap_workspace_content(raw)
    if isinstance(raw, str) and row.action_type in _FILENAME_ACTIONS:
        return wrap_workspace_content(raw)
    if row.action_type in _ENUM_VOCABULARY:
        return _enum_or_fenced(row.action_type, raw)
    if isinstance(raw, dict) and row.action_type == TaskActivityActionType.CREATED:
        return {
            k: (
                wrap_workspace_content(v)
                if k == "title" and isinstance(v, str)
                else _enum_or_fenced(TaskActivityActionType.STATUS, v)
                if k == "status"
                else v
            )
            for k, v in raw.items()
        }
    # Tag names are typed by users; flatten `[{"tagName": …}]` to the
    # names, each wrapped like any other user-authored text.
    if isinstance(raw, list) and row.action_type == TaskActivityActionType.TAGS:
        return [
            wrap_workspace_content(str(tag.get("tagName") if isinstance(tag, dict) else tag))
            for tag in raw
        ]
    return raw


def _field(row: TaskActivity) -> Any:
    if row.action_type == TaskActivityActionType.CUSTOM_FIELD:
        # A project's own field name — typed by a user, so wrapped.
        name = (row.metadata or {}).get("fieldName")
        return wrap_workspace_content(str(name)) if name else row.field_name
    return row.field_name


def _entry(
    row: TaskActivity, usernames: dict[str, str], own: set[tuple[str, str]] | None = None
) -> dict[str, Any]:
    actor = row.actor
    entry: dict[str, Any] = {
        "at": row.ts_created_at.isoformat() if row.ts_created_at else None,
        "action": row.action_type,
        "field": _field(row),
        "old_value": _value(row, "old", usernames),
        "new_value": _value(row, "new", usernames),
        "actor": (
            {"user_id": str(actor.id), "username": actor.username} if actor is not None else None
        ),
        "via": _via(row, own),
    }
    if row.action_type in (
        TaskActivityActionType.COMMENT_ADDED,
        TaskActivityActionType.COMMENT_EDITED,
        TaskActivityActionType.COMMENT_DELETED,
    ):
        entry["comment_id"] = (row.metadata or {}).get("commentId")
    if row.action_type in (
        TaskActivityActionType.PR_LINKED,
        TaskActivityActionType.PR_UNLINKED,
    ):
        entry["pr_url"] = (row.metadata or {}).get("pr_url")
    if row.action_type == TaskActivityActionType.PR_UNLINKED:
        # A branch-only link has no PR; the repo and branch say which one went.
        meta = row.metadata or {}
        entry["repo"] = meta.get("repo")
        entry["branch"] = meta.get("branch")
    if row.action_type == TaskActivityActionType.DESCRIPTION:
        # Whether the body from before this edit was kept, so it can be put
        # back from the task's history in the app (MIP-738).
        entry["restorable"] = bool((row.metadata or {}).get("previousBodyKept"))
        if entry["restorable"]:
            # What `restore_task_body` takes: the OLD text of this edit is the
            # one that comes back.
            entry["activity_id"] = row.activity_id
    # Carried along by a change to another row (a milestone move, a
    # project move, …) rather than edited on this task directly.
    if cascade := (row.metadata or {}).get("cascade"):
        entry["cascade"] = cascade
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
        raise ToolForbidden("Not authorized: task is in a different team.")
    allowed = task_acl_user_ids(
        getattr(task, "project_id", None),
        getattr(task, "assignee_id", None),
        getattr(task, "reporter_id", None),
    )
    if ctx.user_id not in allowed:
        raise ToolForbidden(f"Not authorized to view task {task_id}.")

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
    own = _own_credentials(rows, ctx) if client_enabled() else None
    history = [_entry(r, usernames, own) for r in rows]

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
        "older rows). An entry with `cascade` was carried along by a change "
        "to another row — a milestone or project move, a milestone delete or "
        "sprint change — not edited on this task. Use it for 'who changed this', "
        "'when did this close', or 'why is this Blocked'. A description edit "
        "is listed without its old/new text. A custom-field change "
        "(`custom_field_changed`) names the field in `field`, one entry per field."
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
