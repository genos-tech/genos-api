"""Doing an undo: the person's click, through the service layer, with no model.

`POST /api/v2/agent/runs/<run_id>/steps/<step_id>/undo/` lands here. What a write
kept to be undone is `agent/undo.py`; this puts it back. Three rules shape it:

**No tool, no model.** Nothing here calls a tool's `run`, the registry, or an LLM.
A tool is how the AGENT writes, behind an approval; the person's click is the
authorization, and what it does is the same service call the app's own pages
make (`soft_delete_comment`, `delete_note`, `restore_note_version`,
`put_back_task_body`), or a plain model save, so the signals, the history rows,
the sync watermark and the caches behave as for any edit.

**Re-checked, not remembered.** The person must STILL be allowed to do it, by the
rule the write was authorized by (`can_access_task`, `has_write_permission`, the
author-or-owner rule of a note delete): access lost since is `permission_lost`,
never a quiet success. A step is undone only in the window (`expired`), once
(`already_undone`), and only while the thing is as the write left it
(`changed_since`). `update_task` is the one field-by-field: a field somebody else
changed is skipped and reported, the rest put back, and only when NOTHING can be
put back is it `changed_since`.

**Once, atomically, side effects last.** The step is locked (`select_for_update`)
and claimed (`undone_at`) in the transaction that does the work, so two clicks run
it once and a refusal rolls the claim back with everything else; the entity is
locked and its guards re-read under the lock. What cannot be part of the
transaction (the live editor push, whose outcome is reported; the session's tool
cache) runs after the commit.

Stamped as `{"kind": "app", "undo_of": {"run_id", "step"}}` (`via_override`), so
a task's history tells it from the agent's write and from a plain edit.

**Nothing is sent, nothing is recalled.** An undo files no mention, notification,
email or push, and takes none back; the downstream sync that follows any edit
(an outgoing webhook, a calendar auto-sync) follows this one too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from origin.middleware.current_user import via_override
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_delivery_models import TaskDelivery
from origin.models.task.task_models import (
    TaskAttachments,
    TaskComments,
    TaskDependency,
    TaskMaster,
)
from origin.search_engine.agent import tool_cache, undo
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.body_mentions import store_note_mention_baseline
from origin.search_engine.agent.tools.note_access import has_write_permission
from origin.search_engine.models import AgentRun, AgentStep
from origin.services.collab_push import push_note_body_now
from origin.services.note_delete import DeleteRefused, child_note_count, delete_note
from origin.services.note_version_restore import RestoreRefused, restore_note_version
from origin.services.task_body_restore import put_back_task_body
from origin.services.task_comment_manage import soft_delete_comment
from origin.views.utils.note_role import (
    NOTE_TYPE_PERSONAL,
    ROLE_EDITOR,
    ROLE_OWNER,
    get_effective_role,
)
from origin.views.utils.scope_guards import can_access_task, participates_in_team

log = logging.getLogger(__name__)

# The machine codes of a refusal, and the HTTP status each is answered with.
EXPIRED = "expired"
CHANGED_SINCE = "changed_since"
ALREADY_UNDONE = "already_undone"
NOT_UNDOABLE = "not_undoable"
PERMISSION_LOST = "permission_lost"
STATUS = {
    EXPIRED: 410,
    CHANGED_SINCE: 409,
    ALREADY_UNDONE: 409,
    NOT_UNDOABLE: 409,
    PERMISSION_LOST: 403,
}

# What `action` of the answer says happened to the thing.
RESTORED = "restored"  # put back as it was: re-read it
DELETED = "deleted"  # what the agent made is gone: close it, drop it


class UndoNotFound(Exception):
    """No such step of this run. One answer for every way of not finding it, so
    that a guessed id confirms nothing."""


class UndoRefused(Exception):
    """The undo is not done, and why. `code` is for the client, `message` for a person."""

    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = STATUS[code]
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, **self.extra}


@dataclass
class Undoing:
    """What a handler is given: who, for which run and step, and where to put work
    that must wait for the commit."""

    user: Any
    run: AgentRun
    step: AgentStep
    after_commit: list[Callable[[], dict[str, Any]]] = field(default_factory=list)

    @property
    def team_id(self) -> str:
        return str(self.run.team_id)

    @property
    def user_id(self) -> str:
        return str(self.user.id)

    def tool_context(self) -> ToolContext:
        return ToolContext(team_id=self.team_id, user_id=self.user_id)


@dataclass
class Outcome:
    action: str
    entity: dict[str, Any]
    summary: str
    restored: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _changed(what: str, **extra: Any) -> UndoRefused:
    return UndoRefused(
        CHANGED_SINCE,
        f"{what} has changed since Genos made this change, so it was left as it is. "
        "Nothing was undone.",
        **extra,
    )


def _no_access(what: str) -> UndoRefused:
    return UndoRefused(
        PERMISSION_LOST,
        f"You can no longer change {what}, so this can't be undone. Nothing was undone.",
    )


# --------------------------------------------------------------------------- #
# Tasks                                                                       #
# --------------------------------------------------------------------------- #


def _locked_task(undoing: Undoing, task_id: int) -> TaskMaster:
    """The task, locked, with the checks every task undo shares: it is still there,
    still the team's, and the person may still edit it."""
    task = TaskMaster.objects.select_for_update().filter(task_id=task_id).first()
    if task is None:
        raise _changed("The task")
    if str(task.team_id or "") != undoing.team_id or not can_access_task(
        task.task_id, undoing.user_id
    ):
        raise _no_access(f"task {task.display_id}")
    return task


