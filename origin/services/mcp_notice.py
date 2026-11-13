"""Tell a member what their own AI app did, as it does it (MIP-1432).

Every MCP write runs as the member whose key or connected app it is, and every notification
producer drops the person who acted, which is right for what they typed and wrong for what an
AI app did while they were not watching. This is the one producer that does the opposite: after
a MCP write succeeds, the key's owner gets an `ActivityType.AI_APP` row ("Claude Code created
15 tasks in QR") that rides the same feed, live push and Web Push as anyone else's activity.

**Which writes.** Creates (a task, a task plan, a milestone, a note, a comment, a to-do), status
changes (a task or milestone's status, close and delete included) and the deletes of those
kinds. The small edits (tags, links, deliveries, dependencies, folder moves, favourites,
reactions, calendar events, project settings) would be most of the volume and say little: they
stay silent. `RULES` is the whole list; a tool not in it never notifies.

**One notice per CALL**, not per row: a plan that makes 15 tasks is one notice counting 15, with
up to `MAX_ITEMS` of them named.

**Switches** (MIP-1452). Their own group: `NotificationPreference.enable_ai_apps` is the master, and
`category_settings` has one key per ACTION ("task created", "note deleted", ...; the table is
`ai_app_switches.ACTIONS`). All are on unless the member turned them off, except "note edited"
(an app can edit one note dozens of times in a row). The master and the action's key both gate
the feed row AND the Web Push (a row that is never made cannot be pushed). The in-app agent is not covered: it pauses for the member's approval on every write, so they know.
Plain REST API-key calls are not covered either: there is no app to name.

**Never raises, never slows the call.** It runs after the write has succeeded and swallows its own
errors (log WARNING). The row is excluded from `list_my_unread` (an AI app must not read its own
actions back as news) and is not emailed.

Facts, not sentences: `meta` carries the verb, the kind, the count, the status and the named
items, and the client words them in the member's language. The push text is built here in
English, as every other push is.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.notification_models import NotificationPreference
from origin.models.task.task_models import TaskMaster
from origin.services import ai_app_switches as switches
from origin.services.v3_activity import SURFACE_AI_APP

log = logging.getLogger(__name__)

MAX_ITEMS = 5
TITLE_CHARS = 120
APP_CHARS = 100

VERB_CREATED = "created"
VERB_STATUS = "status"
VERB_COMMENTED = "commented"
VERB_DELETED = "deleted"
VERB_EDITED = "edited"


@dataclass
class Notice:
    verb: str
    kind: str  # task | milestone | note | comment | todo
    count: int
    items: list[dict[str, Any]] = field(default_factory=list)
    status: str | None = None
    # A plan that creates a milestone: how many tasks went with it.
    task_count: int | None = None


def _clip(text: Any) -> str:
    text = str(text or "").strip()
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"


def _task_url(project_id, task_id) -> str:
    if project_id and task_id:
        return f"/workspace/tasks/project/{project_id}/task/{task_id}"
    return "/workspace/tasks"


def _task_facts(task_id) -> dict[str, Any]:
    task = TaskMaster.objects.filter(task_id=task_id).select_related("project").first()
    if task is None:
        return {}
    return {"projectId": task.project_id, "title": task.title, "displayId": task.display_id}


def _task_item(task_id, *, display_id=None, title=None, project_id=None) -> dict[str, Any]:
    if not (display_id and title and project_id):
        facts = _task_facts(task_id)
        display_id = display_id or facts.get("displayId")
        title = title or facts.get("title")
        project_id = project_id or facts.get("projectId")
    return {
        "kind": "task",
        "id": task_id,
        "taskId": task_id,
        "projectId": project_id,
        "displayId": display_id,
        "title": _clip(title),
        "url": _task_url(project_id, task_id),
    }


def _milestone_item(payload: dict[str, Any]) -> dict[str, Any]:
    project_id = payload.get("project_id")
    mid = payload.get("milestone_id")
    return {
        "kind": "milestone",
        "id": mid,
        "taskId": payload.get("task_id"),
        "projectId": project_id,
        "displayId": payload.get("display_id"),
        "title": _clip(payload.get("title")),
        "url": f"/workspace/tasks/project/{project_id}/milestone/{mid}"
        if project_id and mid
        else "/workspace/tasks",
    }


def _note_item(payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    note_type = payload.get("note_type") or args.get("note_type")
    note_id = payload.get("note_id")
    task_id = payload.get("task_id")
    project_id = None
    url = "/workspace/notes"
    if str(note_type) in ("personal", "1") and note_id:
        url = f"/workspace/notes/my/{note_id}"
    elif str(note_type) in ("task", "2") and note_id and task_id:
        project_id = _task_facts(task_id).get("projectId")
        if project_id:
            url = f"/workspace/notes/task/project/{project_id}/task/{task_id}/note/{note_id}"
    return {
        "kind": "note",
        "id": note_id,
        "taskId": task_id,
        "projectId": project_id,
        "title": _clip(payload.get("title")),
        "url": url,
    }


# --- the rules ---------------------------------------------------------------------------
# Each takes the call's resolved arguments and its payload (the dict the tool returned, before
# the result was shaped) and returns a Notice, or None when this call is not worth one.


def _created_task(args, payload) -> Notice | None:
    if not payload.get("task_id"):
        return None
    item = _task_item(
        payload["task_id"],
        display_id=payload.get("display_id"),
        title=payload.get("title"),
        project_id=payload.get("project_id"),
    )
    return Notice(VERB_CREATED, "task", 1, [item])


def _task_plan(args, payload) -> Notice | None:
    tasks = payload.get("tasks") or []
    project_id = payload.get("project_id")
    if args.get("milestone") and payload.get("milestone"):
        milestone = {**payload["milestone"], "project_id": project_id}
        return Notice(
            VERB_CREATED, "milestone", 1, [_milestone_item(milestone)], task_count=len(tasks)
        )
    if not tasks:
        return None
    items = [
        _task_item(
            t.get("task_id"),
            display_id=t.get("display_id"),
            title=t.get("title"),
            project_id=project_id,
        )
        for t in tasks[:MAX_ITEMS]
    ]
    return Notice(VERB_CREATED, "task", len(tasks), items)


def _updated_task(args, payload) -> Notice | None:
    if "status" not in (payload.get("changed_fields") or []) or not payload.get("task_id"):
        return None
    item = _task_item(
        payload["task_id"],
        display_id=payload.get("display_id"),
        title=payload.get("title"),
        project_id=payload.get("project_id"),
    )
    return Notice(VERB_STATUS, "task", 1, [item], status=payload.get("status"))


def _bulk_status(args, payload) -> Notice | None:
    wanted = {
        str(u.get("task_id")): u.get("status")
        for u in (args.get("updates") or [])
        if isinstance(u, dict) and u.get("status")
    }
    rows = [
        r for r in (payload.get("updated") or []) if "status" in (r.get("changed_fields") or [])
    ]
    if not rows:
        return None
    statuses = {wanted.get(str(r.get("task_id"))) for r in rows}
    items = [
        _task_item(
            r.get("task_id"),
            display_id=r.get("display_id"),
            title=r.get("title"),
            project_id=r.get("project_id"),
        )
        for r in rows[:MAX_ITEMS]
    ]
    only = next(iter(statuses)) if len(statuses) == 1 else None
    return Notice(VERB_STATUS, "task", len(rows), items, status=only)


def _milestone(args, payload) -> Notice | None:
    action = payload.get("action")
    if action == "create":
        return Notice(VERB_CREATED, "milestone", 1, [_milestone_item(payload)])
    if action == "update" and "status" in (payload.get("changed_fields") or []):
        return Notice(
            VERB_STATUS, "milestone", 1, [_milestone_item(payload)], status=payload.get("status")
        )
    return None


def _comment(args, payload) -> Notice | None:
    task_id = payload.get("task_id")
    comment_id = payload.get("comment_id")
    if not task_id or not comment_id:
        return None
    task = _task_item(task_id, display_id=payload.get("display_id"))
    task.update(
        {
            "kind": "comment",
            "id": comment_id,
            "commentId": comment_id,
            "url": f"{task['url']}/comment/{comment_id}",
        }
    )
    return Notice(VERB_COMMENTED, "comment", 1, [task])


def _created_note(args, payload) -> Notice | None:
    if not payload.get("note_id"):
        return None
    return Notice(VERB_CREATED, "note", 1, [_note_item(payload, args)])


def _edited_note(args, payload) -> Notice | None:
    # A title or body change is an edit; moving a note between folders is not.
    if not ({"title", "body"} & set(payload.get("changed_fields") or [])):
        return None
    task_id = payload.get("task_id") or (payload.get("parent_context") or {}).get("task_id")
    item = _note_item({**payload, "task_id": task_id}, args)
    return Notice(VERB_EDITED, "note", 1, [item])


def _created_todo(args, payload) -> Notice | None:
    if not payload.get("item_id"):
        return None
    item = {
        "kind": "todo",
        "id": payload["item_id"],
        "title": _clip(payload.get("title")),
        "url": "/workspace/todo",
    }
    return Notice(VERB_CREATED, "todo", 1, [item])


def _deleted(kind: str, make_item: Callable[[dict, dict], dict]):
    def rule(args, payload) -> Notice | None:
        if payload.get("already_deleted") or payload.get("deleted") is False:
            return None
        return Notice(VERB_DELETED, kind, 1, [make_item(args, payload)])

    return rule


def _deleted_comment_item(args, payload) -> dict[str, Any]:
    item = _task_item(payload.get("task_id"), display_id=payload.get("display_id"))
    # The comment is gone: link the task it was on.
    item.update({"kind": "comment", "id": payload.get("comment_id")})
    return item


RULES: dict[str, Callable[[dict, dict], Notice | None]] = {
    "create_task": _created_task,
    "create_task_plan": _task_plan,
    "update_task": _updated_task,
    "update_tasks_bulk": _bulk_status,
    "manage_milestone": _milestone,
    "add_comment": _comment,
    "create_note": _created_note,
    "update_note": _edited_note,
    "create_todo_item": _created_todo,
    "delete_comment": _deleted("comment", _deleted_comment_item),
    "delete_note": _deleted("note", lambda a, p: {**_note_item(p, a), "url": ""}),
    "delete_milestone": _deleted("milestone", lambda a, p: {**_milestone_item(p), "url": ""}),
    "delete_todo_item": _deleted(
        "todo",
        lambda a, p: {
            "kind": "todo",
            "id": p.get("item_id"),
            "title": _clip(p.get("title")),
            "url": "/workspace/todo",
        },
    ),
}


def build_notice(tool_name: str, args: dict[str, Any], payload: dict[str, Any]) -> Notice | None:
    rule = RULES.get(tool_name)
    if rule is None:
        return None
    return rule(args or {}, payload or {})


def sentence(meta: dict[str, Any]) -> str:
    """The notice in English, for the Web Push (the app words its own row in the member's
    language from the same facts)."""
    count = int(meta.get("count") or 1)
    kind = str(meta.get("kind") or "item")
    items = meta.get("items") or []
    first = items[0] if items else {}
    label = first.get("displayId") or first.get("title") or ""
    noun = kind if count == 1 else f"{count} {kind}s"
    target = label if count == 1 and label else noun
    verb = meta.get("verb")
    if verb == VERB_CREATED:
        return f"created {noun}" + (f" {label}" if count == 1 and label else "")
    if verb == VERB_COMMENTED:
        return f"commented on {first.get('displayId') or 'a task'}"
    if verb == VERB_EDITED:
        return f"edited {noun}" + (f" {label}" if count == 1 and label else "")
    if verb == VERB_DELETED:
        return f"deleted {noun}" + (f" {label}" if count == 1 and label else "")
    if verb == VERB_STATUS:
        status = meta.get("status")
        return f"set {target} to {status}" if status else f"changed the status of {target}"
    return f"changed {target}"


def _app_name(credential) -> str:
    """What the member calls the app: a connected app's name, or the name they gave the key.
    (An API key's `label` adds its prefix, for an audit line; a person does not need that.)"""
    name = (
        getattr(credential, "name", None) if getattr(credential, "kind", "") == "api_key" else None
    )
    return str(name or getattr(credential, "label", "") or "AI app")[:APP_CHARS]


def _enabled(user_id, notice: Notice) -> bool:
    """The group's master and the action's own switch. No preference row means the defaults."""
    row = (
        NotificationPreference.objects.filter(user_id=user_id)
        .values_list("enable_ai_apps", "category_settings")
        .first()
    )
    key = switches.key_for(notice.verb, notice.kind)
    if row is None:
        return switches.is_on({}, key)
    master, settings = row
    return bool(master) and switches.is_on(settings, key)


def notify_owner(
    *,
    user,
    team_id,
    credential,
    tool_name: str,
    args: dict[str, Any],
    payload: dict[str, Any],
) -> list[Activity]:
    """After a MCP write succeeded: make the notice, push it. Never raises."""
    try:
        if user is None or not getattr(user, "is_authenticated", False):
            return []
        notice = build_notice(tool_name, args, payload)
        if notice is None or not _enabled(user.id, notice):
            return []
        meta: dict[str, Any] = {
            "aiApp": True,
            "app": _app_name(credential),
            "credentialKind": getattr(credential, "kind", "api_key"),
            "tool": tool_name,
            "verb": notice.verb,
            "kind": notice.kind,
            "count": notice.count,
            "items": notice.items[:MAX_ITEMS],
        }
        if notice.status:
            meta["status"] = notice.status
        if notice.task_count is not None:
            meta["taskCount"] = notice.task_count
        first = notice.items[0] if notice.items else {}
        meta["url"] = first.get("url") or ""
        if first.get("projectId"):
            meta["projectId"] = first["projectId"]
        if first.get("taskId"):
            meta["taskId"] = first["taskId"]
        activity = Activity.objects.create(
            team_id=team_id,
            recipient=user,
            actor=user,
            activity_type=ActivityType.AI_APP,
            surface_type=SURFACE_AI_APP,
            meta=meta,
        )
        # Lazy: the push stack imports the activity helpers.
        from origin.services.webpush_dispatch import schedule_push_for_activities  # noqa: PLC0415

        schedule_push_for_activities([activity])
        # And tell the member's open app now (best effort; the push and the next refetch
        # still find the row if sockets is down).
        from origin.services.realtime_push import push_activity  # noqa: PLC0415

        push_activity(activity)
        return [activity]
    except Exception:  # noqa: BLE001 - a notice never fails the write it follows
        log.warning("MCP notice failed for %s", tool_name, exc_info=True)
        return []
