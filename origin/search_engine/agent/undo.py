"""Undo for an approved agent write: which writes can be undone, and what is kept to do it.

A person approves a write and the agent runs it. Undo is the person's way back,
without asking the model for anything: the answer carries an Undo control, one
click puts back what that one write changed, and nothing else.

**What a write keeps.** A tool in scope computes a PRE-IMAGE while it runs (the
values it is about to replace, and a stamp of the state it leaves behind) and
returns it under `__undo__`, which the controller pops, exactly as it pops
`__summary__`, and stores on `AgentStep.undo_json`. It is NEVER in `result_json`:
that is replayed to the model as the tool's own output (the model-visible
function response is identical with and without capture) and is blanked by
retention, and a pre-image is neither. A tool captures only when its context says
so (`ToolContext.capture_undo`, set by the approve path alone), so MCP, the evals
and every direct caller get exactly the result they always got.

**What a write does NOT keep: text.** A task body or a note body is not copied
into `undo_json`. The history already keeps the text an agent write replaces (the
`description_edited` row for a task, a version row for a note), under its own
retention, and the pre-image points at it: ids, version numbers and hashes.

**State-based, not time-based.** Undo is offered for `UNDO_WINDOW` after the write
(measured from the write, not from the proposal), and only while nothing has
changed since: every guard compares what is there NOW with the stamp the write
left, and a mismatch is `changed_since`. Availability is therefore checked at undo
time, never in the payload that restores a conversation. `update_task` is
field-by-field: a field somebody else changed since is skipped and reported, the
rest is put back.

**The map.** `UNDO_SUPPORT` names every approval-gated tool (`full`, `partial`
or `none`) in the style of `tools/categories.py` and `approval_edits.EDITABLE_FIELDS`,
and `test_agent_undo_map` enforces it in both directions, so a new write tool
cannot be added without deciding. A tool with no undo says why, with a code the
approval card shows BEFORE the person approves ("this can't be undone"). A
`partial` tool decides per call, from its arguments (`proposal`).

**What undo cannot recall:** notifications, mentions, emails and pushes already
sent, outgoing webhooks already delivered, a calendar event already synced, a
display id (`PRJ-12`) already used.

KNOWN LIMITATION: undo does not tell the model. After an undo the next turn's
replay still shows the write as done, so the model may speak as if it stands.
Telling it changes what it sees, which needs an eval first; it is a follow-up.

This module is imported by the tools, so it must not import the tool registry.
Executing an undo is `undo_apply`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from django.db.models import Max
from django.utils import timezone

from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster

log = logging.getLogger(__name__)

# The key a tool returns its pre-image under; the controller pops it.
UNDO_KEY = "__undo__"

# How long after the write an undo is offered. Below any configured retention of
# `agent_runs`, whose delete takes the step (and its undo) with it.
UNDO_WINDOW = timedelta(days=7)

# The shape of `undo_json`. A reader refuses a version it does not know.
SCHEMA_VERSION = 1

# What a stored undo may weigh. It is ids, hashes and a few short values; a
# pre-image past this is a bug, and is dropped rather than stored.
MAX_STORED_BYTES = 64 * 1024

# --------------------------------------------------------------------------- #
# The map                                                                     #
# --------------------------------------------------------------------------- #

FULL = "full"  # every call of the tool is undoable
PARTIAL = "partial"  # decided per call, from its arguments
NONE = "none"  # never

LEVELS = (FULL, PARTIAL, NONE)

# Why a write cannot be undone: what the card says before the person approves.
IRREVERSIBLE_DELETE = "irreversible_delete"  # it deletes for good
NOTIFIED_PEOPLE = "notified_people"  # other people are told, or given access
NO_SNAPSHOT = "no_snapshot"  # the old values are not kept anywhere
UNSUPPORTED_FIELDS = "unsupported_fields"  # this call touches what undo cannot restore
NOT_YET = "not_yet"  # restorable in principle; not built

REASON_CODES = (
    IRREVERSIBLE_DELETE,
    NOTIFIED_PEOPLE,
    NO_SNAPSHOT,
    UNSUPPORTED_FIELDS,
    NOT_YET,
)

# tool name -> level. EVERY approval-gated tool is a key; `test_agent_undo_map`
# holds it to the registry in both directions.
UNDO_SUPPORT: dict[str, str] = {
    # --- tasks ---
    "create_task": FULL,  # undo = delete it the way the app does (status "Deleted")
    "update_task": PARTIAL,  # title, status, priority, effort, dates, tags, body
    "restore_task_body": FULL,  # undo = restore the body it replaced
    "add_comment": FULL,  # undo = delete the comment (soft, as the app does)
    "update_comment": NONE,
    "delete_comment": NONE,
    "update_tasks_bulk": NONE,
    "create_task_plan": NONE,
    "assign_task": NONE,
    "add_task_delivery": NONE,
    "remove_task_delivery": NONE,
    "update_task_dependencies": NONE,
    # --- notes ---
    "create_note": FULL,  # undo = delete the note it made (hard delete: nothing else has it)
    "update_note": PARTIAL,  # title and body; a folder move is not covered
    "restore_note_version": FULL,  # undo = restore the state it replaced
    "delete_note": NONE,
    "delete_note_folder": NONE,
    "manage_note_folder": NONE,
    "favorite_note": NONE,
    "manage_note_sharing": NONE,
    # --- to-dos ---
    "create_todo_item": NONE,
    "update_todo_item": NONE,
    "delete_todo_item": NONE,
    # --- calendar ---
    "create_calendar_event": NONE,
    "update_calendar_event": NONE,
    "delete_calendar_event": NONE,
    # --- milestones, sprints, projects ---
    "manage_milestone": NONE,
    "delete_milestone": NONE,
    "manage_sprint": NONE,
    "delete_sprint": NONE,
    "manage_project": NONE,
    "manage_project_members": NONE,
    "add_project_tag": NONE,
    "manage_project_labels": NONE,
    "approve_join_request": NONE,
    "decline_join_request": NONE,
    "delete_project": NONE,
}

# tool name -> (reason code, one line) for every tool that is not `full`. The
# line is for people reading this file and the PR; the card shows copy for the
# code. A `partial` tool's code is the one a refused call gets.
UNDO_REASONS: dict[str, tuple[str, str]] = {
    "update_task": (
        UNSUPPORTED_FIELDS,
        "a call that touches collaborators, custom fields, or a move to another "
        "project, parent or milestone (or a milestone's own task) has no stored old value",
    ),
    "update_note": (
        UNSUPPORTED_FIELDS,
        "a folder move is not covered; title and body are put back from the note's versions",
    ),
    "update_comment": (NO_SNAPSHOT, "the comment's previous text is not kept"),
    "delete_comment": (
        NOT_YET,
        "a soft delete, so recoverable in principle; the restore is not built",
    ),
    "update_tasks_bulk": (NOT_YET, "per-row undo of a bulk update is a follow-up"),
    "create_task_plan": (NOT_YET, "undoing a milestone with its tasks as one step is a follow-up"),
    "assign_task": (NOTIFIED_PEOPLE, "the new assignee is told, and the old one is not told back"),
    "add_task_delivery": (NOT_YET, "unlinking what it linked is not built"),
    "remove_task_delivery": (NOT_YET, "relinking what it removed is not built"),
    "update_task_dependencies": (NOT_YET, "putting the old dependencies back is not built"),
    "delete_note": (
        IRREVERSIBLE_DELETE,
        "a hard delete: the note, its versions and its index entries go",
    ),
    "delete_note_folder": (
        IRREVERSIBLE_DELETE,
        "a hard delete of the folder and every note in it",
    ),
    "manage_note_folder": (NO_SNAPSHOT, "the folder's old name and place are not kept"),
    "favorite_note": (NOT_YET, "a toggle; putting it back is not built"),
    "manage_note_sharing": (NOTIFIED_PEOPLE, "people are given or lose access, and are told"),
    "create_todo_item": (NOT_YET, "a to-do has no history; deleting it again is not built"),
    "update_todo_item": (NO_SNAPSHOT, "a to-do keeps no history of its old values"),
    "delete_todo_item": (IRREVERSIBLE_DELETE, "a hard delete: a to-do keeps no history"),
    "create_calendar_event": (NOTIFIED_PEOPLE, "the guests are invited by the calendar"),
    "update_calendar_event": (NO_SNAPSHOT, "the event is not read before it is changed"),
    "delete_calendar_event": (IRREVERSIBLE_DELETE, "the calendar deletes the event for good"),
    "manage_milestone": (NO_SNAPSHOT, "the milestone's old values are not kept"),
    "delete_milestone": (IRREVERSIBLE_DELETE, "it detaches its tasks and removes it"),
    "manage_sprint": (NO_SNAPSHOT, "the sprint's old values are not kept"),
    "delete_sprint": (IRREVERSIBLE_DELETE, "it removes the sprint and detaches its tasks"),
    "manage_project": (NO_SNAPSHOT, "the project's old values are not kept"),
    "manage_project_members": (NOTIFIED_PEOPLE, "people are added or removed, and are told"),
    "add_project_tag": (NOT_YET, "removing the tag it added is not built"),
    "manage_project_labels": (NO_SNAPSHOT, "the labels' old values are not kept"),
    "approve_join_request": (NOTIFIED_PEOPLE, "the person is let in and told"),
    "decline_join_request": (NOTIFIED_PEOPLE, "the person is told no"),
    "delete_project": (IRREVERSIBLE_DELETE, "it removes the project and everything in it"),
}


@dataclass(frozen=True)
class Proposal:
    """Whether a PROPOSED call is undoable, for the approval card.

    A prediction: the result of the approved write says whether capture
    really happened (`undo` on `tool_call_result`), and that is what decides
    whether Undo is shown.
    """

    undoable: bool
    reason: str | None = None


# The arguments of the `partial` tools, split by whether undo can put them back.
# Together they are the tool's whole schema (`test_agent_undo_map` holds that), so
# a new argument must be classified here before it ships.
UPDATE_TASK_RESTORABLE_ARGS = frozenset(
    {
        "task_id",
        "title",
        "content_text",
        "body_append",
        "body_prepend",
        "body_replace",
        "base_body_hash",
        "status",
        "priority",
        "effort_level",
        "due_date",
        "start_date",
        "tags",
    }
)
UPDATE_TASK_UNRESTORABLE_ARGS = frozenset(
    {"collaborators", "custom_fields", "milestone_id", "parent_task_id", "project_id"}
)
UPDATE_NOTE_RESTORABLE_ARGS = frozenset({"note_id", "note_type", "title", "content_text"})
UPDATE_NOTE_UNRESTORABLE_ARGS = frozenset({"folder_id"})

_TASK_BODY_ARGS = ("content_text", "body_append", "body_prepend", "body_replace")


def update_task_blocker(args: dict[str, Any], *, is_milestone: bool | None = None) -> str | None:
    """The reason code why this `update_task` call cannot be undone, or None.

    `is_milestone` is whether the task is a milestone's backing row, whose
    values live on the milestone and are mirrored down: restoring the mirror
    alone is undone by the milestone's next edit. None = not known (a proposal
    for a task that is not there is the tool's to refuse).
    """
    for key, value in args.items():
        if value is None:
            continue
        if key in UPDATE_TASK_UNRESTORABLE_ARGS or key not in UPDATE_TASK_RESTORABLE_ARGS:
            return UNSUPPORTED_FIELDS
    if is_milestone:
        return UNSUPPORTED_FIELDS
    return None


def update_note_blocker(args: dict[str, Any]) -> str | None:
    """The reason code why this `update_note` call cannot be undone, or None.
    A `folder_id` present at all, even null (= out of every folder), is a move."""
    if "folder_id" in args:
        return UNSUPPORTED_FIELDS
    for key, value in args.items():
        if value is not None and key not in UPDATE_NOTE_RESTORABLE_ARGS:
            return UNSUPPORTED_FIELDS
    return None


def _is_milestone_task(raw_task_id: Any) -> bool | None:
    """Best-effort: whether `task_id` is a milestone's backing task. None when
    it cannot be told (no such task, a bad id, no database): the proposal is then
    judged on its arguments alone and the tool decides again when it runs."""
    try:
        task_id = int(raw_task_id)
        return (
            TaskMaster.objects.filter(task_id=task_id)
            .values_list("is_milestone", flat=True)
            .first()
        )
    except Exception:  # noqa: BLE001
        return None


def proposal(tool_name: str, args: dict[str, Any] | None) -> Proposal:
    """Whether a call the model proposed would be undoable, and if not, the code why.

    An unknown tool is not undoable (and gets no reason beyond `no_snapshot`):
    the map is what promises an undo, and it is held to the registry by a test.
    """
    args = args if isinstance(args, dict) else {}
    level = UNDO_SUPPORT.get(tool_name)
    if level is None:
        return Proposal(False, NO_SNAPSHOT)
    if level == NONE:
        return Proposal(False, UNDO_REASONS[tool_name][0])
    if level == PARTIAL:
        if tool_name == "update_task":
            reason = update_task_blocker(args, is_milestone=_is_milestone_task(args.get("task_id")))
        elif tool_name == "update_note":
            reason = update_note_blocker(args)
        else:  # a new `partial` tool must say how it decides
            reason = UNDO_REASONS.get(tool_name, (NO_SNAPSHOT, ""))[0]
        if reason:
            return Proposal(False, reason)
    return Proposal(True, None)


# --------------------------------------------------------------------------- #
# What is stored                                                              #
# --------------------------------------------------------------------------- #


def stored(tool_name: str, payload: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The `undo_json` for what a tool returned under `__undo__`, or None when
    there is nothing storable (not a dict with an `image`, or too heavy)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("image"), dict):
        return None
    body = {
        "v": SCHEMA_VERSION,
        "tool": tool_name,
        "captured_at": (now or timezone.now()).isoformat(),
        "entity": payload.get("entity") or {},
        "image": payload["image"],
    }
    try:
        size = len(json.dumps(body, default=str))
    except (TypeError, ValueError):
        log.warning("undo for %s is not JSON-serializable; not stored", tool_name)
        return None
    if size > MAX_STORED_BYTES:
        log.warning("undo for %s weighs %d bytes; not stored", tool_name, size)
        return None
    return body


def captured_at(undo_json: dict[str, Any] | None) -> datetime | None:
    try:
        value = datetime.fromisoformat(str((undo_json or {})["captured_at"]))
    except (KeyError, ValueError):
        return None
    return value if timezone.is_aware(value) else timezone.make_aware(value)


def available_until(undo_json: dict[str, Any] | None) -> datetime | None:
    """When the undo stops being offered: `UNDO_WINDOW` after the write."""
    at = captured_at(undo_json)
    return at + UNDO_WINDOW if at is not None else None


def is_expired(undo_json: dict[str, Any] | None, *, now: datetime | None = None) -> bool:
    until = available_until(undo_json)
    return until is None or (now or timezone.now()) >= until


def step_info(step: Any) -> dict[str, Any] | None:
    """What the browser is told of a step's undo, or None when it has none.
    Not whether it can still be done: that is checked when it is tried."""
    undo_json = getattr(step, "undo_json", None)
    until = available_until(undo_json)
    if not undo_json or until is None:
        return None
    return {
        "step_id": step.step_id,
        "step": step.step_index,
        "tool_name": step.tool_name,
        "available_until": until.isoformat(),
        "undone_at": step.undone_at.isoformat() if step.undone_at else None,
    }


def event_info(step: Any) -> dict[str, Any] | None:
    """The `undo` of a `tool_call_result` event: the step to undo, and until when.
    (Not `undone_at`: a write that has just run has not been undone.)"""
    info = step_info(step)
    if info is None:
        return None
    return {"step_id": info["step_id"], "available_until": info["available_until"]}


# --------------------------------------------------------------------------- #
# Capture helpers (used by the tools; they read, they never write)            #
# --------------------------------------------------------------------------- #


def state_hash(value: Any) -> str:
    """A stable fingerprint of a JSON-like value."""
    canonical = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def jsonable(value: Any) -> Any:
    """`value` as plain JSON (dates as ISO strings, UUIDs as strings), copied."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def tags_key(value: Any) -> Any:
    """Tags as they are compared: a list whose order the UI does not keep,
    with None and [] the same (the history signal's rule, `_normalise_tags`)."""
    if not value:
        return None
    if isinstance(value, list):
        return sorted(json.dumps(tag, sort_keys=True, default=str) for tag in value)
    return value