def _column_value(column: str, value: Any) -> Any:
    """A stored JSON value as the model column takes it."""
    if column in ("due_date", "start_date"):
        return date.fromisoformat(value) if value else None
    return value


def undo_create_task(undoing: Undoing, image: dict[str, Any]) -> Outcome:
    """Delete what the agent made, the way the app deletes a task (status
    "Deleted"), while it is still exactly as made, and nobody has built on it."""
    task = _locked_task(undoing, int(image["task_id"]))
    entity = undo.task_entity(task)
    if (
        undo.task_fingerprint(task.task_id) != image["fingerprint"]
        # What other people built on it. Their own rows, not the task's history: a
        # comment added and then taken back leaves rows in the history, and the
        # writes of one conversation are meant to be undone newest first.
        or TaskMaster.objects.filter(parent_task_id=task.task_id).exclude(status="Deleted").exists()
        or TaskComments.objects.filter(task=task.task_id, is_deleted=False).exists()
        or TaskDependency.objects.filter(
            Q(blocker_task=task.task_id) | Q(blocked_task=task.task_id)
        ).exists()
        or TaskDelivery.objects.filter(task=task.task_id).exists()
        or TaskAttachments.objects.filter(task=task.task_id).exists()
    ):
        raise _changed(f"Task {task.display_id}")
    task.status = "Deleted"
    task.save(update_fields=["status", "ts_updated_at"])
    return Outcome(
        action=DELETED,
        entity=entity,
        summary=f"Deleted task {task.display_id}, which Genos had created.",
    )


