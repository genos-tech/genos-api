"""Signal receivers that keep task status in sync with dependencies.

Triggers for `origin.services.task_blocking.sync_blocked_status`:

  1. A `TaskDependency` row is created  → re-sync the blocked task.
  2. A `TaskDependency` row is deleted  → re-sync the blocked task.
  3. A `TaskMaster` save crosses the blocking boundary (its status moves
     between {Open, WIP, Blocked, Pending, …} and {Closed, Deleted}, or
     its `is_deleted` flag flips) → re-sync every task it blocks.

The boundary check in (3) is what makes the recursion terminate: the
sync itself only ever writes "Blocked" ↔ "Open", which are both on the
blocking side of the boundary, so a synced task never re-triggers a
sync of ITS dependents.

Dependency edges written via `bulk_create` (e.g. the agent's
create_task_plan tool) don't fire post_save — those call sites invoke
`sync_blocked_status` explicitly.

Uses its own tiny pre_save snapshot (`_blocking_prev`) instead of
piggybacking on task_signals' `_activity_original` so this module stays
self-contained and receiver ordering can't matter.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services.task_blocking import CLOSED_STATUSES, sync_blocked_status


@receiver(post_save, sender=TaskDependency)
def dependency_saved(sender, instance: TaskDependency, created: bool, **kwargs):
    # `created` is almost always True (dependency rows are insert/delete,
    # never edited), but re-syncing on a hypothetical update is harmless
    # and keeps the invariant unconditional.
    sync_blocked_status([instance.blocked_task_id])


@receiver(post_delete, sender=TaskDependency)
def dependency_deleted(sender, instance: TaskDependency, **kwargs):
    sync_blocked_status([instance.blocked_task_id])


def _is_blocking(status: str | None, is_deleted: bool) -> bool:
    """Whether a blocker in this state actually blocks its dependents."""
    return not is_deleted and (status or "") not in CLOSED_STATUSES


@receiver(pre_save, sender=TaskMaster)
def blocking_capture_previous(sender, instance: TaskMaster, **kwargs):
    """Stash the previous (status, is_deleted) so post_save can detect a
    blocking-boundary crossing without another full-row SELECT."""
    if not instance.pk:
        instance._blocking_prev = None
        return
    prev = (
        TaskMaster.objects.filter(pk=instance.pk).values("status", "is_deleted").first()
    )
    instance._blocking_prev = prev


@receiver(post_save, sender=TaskMaster)
def blocker_state_changed(sender, instance: TaskMaster, created: bool, **kwargs):
    """When a task crosses the blocking boundary, re-sync its dependents.

    Examples: blocker closed → dependents may auto-unblock; a closed
    blocker reopened (or un-soft-deleted) → dependents re-block.
    A fresh insert can't already block anyone (dependency rows reference
    existing tasks), so `created` is skipped.
    """
    if created:
        return
    prev = getattr(instance, "_blocking_prev", None)
    if prev is None:
        return
    was_blocking = _is_blocking(prev["status"], prev["is_deleted"])
    now_blocking = _is_blocking(instance.status, instance.is_deleted)
    if was_blocking == now_blocking:
        return
    dependent_ids = TaskDependency.objects.filter(blocker_task_id=instance.task_id).values_list(
        "blocked_task_id", flat=True
    )
    sync_blocked_status(dependent_ids)