# The `TaskMaster` columns an `update_task` undo puts back, and nothing else.
TASK_COLUMNS = ("title", "status", "priority", "effort_level", "due_date", "start_date", "tags")


def column_key(column: str, value: Any) -> Any:
    """A column's JSON value as it is compared."""
    return tags_key(value) if column == "tags" else value


def latest_activity_id(task_id: int) -> int:
    """The newest history row of a task (0 when it has none): a mark to look past."""
    return (
        TaskActivity.objects.filter(task_id=task_id).aggregate(newest=Max("activity_id"))["newest"]
        or 0
    )


def task_entity(task: Any) -> dict[str, Any]:
    return {
        "kind": "task",
        "task_id": int(task.task_id),
        "project_id": task.project_id,
        "display_id": task.display_id,
    }


class BodyCapture:
    """The task body a write is about to replace, kept so it can be put back.

    Take it BEFORE the write, `finish()` it after. The text itself is not copied:
    the history keeps it. An unattended rewrite leaves a `description_edited` row
    holding the old body (`task_signals._keeps_previous_body`), and this asks for
    that row to be kept (`_keep_previous_body`, which also holds when the write
    arrives by a surface the signal does not keep for) and then FINDS it, checking
    it holds exactly the body that was replaced. A task with no body at all leaves
    no row to find, and needs none: undo puts the empty body back.
    """

    def __init__(self, task: Any):
        self.task_id = int(task.task_id)
        self.before = copy.deepcopy(task.content)
        self.mark = latest_activity_id(self.task_id)
        task._keep_previous_body = True

    def finish(self) -> dict[str, Any] | None:
        """How to put the body back, or None when it cannot be (then the call is
        not undoable: half an undo, with the body left behind, would mislead)."""
        content = (
            TaskMaster.objects.filter(task_id=self.task_id)
            .values_list("content", flat=True)
            .first()
        )
        after_hash = state_hash(content)
        if self.before is None:
            return {"mode": "none", "activity_id": None, "after_hash": after_hash}
        if not isinstance(self.before, list):
            return None
        row = (
            TaskActivity.objects.filter(
                task_id=self.task_id,
                action_type=TaskActivityActionType.DESCRIPTION,
                activity_id__gt=self.mark,
                metadata__previousBodyKept=True,
            )
            .order_by("-activity_id")
            .first()
        )
        if row is None or state_hash(row.old_value) != state_hash(self.before):
            return None
        return {"mode": "row", "activity_id": row.activity_id, "after_hash": after_hash}