def undo_task_update(undoing: Undoing, image: dict[str, Any]) -> Outcome:
    """Put back the fields (and the body) the write changed, each only while it is
    still what the write left. `update_task` and `restore_task_body` both."""
    task = _locked_task(undoing, int(image["task_id"]))
    entity = undo.task_entity(task)
    if task.is_deleted or task.is_milestone:
        raise _changed(f"Task {task.display_id}")

    restored: list[str] = []
    skipped: list[dict[str, str]] = []
    update_fields: list[str] = []
    # In the column order the answer promises (a JSON column does not keep the
    # order the pre-image was written in), and only the columns undo knows.
    for column in (c for c in undo.TASK_COLUMNS if c in image["fields"]):
        pair = image["fields"][column]
        now = undo.jsonable(getattr(task, column))
        if undo.column_key(column, now) != undo.column_key(column, pair["after"]):
            skipped.append({"field": column, "reason": CHANGED_SINCE})
            continue
        setattr(task, column, _column_value(column, pair["before"]))
        update_fields.append(column)
        restored.append(column)
    if update_fields:
        task.save(update_fields=[*update_fields, "ts_updated_at"])

    body = image.get("body")
    if body:
        if undo.state_hash(task.content) != body["after_hash"]:
            skipped.append({"field": "body", "reason": CHANGED_SINCE})
        else:
            previous = _kept_body(task, body)
            if previous is _GONE:
                skipped.append({"field": "body", "reason": CHANGED_SINCE})
            else:
                put_back_task_body(
                    task=task, body=previous, actor_id=undoing.user_id, notify_mentions=False
                )
                restored.append("body")

    if not restored:
        # Nothing could be put back: not an undo. (Nothing was written either.)
        raise _changed(f"Task {task.display_id}", skipped=skipped)
    return Outcome(
        action=RESTORED,
        entity=entity,
        restored=restored,
        skipped=skipped,
        summary=f"Put back {', '.join(restored)} of task {task.display_id}.",
    )


_GONE = object()


def _kept_body(task: TaskMaster, body: dict[str, Any]) -> Any:
    """The body to put back: what the history row kept, or none at all, or `_GONE`
    when the row is no longer there to keep it (history retention)."""
    if body["mode"] == "none":
        return None
    row = TaskActivity.objects.filter(
        activity_id=body["activity_id"],
        task_id=task.task_id,
        action_type=TaskActivityActionType.DESCRIPTION,
    ).first()
    if row is None or not (row.metadata or {}).get("previousBodyKept"):
        return _GONE
    return row.old_value if isinstance(row.old_value, list) else _GONE


def undo_add_comment(undoing: Undoing, image: dict[str, Any]) -> Outcome:
    """Delete the comment (soft, as the app does), while nobody has edited it."""
    task = _locked_task(undoing, int(image["task_id"]))
    comment = (
        TaskComments.objects.select_for_update()
        .filter(task=task.task_id, comment_id=int(image["comment_id"]))
        .first()
    )
    entity = {
        "kind": "comment",
        "task_id": int(task.task_id),
        "project_id": task.project_id,
        "display_id": task.display_id,
        "comment_id": int(image["comment_id"]),
    }
    if comment is None or comment.is_deleted:
        raise _changed("The comment")
    if str(comment.sender_id) != undoing.user_id:
        raise _no_access("this comment")
    if undo.state_hash(comment.comment_body) != image["body_hash"]:
        raise _changed("The comment")
    count = soft_delete_comment(comment)
    return Outcome(
        action=DELETED,
        entity=entity,
        extra={"comment_count": count},
        summary=f"Deleted the comment Genos added to {task.display_id}.",
    )


# --------------------------------------------------------------------------- #
# Notes                                                                       #
# --------------------------------------------------------------------------- #


def _locked_note(undoing: Undoing, family: str, note_id: int):
    model, code = undo.NOTE_FAMILIES[family]
    note = model.objects.select_for_update().filter(note_id=note_id).first()
    if note is None:
        raise _changed("The note")
    if str(note.team_id or "") != undoing.team_id:
        raise _no_access("this note")
    return note, code


def _may_delete_note(user_id: str, code: int, note: Any) -> bool:
    """Who may delete a note, as the agent's `delete_note` decides it (stricter than
    the page, on purpose): the Owner, or the author unless a grant has since made
    them a mere reader of their own note.

    The author with NO role counts only for a PERSONAL note (a private note has no implicit
    access: its author is its owner, as in `has_write_permission`). A chat note's author who left
    the chat (MIP-1492) or a task note's who left the project (MIP-1511) holds no role on the
    note on any other gate and must not delete it here either (MIP-1551)."""
    role = get_effective_role(user_id, code, note.note_id)
    if role == ROLE_OWNER:
        return True
    wrote_it = str(getattr(note, "owner_id", "") or "") == user_id
    if role is None:
        return wrote_it and code == NOTE_TYPE_PERSONAL
    return wrote_it and role <= ROLE_EDITOR


