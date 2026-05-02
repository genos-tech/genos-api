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

from typing import Any, Optional

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from origin.middleware.current_user import get_current_user
from origin.models.task.milestone_models import MilestoneAssignees
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskAttachments, TaskComments, TaskMaster


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

    Date / datetime → ISO string. Model instances → primary key.
    Everything else passes through. Centralised so the diff and the
    individual emitters can share the conversion.
    """

    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "pk"):
        return value.pk
    return value


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
        metadata=metadata or {},
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
        _record(
            task=instance,
            action_type=action,
            field_name=field,
            old_value=old,
            new_value=new,
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