class TaskUpdateCapture:
    """The pre-image of one `update_task` call. Take it before the first field
    changes (after the ACL checks); `finish(update_fields)` once it is saved."""

    def __init__(self, task: Any, args: dict[str, Any]):
        self.task = task
        self.blocker = update_task_blocker(args, is_milestone=bool(task.is_milestone))
        self.before: dict[str, Any] = {}
        self.body: BodyCapture | None = None
        if self.blocker:
            return
        self.before = {column: jsonable(getattr(task, column)) for column in TASK_COLUMNS}
        if any(args.get(key) is not None for key in _TASK_BODY_ARGS):
            self.body = BodyCapture(task)

    def finish(self, update_fields: list[str]) -> dict[str, Any] | None:
        if self.blocker:
            return None
        columns = [c for c in TASK_COLUMNS if c in update_fields]
        body = None
        if "content" in update_fields:
            body = self.body.finish() if self.body is not None else None
            if body is None:
                return None
        if not columns and body is None:
            return None
        row = (
            TaskMaster.objects.filter(task_id=self.task.task_id).values(*columns).first()
            if columns
            else {}
        ) or {}
        fields = {
            column: {"before": self.before[column], "after": jsonable(row.get(column))}
            for column in columns
        }
        return {
            "entity": task_entity(self.task),
            "image": {"task_id": int(self.task.task_id), "fields": fields, "body": body},
        }