def undo_create_note(undoing: Undoing, image: dict[str, Any]) -> Outcome:
    """Delete the note the agent made, for good (nothing else has it: it is its own
    note and its version history), while it is still as made, with the people it
    was made for, and no sub-notes."""
    family = image["family"]
    note_id = int(image["note_id"])
    note, code = _locked_note(undoing, family, note_id)
    title = note.title or "Untitled"
    entity = undo.note_entity(family, note_id, title)
    if not _may_delete_note(undoing.user_id, code, note):
        raise _no_access(f'the note "{title}"')
    head = undo.head_version(family, note_id)
    if (
        undo.live_note_state(family, note_id) != image["state"]
        or head is None
        or head.version_no != int(image["head_version_no"])
        or undo.version_state(head) != image["state"]
        or undo.note_access_hash(family, note_id) != image["access"]
        or child_note_count(team_id=undoing.team_id, family=family, note_id=note_id)
    ):
        raise _changed(f'The note "{title}"')
    try:
        delete_note(team_id=undoing.team_id, family=family, note_id=note_id)
    except DeleteRefused as refused:
        raise _changed(f'The note "{title}"', detail=refused.message)
    return Outcome(
        action=DELETED,
        entity=entity,
        summary=f'Deleted the note "{title}", which Genos had created.',
    )


def undo_note_edit(undoing: Undoing, image: dict[str, Any]) -> Outcome:
    """Put back the version a write replaced, while the note is still exactly what the
    write left and the version to put back is still the one the write replaced.
    `update_note` and `restore_note_version` both.

    "Exactly what the write left" is the note's title and body AND its newest
    version's, by content and not by version number: an editor's own save within
    five minutes rewrites the newest version IN PLACE (the number does not move
    while the text does), and an undo of a later write adds a version holding
    the very text an earlier write left, so that earlier write must stay
    undoable, newest first."""
    family = image["family"]
    note_id = int(image["note_id"])
    note, code = _locked_note(undoing, family, note_id)
    title = note.title or "Untitled"
    if not has_write_permission(
        owner_id=getattr(note, "owner_id", None),
        note_id=note_id,
        note_type_code=code,
        ctx=undoing.tool_context(),
    ):
        raise _no_access(f'the note "{title}"')
    pre, post = image["pre"], image["post"]
    head = undo.head_version(family, note_id)
    replaced = NoteVersionMaster.objects.filter(
        note_type=code, note_id=note_id, version_no=int(pre["version_no"])
    ).first()
    if (
        undo.live_note_state(family, note_id) != post["state"]
        or head is None
        or undo.version_state(head) != post["state"]
        or replaced is None
        or undo.version_state(replaced) != pre["state"]
    ):
        raise _changed(f'The note "{title}"')
    try:
        _new_version, source = restore_note_version(
            team=TeamMaster.objects.get(team_id=undoing.team_id),
            user=CustomUser.objects.get(id=undoing.user_id),
            family=family,
            note_id=note_id,
            version_no=int(pre["version_no"]),
        )
    except RestoreRefused as refused:
        raise _changed(f'The note "{title}"', detail=refused.message)
    note.refresh_from_db()
    store_note_mention_baseline(note)

    body = source.body or []

    def push_to_the_live_editor() -> dict[str, Any]:
        return {"live_editor": push_note_body_now(family, note_id, body).value}

    undoing.after_commit.append(push_to_the_live_editor)
    return Outcome(
        action=RESTORED,
        entity=undo.note_entity(family, note_id, source.title or title),
        restored=["title", "body"],
        summary=f'Put back the earlier text of "{source.title or title}".',
    )


# --------------------------------------------------------------------------- #
# The registry                                                                #
# --------------------------------------------------------------------------- #

