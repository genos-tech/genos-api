"""Task / milestone audit-log signal handlers.

Fan out an immutable `TaskActivity` row whenever a watched field on
`TaskMaster` changes, a comment is created/edited/deleted, an
attachment is added/removed, or a milestone-assignee relation flips.

The signal layer reads the actor from the thread-local stashed by
`origin.middleware.current_user.CurrentUserMiddleware`. SocketIO
handlers (Flask) that bypass Django middleware and still want
attribution must call `set_current_user(user)` before the ORM write
and `clear_current_user()` afterwards (see the comment / attachment
handlers in `backend/socketio_events/`).

Adding a new field to track:
1. Add a `TaskActivityActionType` enum value.
2. Append the field name to `_TRACKED_TASK_FIELDS` (and add a
   normaliser to `_NORMALISERS` if it needs a value transform).
3. Map it in `_FIELD_TO_ACTION` so the diff can pick the right enum.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from django.db import transaction
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from origin.middleware.current_user import get_current_user
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskAttachments, TaskComments, TaskMaster
from origin.services.calendar_sync import (
    LINK_ONLY_FIELDS,
    delete_task_event,
    get_google_connected_account,
    sync_task_event,
)
from origin.services.task_cache import invalidate_project_tasks_cache

_calendar_logger = logging.getLogger("origin.calendar_sync")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fields on `TaskMaster` whose changes should generate audit rows.
# `is_deleted` is handled separately so we can emit DELETED / REOPENED
# (vs. STATUS) when the soft-delete flag flips.
_TRACKED_TASK_FIELDS = (
    "title",
    "status",
    "priority",
    "effort_level",
    "assignee_id",
    "reporter_id",
    "due_date",
    "parent_task_id",
    "milestone_id",
    "sprint_id",
)


_FIELD_TO_ACTION = {
    "title": TaskActivityActionType.TITLE,
    "status": TaskActivityActionType.STATUS,
    "priority": TaskActivityActionType.PRIORITY,
    "effort_level": TaskActivityActionType.EFFORT,
    "assignee_id": TaskActivityActionType.ASSIGNEE,
    "reporter_id": TaskActivityActionType.REPORTER,
    "due_date": TaskActivityActionType.DUE_DATE,
    "parent_task_id": TaskActivityActionType.PARENT,
    "milestone_id": TaskActivityActionType.MILESTONE_LINK,
    "sprint_id": TaskActivityActionType.SPRINT_LINK,
}


def _to_jsonable(value: Any) -> Any:
    """Coerce `value` to something JSONField will accept.

    UUID → str. Date / datetime → ISO string. Dicts / lists / tuples
    are walked recursively so values nested inside `metadata` (e.g.
    a `senderId` that's actually a `uuid.UUID`) get the same
    treatment. Model instances unwrap to their primary key, which is
    re-fed through the helper so a UUID-typed pk is also stringified.

    Centralised so the diff and the individual emitters can share
    the conversion — and so adding a new "this type isn't JSON
    serializable" hotfix only needs to touch one place.
    """

    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _to_jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "pk"):
        return _to_jsonable(value.pk)
    return value


def _resolve_relation_label(field: str, value: Any) -> Optional[str]:
    """Return a human-readable label for a relation-id value, or None.

    Backs the Activity feed: a row recording a `parent_task_id` /
    `milestone_id` / `sprint_id` flip stores the raw id in
    `old_value` / `new_value` (so the diff is faithful) and the
    corresponding title / name in `metadata.oldLabel` / `newLabel`
    so the frontend can render `Title (#id)` instead of a bare id —
    the latter is meaningless to humans skimming the feed.
    """
    if value in (None, ""):
        return None
    try:
        if field == "parent_task_id":
            row = TaskMaster.objects.only("title").filter(pk=value).first()
            return row.title if row else None
        if field == "milestone_id":
            row = MilestoneMaster.objects.only("title").filter(pk=value).first()
            return row.title if row else None
        if field == "sprint_id":
            row = Sprint.objects.only("name").filter(pk=value).first()
            return row.name if row else None
    except (ValueError, TypeError):
        # `value` was not a valid pk shape — fall through to None so
        # the audit row still gets created with just the id.
        return None
    return None


def _resolve_task_display_id(value: Any) -> Optional[str]:
    """Return a parent task's human-readable display id ("PRF-123"), or
    None. Snapshotted into a `parent_task_id`-change row's metadata so
    the feed can show the ticket-style id instead of a raw "#<pk>" that
    means nothing to a human skimming the activity log. `select_related`
    keeps the `display_id` property's `project.code` lookup to one query.
    """
    if value in (None, ""):
        return None
    try:
        row = TaskMaster.objects.select_related("project").filter(pk=value).first()
    except (ValueError, TypeError):
        return None
    return row.display_id if row else None


def _metadata_for_change(field: str, old: Any, new: Any) -> Optional[dict]:
    """Build the optional metadata payload for a tracked-field diff.

    Relation-id fields participate so the feed can show
    `Old title (#oldId) → New title (#newId)`. Parent-task changes
    additionally carry the related task's display id ("PRF-123") so the
    feed renders the ticket-style id instead of the raw pk — milestones
    and sprints have no such id and keep the bare "#id". Returns None
    for fields that don't need extra context.
    """
    if field == "parent_task_id":
        return {
            "oldLabel": _resolve_relation_label(field, old),
            "newLabel": _resolve_relation_label(field, new),
            "oldDisplayId": _resolve_task_display_id(old),
            "newDisplayId": _resolve_task_display_id(new),
        }
    if field in ("milestone_id", "sprint_id"):
        return {
            "oldLabel": _resolve_relation_label(field, old),
            "newLabel": _resolve_relation_label(field, new),
        }
    return None


def _resolve_actor() -> Optional[Any]:
    """Return the user FK suitable for `TaskActivity.actor` or None.

    Filters out `AnonymousUser` defensively; the FK target only
    accepts `CustomUser` rows.
    """

    user = get_current_user()
    if user is None:
        return None
    if not getattr(user, "is_authenticated", False):
        return None
    return user


def _record(
    *,
    task: TaskMaster,
    action_type: TaskActivityActionType,
    field_name: Optional[str] = None,
    old_value: Any = None,
    new_value: Any = None,
    metadata: Optional[dict] = None,
) -> None:
    """Single point for inserting an activity row.

    Tolerates missing tasks (None pk) so signals during fixture loads
    or partial saves don't blow up.
    """

    if task is None or task.pk is None:
        return
    TaskActivity.objects.create(
        team=task.team,
        project=task.project,
        task=task,
        actor=_resolve_actor(),
        action_type=action_type,
        field_name=field_name,
        old_value=_to_jsonable(old_value),
        new_value=_to_jsonable(new_value),
        metadata=_to_jsonable(metadata or {}),
    )


# ---------------------------------------------------------------------------
# TaskMaster — capture pre-save snapshot, diff on post_save
# ---------------------------------------------------------------------------


@receiver(pre_save, sender=TaskMaster)
def task_capture_original(sender, instance: TaskMaster, **kwargs):
    """Stash the row's previous values on the instance so post_save can
    diff. Skip when there's no PK yet (fresh insert) — `created` covers
    that path in post_save."""

    if not instance.pk:
        instance._activity_original = None
        return
    try:
        previous = TaskMaster.objects.get(pk=instance.pk)
    except TaskMaster.DoesNotExist:
        instance._activity_original = None
        return
    snapshot = {field: getattr(previous, field) for field in _TRACKED_TASK_FIELDS}
    snapshot["is_deleted"] = previous.is_deleted
    snapshot["content"] = previous.content
    # Captured so post_save can recognise the init→real "finalize" save
    # (the moment CreateTaskForm's draft becomes a real task) and treat
    # it as a single creation event instead of a diff of every field.
    snapshot["is_init_task"] = previous.is_init_task
    instance._activity_original = snapshot


@receiver(post_save, sender=TaskMaster)
def task_record_changes(sender, instance: TaskMaster, created: bool, **kwargs):
    """Emit one CREATED row on insert, otherwise one row per changed
    watched field. `is_deleted` flipping records DELETED / REOPENED
    rather than a generic field diff so the feed reads naturally."""

    # Skip the placeholder rows that empty CreateTaskForm uses; the
    # real audit trail starts when the user actually saves the task.
    if instance.is_init_task:
        return

    if created:
        _record(
            task=instance,
            action_type=TaskActivityActionType.CREATED,
            new_value={
                "title": instance.title,
                "status": instance.status,
                "is_milestone": instance.is_milestone,
            },
        )
        return

    original = getattr(instance, "_activity_original", None)
    if original is None:
        return

    # Finalize transition: the draft row CreateTaskForm has been editing
    # (is_init_task=True) just became a real task. The submit PUT writes
    # every field at once, which would otherwise spam the feed with a
    # "changed X from None" row per field — none of which the user did
    # *after* creating the task. Treat the whole transition as a single
    # creation: emit one CREATED (the init insert was skipped by the
    # guard above, so the task has no CREATED row yet) and stop.
    #
    # The submit PUT also runs follow-up saves on this same instance
    # (the due-date clear, and `_bridge_milestone_to_parent` for
    # milestone/sub-task creation) AFTER is_init_task is already False,
    # so the snapshot check alone can't catch them. Flag the instance so
    # those trailing saves within this request are suppressed too.
    if original.get("is_init_task"):
        _record(
            task=instance,
            action_type=TaskActivityActionType.CREATED,
            new_value={
                "title": instance.title,
                "status": instance.status,
                "is_milestone": instance.is_milestone,
            },
        )
        instance._activity_finalized_creation = True
        return
    if getattr(instance, "_activity_finalized_creation", False):
        return

    # Soft-delete / restore takes precedence over a plain status diff.
    if original.get("is_deleted") != instance.is_deleted:
        _record(
            task=instance,
            action_type=(
                TaskActivityActionType.DELETED
                if instance.is_deleted
                else TaskActivityActionType.REOPENED
            ),
            field_name="is_deleted",
            old_value=original.get("is_deleted"),
            new_value=instance.is_deleted,
        )

    # Description edits are tracked as a single "edited" event without
    # a body diff — comparing JSONField BlockNote documents is noisy
    # and the body is preserved on the task itself anyway.
    if original.get("content") != instance.content:
        _record(
            task=instance,
            action_type=TaskActivityActionType.DESCRIPTION,
            field_name="content",
        )

    for field in _TRACKED_TASK_FIELDS:
        old = original.get(field)
        new = getattr(instance, field)
        if old == new:
            continue
        action = _FIELD_TO_ACTION.get(field)
        if action is None:
            continue
        metadata = _metadata_for_change(field, old, new)
        # PR-merge auto-close: the GitHub webhook stashes `_pr_merge_close`
        # on the instance before flipping status to Closed. Tag the status
        # row so the feed attributes it to the merged PR rather than the
        # null actor the unauthenticated webhook produces.
        if field == "status":
            pr_close = getattr(instance, "_pr_merge_close", None)
            if pr_close is not None:
                metadata = {
                    **(metadata or {}),
                    "closedByPrMerge": True,
                    "prUrl": pr_close.get("prUrl"),
                }
        _record(
            task=instance,
            action_type=action,
            field_name=field,
            old_value=old,
            new_value=new,
            metadata=metadata,
        )


# ---------------------------------------------------------------------------
# TaskComments
# ---------------------------------------------------------------------------


@receiver(post_save, sender=TaskComments)
def comment_record(sender, instance: TaskComments, created: bool, **kwargs):
    """Comment add / edit / soft-delete. Soft-deletes flip `is_deleted`
    on an existing row, which we surface as COMMENT_DELETED so the
    feed mirrors the user's intent."""

    task = instance.task
    if task is None:
        return
    if created:
        action = TaskActivityActionType.COMMENT_ADDED
    else:
        action = (
            TaskActivityActionType.COMMENT_DELETED
            if instance.is_deleted
            else TaskActivityActionType.COMMENT_EDITED
        )
    _record(
        task=task,
        action_type=action,
        field_name="comment",
        metadata={
            "commentId": instance.comment_id,
            "senderId": getattr(instance.sender, "id", None),
        },
    )


@receiver(post_delete, sender=TaskComments)
def comment_record_delete(sender, instance: TaskComments, **kwargs):
    task = instance.task
    if task is None:
        return
    _record(
        task=task,
        action_type=TaskActivityActionType.COMMENT_DELETED,
        field_name="comment",
        metadata={
            "commentId": instance.comment_id,
            "senderId": getattr(instance.sender, "id", None),
        },
    )


# ---------------------------------------------------------------------------
# TaskAttachments
# ---------------------------------------------------------------------------


@receiver(post_save, sender=TaskAttachments)
def attachment_record_add(sender, instance: TaskAttachments, created: bool, **kwargs):
    if not created:
        return
    task = instance.task
    if task is None:
        return
    _record(
        task=task,
        action_type=TaskActivityActionType.ATTACHMENT_ADDED,
        field_name="attachment",
        new_value=instance.original_filename or str(instance.attached_file),
        metadata={"attachmentId": instance.attachment_id},
    )


@receiver(post_delete, sender=TaskAttachments)
def attachment_record_remove(sender, instance: TaskAttachments, **kwargs):
    task = instance.task
    if task is None:
        return
    _record(
        task=task,
        action_type=TaskActivityActionType.ATTACHMENT_REMOVED,
        field_name="attachment",
        old_value=instance.original_filename or str(instance.attached_file),
        metadata={"attachmentId": instance.attachment_id},
    )


# ---------------------------------------------------------------------------
# MilestoneAssignees — record against the milestone's backing task so
# the milestone preview's Activity tab picks them up alongside everything
# else that targets the backing TaskMaster row.
# ---------------------------------------------------------------------------


def _milestone_backing_task(milestone) -> Optional[TaskMaster]:
    if milestone is None:
        return None
    if getattr(milestone, "task_id", None) is None:
        return None
    try:
        return TaskMaster.objects.get(pk=milestone.task_id)
    except TaskMaster.DoesNotExist:
        return None


@receiver(post_save, sender=MilestoneAssignees)
def milestone_assignee_record_add(sender, instance: MilestoneAssignees, created: bool, **kwargs):
    if not created:
        return
    task = _milestone_backing_task(instance.milestone)
    if task is None:
        return
    _record(
        task=task,
        action_type=TaskActivityActionType.MILESTONE_ASSIGNEE_ADDED,
        field_name="milestone_assignee",
        new_value=getattr(instance.user, "id", None),
        metadata={
            "milestoneId": instance.milestone_id,
            "userName": getattr(instance.user, "username", None),
        },
    )


@receiver(post_delete, sender=MilestoneAssignees)
def milestone_assignee_record_remove(sender, instance: MilestoneAssignees, **kwargs):
    task = _milestone_backing_task(instance.milestone)
    if task is None:
        return
    _record(
        task=task,
        action_type=TaskActivityActionType.MILESTONE_ASSIGNEE_REMOVED,
        field_name="milestone_assignee",
        old_value=getattr(instance.user, "id", None),
        metadata={
            "milestoneId": instance.milestone_id,
            "userName": getattr(instance.user, "username", None),
        },
    )


# ---------------------------------------------------------------------------
# Task → Google Calendar auto-sync (opt-in per user)
# ---------------------------------------------------------------------------
#
# One-way sync: changes flow App → Google only. Deletions or edits on
# Google never propagate back. Per the v1 product decision, if a user
# deletes the linked event on Google we clear the link on the next 404
# and never re-create.
#
# Calendar API calls are deferred via `transaction.on_commit` so the
# request transaction commits before any Google round-trip. This keeps
# task-save latency tied to the DB write only, and naturally drops the
# sync if the transaction rolls back.


def _sync_save(task: TaskMaster) -> None:
    """Persist any link-column mutations made by the sync helpers,
    using `update_fields=LINK_ONLY_FIELDS` so the post_save recursion
    guard short-circuits the re-fire."""
    task.save(update_fields=list(LINK_ONLY_FIELDS))


def _run_upsert(task_pk: int) -> None:
    """on_commit callback — re-fetch the task fresh (the instance in
    the signal closure may be stale by the time the txn commits), do
    the upsert, save link columns if changed."""
    try:
        task = TaskMaster.objects.filter(pk=task_pk).first()
        if task is None or task.due_date is None or task.is_deleted:
            return
        if task.assignee_id is None:
            return
        user = task.assignee
        if not getattr(user, "auto_sync_tasks_to_calendar", False):
            return
        account = get_google_connected_account(user)
        if account is None:
            return
        # Save the model row only when link columns actually changed
        # (a new event was created, or Google returned 404 and we
        # cleared the stale link). A clean "patched" outcome means
        # the columns are already accurate; no save needed.
        if sync_task_event(account, task) in ("created", "cleared"):
            _sync_save(task)
    except Exception as exc:  # pragma: no cover - defensive
        _calendar_logger.warning("auto-sync upsert failed task=%s err=%s", task_pk, exc)


def _run_delete(task_pk: int) -> None:
    """on_commit callback for deletion paths (soft-delete or due_date
    cleared while a link exists). Always tries to clear the link
    columns even if the upstream DELETE fails."""
    try:
        task = TaskMaster.objects.filter(pk=task_pk).first()
        if task is None or not task.linked_calendar_event_id:
            return
        if task.assignee_id is None:
            return
        account = get_google_connected_account(task.assignee)
        if account is None:
            # No account → just clear our pointer; nothing to delete
            # upstream we could touch.
            task.linked_calendar_event_id = None
            task.linked_calendar_id = None
            _sync_save(task)
            return
        if delete_task_event(account, task):
            _sync_save(task)
    except Exception as exc:  # pragma: no cover - defensive
        _calendar_logger.warning("auto-sync delete failed task=%s err=%s", task_pk, exc)


@receiver(post_save, sender=TaskMaster)
def task_auto_sync_to_calendar(sender, instance: TaskMaster, created: bool, **kwargs):
    """Opt-in auto-sync of task changes to the assignee's Google
    Calendar. Toggled per-user via `auto_sync_tasks_to_calendar`.

    Decisions:
      - Recursion guard: when the sync helpers save link columns with
        `update_fields=LINK_ONLY_FIELDS`, the re-fired post_save
        early-returns here so we don't loop.
      - Defer the API call to `on_commit` so a rolled-back request
        doesn't trigger a phantom Google write, and request latency
        isn't gated on the Google round-trip.
      - Init-task placeholders never sync — they're empty rows the
        Create Task form uses pre-save.
      - Past-due tasks DO sync on edit (only the backfill endpoint
        filters out past dates).
    """
    update_fields = kwargs.get("update_fields")
    if update_fields and set(update_fields).issubset(set(LINK_ONLY_FIELDS)):
        return
    if instance.is_init_task:
        return
    if instance.assignee_id is None:
        return

    # Soft-delete: drop the upstream event if one exists.
    if instance.is_deleted:
        if instance.linked_calendar_event_id:
            transaction.on_commit(lambda pk=instance.pk: _run_delete(pk))
        return

    # No due_date: if a link exists, clear it; otherwise nothing to do.
    if instance.due_date is None:
        if instance.linked_calendar_event_id:
            transaction.on_commit(lambda pk=instance.pk: _run_delete(pk))
        return

    # Assignee opt-in check is repeated in `_run_upsert` since the user
    # could toggle off between now and the on_commit callback firing.
    # The short-circuit here is just a cheap defense against queueing
    # an unnecessary callback.
    if not getattr(instance.assignee, "auto_sync_tasks_to_calendar", False):
        return

    transaction.on_commit(lambda pk=instance.pk: _run_upsert(pk))


# ---------------------------------------------------------------------------
# Project-tasks response cache invalidation
# ---------------------------------------------------------------------------
#
# Catches every `TaskMaster.save()` / `.delete()` regardless of caller —
# view, agent tool, webhook, or signal-driven sync (e.g. PR-merge
# auto-close, calendar reconciliation). Queryset-level
# `.update(...)` / `.delete()` bypass post_save, so the milestone +
# sprint views that bulk-update task rows still invalidate explicitly.


@receiver(post_save, sender=TaskMaster)
def task_invalidate_project_cache_on_save(sender, instance: TaskMaster, **kwargs):
    invalidate_project_tasks_cache(instance.team_id, instance.project_id)


@receiver(post_delete, sender=TaskMaster)
def task_invalidate_project_cache_on_delete(sender, instance: TaskMaster, **kwargs):
    invalidate_project_tasks_cache(instance.team_id, instance.project_id)