def task_fingerprint(task_id: int) -> dict[str, Any] | None:
    """What a task is, as the guard of undoing its creation compares it. None
    when the row is gone. Read once when the task is made and again when undone."""
    row = (
        TaskMaster.objects.filter(task_id=task_id)
        .values(
            "title",
            "status",
            "priority",
            "effort_level",
            "assignee_id",
            "reporter_id",
            "due_date",
            "start_date",
            "tags",
            "parent_task_id",
            "milestone_id",
            "sprint_id",
            "project_id",
            "is_deleted",
            "content",
            "custom_field_values",
        )
        .first()
    )
    if row is None:
        return None
    fingerprint = {key: jsonable(value) for key, value in row.items()}
    fingerprint["tags"] = tags_key(row["tags"])
    fingerprint["content"] = state_hash(row["content"])
    fingerprint["custom_field_values"] = state_hash(row["custom_field_values"])
    fingerprint["collaborators"] = sorted(
        str(uid)
        for uid in TaskMaster.collaborators.through.objects.filter(taskmaster_id=task_id)
        .values_list("customuser_id", flat=True)
        .order_by("customuser_id")
    )
    return fingerprint


def created_task_capture(task: Any) -> dict[str, Any] | None:
    """The pre-image of `create_task`: nothing to restore, only a stamp of the
    task as made, so that undoing it (deleting it) is refused once it has changed.

    The stamp is the task's own fields, not a mark on its history: a history mark
    would also refuse an undo after something later was itself undone (a comment
    added to it and taken back leaves two rows above any mark), and the writes of
    one conversation are meant to be undone newest first."""
    fingerprint = task_fingerprint(task.task_id)
    if fingerprint is None:
        return None
    return {
        "entity": task_entity(task),
        "image": {"task_id": int(task.task_id), "fingerprint": fingerprint},
    }


