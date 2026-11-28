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
`ai_app_switches.ACTIONS`). Only the risky ones start on (MIP-2009): a delete, or any call that
changes 10+ items; the rest are off until the member turns them on (they show in the Today card). The master and the action's key both gate
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

from django.db import transaction

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.notification_models import NotificationPreference
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL
from origin.services import ai_app_switches as switches
from origin.services.mcp_client_family import credential_family
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
# The risky kinds that are not a write of an item (MIP-2063).
VERB_SHARING = "sharing"  # who can open a note
VERB_MEMBERS = "members"  # who is in a project
VERB_REFUSED = "refused"  # a burst of refused calls
VERB_CONNECTED = "connected"  # a new app or API key


@dataclass
class Notice:
    verb: str
    kind: str  # task | milestone | note | comment | todo
    count: int
    items: list[dict[str, Any]] = field(default_factory=list)
    status: str | None = None
    # A plan that creates a milestone: how many tasks went with it.
    task_count: int | None = None
    # What was done, for a verb that covers several acts (`share` | `unshare`, `add` | `set_role`
    # | `remove`, `oauth` | `api_key`): a fact for the client's wording, never a sentence.
    action: str | None = None
    # More facts for the row, merged into `meta` (a refusal burst: how many in how many seconds).
    extra: dict[str, Any] = field(default_factory=dict)


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


def _note_place(family: str, note_id, task_id) -> tuple[str, Any, Any]:
    """`(url, project_id, task_id)` of a note. The app opens a notice's note in a modal from the
    url alone (MIP-1755), and not every write's answer says where its note sits (a comment, a
    block edit or a share names only the note), so a task or chat note is read from its row."""
    fallback = ("/workspace/notes", None, task_id)
    if not note_id:
        return fallback
    # A Team Notes or shared note is a personal note; its id opens it.
    if family in ("personal", "team", "shared", "1"):
        return f"/workspace/notes/my/{note_id}", None, task_id
    if family in ("task", "2"):
        row = (
            TaskNoteMaster.objects.filter(note_id=note_id)
            .values_list("project_id", "task_id")
            .first()
        )
        project_id, task_id = row if row else (None, task_id)
        if task_id and not project_id:
            project_id = _task_facts(task_id).get("projectId")
        if not (project_id and task_id):
            return fallback
        url = f"/workspace/notes/task/project/{project_id}/task/{task_id}/note/{note_id}"
        return url, project_id, int(task_id)
    if family in ("chat", "3"):
        row = (
            ChatNoteMaster.objects.filter(note_id=note_id)
            .values_list("chat_type", "channel_id", "thread_root_id")
            .first()
        )
        if row and row[0] in CHAT_TYPE_LABEL and row[1]:
            chat_type, channel_id, thread_root_id = row
            url = (
                f"/workspace/notes/chat/{CHAT_TYPE_LABEL[chat_type]}/{channel_id}"
                f"/thread/{thread_root_id or 0}/note/{note_id}"
            )
            return url, None, task_id
    return fallback


