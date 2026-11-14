"""Signal receivers that keep task status in sync with dependencies.

Triggers for `origin.services.task_blocking.sync_blocked_status`:

  1. A `TaskDependency` row is created  → re-sync the blocked task.
  2. A `TaskDependency` row is deleted  → re-sync the blocked task.
  3. A `TaskMaster` save crosses the blocking boundary (its status moves
     between {Open, WIP, Blocked, Pending, …} and {Closed, Deleted}, or
     its `is_deleted` flag flips) → re-sync every task it blocks, and every
     task blocked by something UNDER it (a Closed or Deleted ancestor makes
     what is below read as Closed: the closed-ancestor rule, MIP-1544).

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

import logging

from django.db.models import QuerySet
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from origin.models.common.member_event_models import KIND_DEPENDENCY_ADD, KIND_DEPENDENCY_REMOVE
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services import member_events, task_ancestry
from origin.services.task_blocking import CLOSED_STATUSES, sync_blocked_status

log = logging.getLogger(__name__)


@receiver(post_save, sender=TaskDependency)
def dependency_saved(sender, instance: TaskDependency, created: bool, **kwargs):
    # `created` is almost always True (dependency rows are insert/delete,
    # never edited), but re-syncing on a hypothetical update is harmless
    # and keeps the invariant unconditional.
    sync_blocked_status([instance.blocked_task_id])
    if created:
        member_events.note_dependencies([instance], KIND_DEPENDENCY_ADD)


@receiver(post_delete, sender=TaskDependency)
def dependency_deleted(sender, instance: TaskDependency, **kwargs):
    sync_blocked_status([instance.blocked_task_id])
    # The acting member's ledger notes a delete someone chose to make (MIP-1085), not the
    # rows that go because a task or project did: the cascade's `origin` is that task.
    origin = kwargs.get("origin")
    asked_for = isinstance(origin, TaskDependency) or (
        isinstance(origin, QuerySet) and origin.model is TaskDependency
    )
    if asked_for:
        member_events.note_dependencies([instance], KIND_DEPENDENCY_REMOVE)


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


def _resync_dependents_of_descendants(instance: TaskMaster) -> None:
    """The closed-ancestor rule (MIP-1544): a task under a Closed or Deleted parent, grandparent or
    milestone reads as Closed, so when `instance` crosses the boundary every task under it changes
    what it reads as, though none of their rows is saved. Re-sync whatever those tasks block: closed ->
    the dependents may unblock; reopened -> they re-block. Only when the project has any dependency
    edge at all (a close in a project with none walks nothing)."""
    if instance.project_id is None:
        return
    if not TaskDependency.objects.filter(blocker_task__project_id=instance.project_id).exists():
        return
    below = task_ancestry.descendant_ids(instance.task_id, instance.project_id)
    if not below:
        return
    sync_blocked_status(
        TaskDependency.objects.filter(blocker_task_id__in=below).values_list(
            "blocked_task_id", flat=True
        )
    )


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
    try:
        _resync_dependents_of_descendants(instance)
    except Exception:  # noqa: BLE001 — like `sync_blocked_status`: never break the save that caused it
        log.exception("closed-ancestor re-sync failed for task %s", instance.task_id)