def body_restore_capture(task: Any, body: BodyCapture) -> dict[str, Any] | None:
    """The pre-image of `restore_task_body`: the body the restore replaced."""
    how = body.finish()
    if how is None:
        return None
    return {
        "entity": task_entity(task),
        "image": {"task_id": int(task.task_id), "fields": {}, "body": how},
    }


def comment_capture(task: Any, comment: Any) -> dict[str, Any] | None:
    """The pre-image of `add_comment`: the comment as written."""
    body = (
        TaskComments.objects.filter(task=task.task_id, comment_id=comment.comment_id)
        .values_list("comment_body", flat=True)
        .first()
    )
    if body is None:
        return None
    return {
        "entity": {
            "kind": "comment",
            "task_id": int(task.task_id),
            "project_id": task.project_id,
            "display_id": task.display_id,
            "comment_id": int(comment.comment_id),
        },
        "image": {
            "task_id": int(task.task_id),
            "comment_id": int(comment.comment_id),
            "body_hash": state_hash(body),
        },
    }


# Note family -> (table, the NOTE_TYPE_* code the permission and version tables use).
NOTE_FAMILIES = {
    "personal": (PersonalNoteMaster, 1),
    "task": (TaskNoteMaster, 2),
    "chat": (ChatNoteMaster, 3),
}