def _note_item(payload: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    note_type = payload.get("note_type") or args.get("note_type")
    note_id = payload.get("note_id")
    url, project_id, task_id = _note_place(str(note_type), note_id, payload.get("task_id"))
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


def _commented_note(args, payload) -> Notice | None:
    """A comment thread started or answered on a note: one notice, linking the thread so a click
    opens the note with its comment card (`?comment=`, MIP-146)."""
    if not payload.get("note_id") or not payload.get("thread_id"):
        return None
    item = _note_item(payload, args)
    if item["url"] and item["url"] != "/workspace/notes":
        item["url"] = f"{item['url']}?comment={payload['thread_id']}"
    return Notice(VERB_COMMENTED, "note", 1, [item])


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
    # The day the item sits under is part of its link: without it only the whole list can open.
    if local_date := payload.get("local_date"):
        item["localDate"] = local_date
        item["url"] = f"/workspace/todo/{local_date}/item/{payload['item_id']}"
    return Notice(VERB_CREATED, "todo", 1, [item])


def _note_sharing(args, payload) -> Notice | None:
    """A note shared with a person, or a share taken away. The tool's answer carries ids and no
    title, and it is the model's own view (not changed for a notice), so the notice names neither
    the note nor the person: the row says "shared a note" and the note's access list is one click
    away. A re-share with the same role is noticed too: the tool does not say it changed nothing."""
    if not payload.get("note_id") or payload.get("action") not in ("share", "unshare"):
        return None
    item = _note_item(
        {**payload, "note_type": payload.get("note_type") or args.get("note_type")}, args
    )
    return Notice(VERB_SHARING, "note", 1, [item], action=payload["action"])


def _project_members(args, payload) -> Notice | None:
    """A person added to a project, their role changed, or removed. An `add` of someone already in
    it changed nothing, and neither did a `set_role` to the role they already had."""
    project_id = payload.get("project_id")
    action = args.get("action")
    if not project_id or action not in ("add", "set_role", "remove"):
        return None
    if payload.get("already_member"):
        return None
    if action == "set_role" and payload.get("previous_role") == payload.get("role"):
        return None
    return Notice(VERB_MEMBERS, "project", 1, [_project_item(project_id)], action=action)


def _approved_join(args, payload) -> Notice | None:
    """A request to join a project approved: the person is let in (a member change)."""
    project_id = payload.get("project_id")
    if not project_id or payload.get("already_member"):
        return None
    return Notice(VERB_MEMBERS, "project", 1, [_project_item(project_id)], action="add")


def _project_item(project_id) -> dict[str, Any]:
    project = ProjectMaster.objects.filter(project_id=project_id).first()
    return {
        "kind": "project",
        "id": project_id,
        "projectId": project_id,
        "displayId": getattr(project, "code", None),
        "title": _clip(getattr(project, "project_name", "")),
        "url": f"/workspace/tasks/project/{project_id}",
    }


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


# `manage_milestone` `convert` (a task into a milestone, a milestone into a task) has NO rule here,
# on purpose (MIP-1597): a conversion is the app's own structural act on something the member asked
# for in this very call, and its result (the new id, what stayed under it) is in the tool's answer
# they are reading, so a "changed in your AI app" notice would repeat it. A rule would also need a
# new notification switch, which is not worth it for that. A project move of a milestone makes none
# either (`_milestone` only notices a create and a status change). Pinned by
# `tests/test_move_leftovers.py::ConvertMakesNoAiAppNoticeTests`.
RULES: dict[str, Callable[[dict, dict], Notice | None]] = {
    "create_task": _created_task,
    "create_task_plan": _task_plan,
    "update_task": _updated_task,
    "update_tasks_bulk": _bulk_status,
    "manage_milestone": _milestone,
    "add_comment": _comment,
    "create_note": _created_note,
    # An HTML page saved as a note (MIP-2214): a new note is a creation, a replaced page an edit.
    "save_html_note": lambda args, payload: (
        _created_note(args, payload) if payload.get("created") else _edited_note(args, payload)
    ),
    # A comment on a note by an AI app: the key's owner is told, as for a task comment. Starting a
    # thread and answering one are the same act to the person reading the feed, so one switch.
    "comment_on_note": _commented_note,
    "reply_to_note_comment": _commented_note,
    "update_note": _edited_note,
    # One block of a note is still an edit of the note: the same notice, under the same switch.
    "edit_note_block": _edited_note,
    "create_todo_item": _created_todo,
    # Who can open a note and who is in a project: risky, so on by default (MIP-2063).
    "manage_note_sharing": _note_sharing,
    "manage_project_members": _project_members,
    "approve_join_request": _approved_join,
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
    if verb == VERB_COMMENTED and kind == "note":
        return f"commented on note {label}" if label else "commented on a note"
    if verb == VERB_COMMENTED:
        return f"commented on {first.get('displayId') or 'a task'}"
    if verb == VERB_EDITED:
        return f"edited {noun}" + (f" {label}" if count == 1 and label else "")
    if verb == VERB_DELETED:
        return f"deleted {noun}" + (f" {label}" if count == 1 and label else "")
    action = meta.get("action")
    if verb == VERB_SHARING:
        what = f"note {label}" if label else "a note"
        return f"stopped sharing {what}" if action == "unshare" else f"shared {what}"
    if verb == VERB_MEMBERS:
        what = f"project {label}" if label else "a project"
        if action == "remove":
            return f"removed someone from {what}"
        if action == "set_role":
            return f"changed a member's role in {what}"
        return f"added someone to {what}"
    if verb == VERB_REFUSED:
        return "was refused many requests in a minute"
    if verb == VERB_CONNECTED:
        return "API key was created" if action == "api_key" else "was connected to your account"
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
        return switches.allows({}, key, notice.count)
    master, settings = row
    return bool(master) and switches.allows(settings, key, notice.count)


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
        return [_emit(user, team_id, credential, tool_name, notice)]
    except Exception:  # noqa: BLE001 - a notice never fails the write it follows
        log.warning("MCP notice failed for %s", tool_name, exc_info=True)
        return []


def _owner_team_id(user_id) -> str | None:
    """A team to file a notice under when the credential names none (a personal API key): the
    owner's earliest live membership."""
    team_id = (
        TeamMembers.objects.filter(
            attendee_id=user_id, is_deleted=False, team__isnull=False, team__is_deleted=False
        )
        .order_by("ts_joined_at", "id")
        .values_list("team_id", flat=True)
        .first()
    )
    return str(team_id) if team_id else None


def connected_notice(credential, *, action: str) -> Notice:
    """A new app (`oauth`) or API key (`api_key`) was connected to the member's account."""
    item = {"kind": "app", "id": str(credential.id), "title": _app_name(credential), "url": ""}
    return Notice(VERB_CONNECTED, "app", 1, [item], action=action)


def refusal_burst_notice(refused: int, window_seconds: int) -> Notice:
    """The credential was refused `refused` requests inside `window_seconds`: what walking through
    other people's ids looks like. `count` stays 1 (it counts items, and 10+ would read as a
    bulk change); the number is a fact of its own."""
    return Notice(
        VERB_REFUSED,
        "burst",
        1,
        extra={"refused": int(refused), "windowSeconds": int(window_seconds)},
    )


def notify_key_leaked(key) -> list[Activity]:
    """An API key was found in public and revoked (MIP-2271). Unlike the other credential notices
    this ignores the member's switches: it is the one notice that says a secret of theirs leaked,
    and it fires once per key. The client words an unknown verb with its generic line; `extra`
    carries the facts. Never raises."""
    try:
        user = key.user
        team_id = key.pinned_team_id or _owner_team_id(user.id)
        if team_id is None:
            return []
        item = {"kind": "app", "id": str(key.id), "title": _app_name(key), "url": ""}
        notice = Notice("revoked", "app", 1, [item], action="leaked_key")
        with transaction.atomic():
            return [_emit(user, team_id, key, "github_secret_scanning", notice)]
    except Exception:  # noqa: BLE001 - a notice never fails what it follows
        log.warning("Leaked-key notice failed", exc_info=True)
        return []


def notify_credential_event(*, credential, notice: Notice, tool_name: str) -> list[Activity]:
    """A notice about the credential itself, not a tool call (MIP-2063): a new app or API key
    connected, or a burst of refused calls. Goes to the credential's owner under the same
    switches and the same feed and push as a write's notice. Never raises."""
    try:
        user = getattr(credential, "user", None)
        if user is None or not getattr(user, "is_authenticated", True):
            return []
        team_id = getattr(credential, "pinned_team_id", None) or getattr(
            credential, "default_team_id", None
        )
        team_id = team_id or _owner_team_id(user.id)
        if team_id is None or not _enabled(user.id, notice):
            return []
        # A savepoint: the callers run inside a request transaction, and a failed insert must not
        # poison it for the consent or key creation it follows.
        with transaction.atomic():
            return [_emit(user, team_id, credential, tool_name, notice)]
    except Exception:  # noqa: BLE001 - a notice never fails what it follows
        log.warning("Credential notice failed for %s", tool_name, exc_info=True)
        return []


def _emit(user, team_id, credential, tool_name: str, notice: Notice) -> Activity:
    """Make the feed row and push it. The caller has checked the switches."""
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
    if notice.action:
        meta["action"] = notice.action
    meta.update(notice.extra)
    if switches.is_bulk(notice.count):
        meta["bulk"] = True
    # The official family of the connected app (`claude` | `chatgpt`), for its icon on the
    # notice (MIP-1571); absent for any other app, an API key, and every older notice. The
    # notice is the owner's alone (recipient == actor), who is also the one reader of the
    # app's name above, so `via_privacy.SHOW_OAUTH_APP_TO_EVERYONE` has nothing to decide.
    if family := credential_family(credential):
        meta["appFamily"] = family
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
    return activity