# tool name -> how its undo is done. Every tool the map (`undo.UNDO_SUPPORT`) says
# is not `none` has one, and only those do: `test_agent_undo_map` holds that.
HANDLERS: dict[str, Callable[[Undoing, dict[str, Any]], Outcome]] = {
    "create_task": undo_create_task,
    "update_task": undo_task_update,
    "restore_task_body": undo_task_update,
    "add_comment": undo_add_comment,
    "create_note": undo_create_note,
    "update_note": undo_note_edit,
    "restore_note_version": undo_note_edit,
}


# --------------------------------------------------------------------------- #
# The undo                                                                    #
# --------------------------------------------------------------------------- #


def perform_undo(*, run: AgentRun, step_id: int, user: Any) -> dict[str, Any]:
    """Undo one approved write of `run`, for `user`, who must be the run's own and
    is checked by the caller. Returns the answer, or raises `UndoNotFound` /
    `UndoRefused`."""
    undoing: Undoing
    with transaction.atomic():
        # Locked: a second click waits here, and then finds the first one's claim.
        step = (
            AgentStep.objects.select_for_update().filter(step_id=step_id, run_id=run.run_id).first()
        )
        if step is None:
            raise UndoNotFound()
        undoing = Undoing(user=user, run=run, step=step)
        stored = step.undo_json
        handler = HANDLERS.get(step.tool_name)
        if (
            not isinstance(stored, dict)
            or stored.get("v") != undo.SCHEMA_VERSION
            or stored.get("tool") != step.tool_name
            or handler is None
        ):
            raise UndoRefused(NOT_UNDOABLE, "This change can't be undone.")
        if step.undone_at is not None:
            raise UndoRefused(ALREADY_UNDONE, "This change was already undone.")
        now = timezone.now()
        if undo.is_expired(stored, now=now):
            raise UndoRefused(
                EXPIRED,
                "This change can no longer be undone: Undo is available for "
                f"{undo.UNDO_WINDOW.days} days after it was made.",
            )
        if not participates_in_team(undoing.team_id, undoing.user_id):
            raise UndoRefused(
                PERMISSION_LOST,
                "You are no longer part of this team, so this can't be undone. Nothing was undone.",
            )
        # Claimed first, so that the work below is the last thing in the transaction,
        # and a refusal or a failure rolls the claim back with it.
        step.undone_at = now
        step.save(update_fields=["undone_at"])
        try:
            with via_override(
                {"kind": "app", "undo_of": {"run_id": str(run.run_id), "step": step.step_index}}
            ):
                outcome = handler(undoing, stored["image"])
        except (KeyError, TypeError, ValueError, AttributeError):
            # What was stored does not read as this handler expects (a shape this
            # code does not know). It is a bug to fix, not a state of the
            # workspace: say it cannot be undone, and keep the trace.
            log.exception("The undo stored on step %s is malformed", step.step_id)
            raise UndoRefused(NOT_UNDOABLE, "This change can't be undone.")
        # What it did, kept with the claim (same transaction): the model can be told it later.
        step.undo_outcome_json = {
            "action": outcome.action,
            "restored": list(outcome.restored),
            "skipped": [dict(s) for s in outcome.skipped],
        }
        step.save(update_fields=["undo_outcome_json"])

    # After the commit: what cannot be part of the transaction.
    extra = dict(outcome.extra)
    for effect in undoing.after_commit:
        try:
            extra.update(effect())
        except Exception:  # noqa: BLE001 — the undo is done; only its side effect failed
            log.exception("An effect of undoing step %s failed", step.step_id)
    if run.session_id:
        tool_cache.invalidate_session(str(run.session_id))
    return {
        "run_id": str(run.run_id),
        "step_id": step.step_id,
        "tool_name": step.tool_name,
        "undone_at": now.isoformat(),
        "action": outcome.action,
        "restored": outcome.restored,
        "skipped": outcome.skipped,
        "entity": outcome.entity,
        "summary": outcome.summary,
        **extra,
    }