def note_state_hash(title: Any, body: Any) -> str:
    """A note's title and body as one fingerprint. `or` on both sides, because
    the version history stores a missing title as '' and a missing body as []."""
    return state_hash([title or "", body or []])


def live_note_state(family: str, note_id: int) -> str | None:
    """The fingerprint of the note as it is stored now; None when it is gone."""
    model, _code = NOTE_FAMILIES[family]
    row = model.objects.filter(note_id=note_id).values("title", "body").first()
    return None if row is None else note_state_hash(row["title"], row["body"])


def version_state(version: Any) -> str:
    return note_state_hash(version.title, version.body)


def head_version(family: str, note_id: int) -> Any:
    _model, code = NOTE_FAMILIES[family]
    return (
        NoteVersionMaster.objects.filter(note_type=code, note_id=note_id)
        .order_by("-version_no")
        .first()
    )


def note_access_hash(family: str, note_id: int) -> str:
    """Who holds a role on the note, and which: a note somebody was given access
    to since is not one only its author has seen."""
    _model, code = NOTE_FAMILIES[family]
    pairs = NotePermissionMaster.objects.filter(note_type=code, note_id=note_id).values_list(
        "user_id", "role_id"
    )
    return state_hash(sorted((str(user), role) for user, role in pairs))


def note_entity(family: str, note_id: int, title: Any) -> dict[str, Any]:
    return {"kind": "note", "note_type": family, "note_id": int(note_id), "title": title or ""}


def created_note_capture(family: str, note_id: int, title: Any) -> dict[str, Any] | None:
    """The pre-image of `create_note`: the note as made, and who can reach it."""
    state = live_note_state(family, note_id)
    head = head_version(family, note_id)
    if state is None or head is None:
        return None
    return {
        "entity": note_entity(family, note_id, title),
        "image": {
            "family": family,
            "note_id": int(note_id),
            "state": state,
            "head_version_no": head.version_no,
            "access": note_access_hash(family, note_id),
        },
    }


def note_restore_capture(
    family: str, note_id: int, title: Any, *, before_state: str | None, restored: Any
) -> dict[str, Any] | None:
    """The pre-image of `restore_note_version`. The state it replaced is the version
    just below the row the restore wrote (a restore always appends one, after
    making sure the replaced state is the newest), and that row must hold exactly
    what the note held before: `before_state` is `live_note_state` taken first."""
    if before_state is None:
        return None
    _model, code = NOTE_FAMILIES[family]
    replaced = NoteVersionMaster.objects.filter(
        note_type=code, note_id=note_id, version_no=restored.version_no - 1
    ).first()
    if replaced is None or version_state(replaced) != before_state:
        return None
    return note_edit_capture(family, note_id, title, before=replaced, after=restored)


def note_edit_capture(
    family: str, note_id: int, title: Any, *, before: Any, after: Any
) -> dict[str, Any] | None:
    """The pre-image of a write that replaces a note's title and body
    (`update_note`, `restore_note_version`): the version that holds what it
    replaced, and the one that holds what it left, with the fingerprint of each.

    `before` and `after` are the version rows. Either missing (the best-effort
    snapshot did not happen) means there is nothing to put back from, and a note
    that does not read back as the `after` row says it should is not trusted.
    """
    if before is None or after is None:
        return None
    state = live_note_state(family, note_id)
    if state is None or state != version_state(after):
        return None
    return {
        "entity": note_entity(family, note_id, title),
        "image": {
            "family": family,
            "note_id": int(note_id),
            "pre": {"version_no": before.version_no, "state": version_state(before)},
            "post": {"version_no": after.version_no, "state": state},
        },
    }
