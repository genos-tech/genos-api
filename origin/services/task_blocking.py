"""Dependency-driven auto-"Blocked" status for tasks.

Rules (product decision, 2026-07-13):

  * A task with at least one OPEN blocker (a `TaskDependency` whose
    blocker task is not Closed/Deleted and not soft-deleted) is
    automatically moved to status "Blocked".
  * When the LAST open blocker clears (blocker closed / deleted /
    dependency removed), a task whose status is exactly "Blocked"
    automatically returns to "Open".

"Blocked" stays a manually settable status too — a task can be blocked
by non-task reasons (staffing, external vendor, …). The automation is
therefore strictly EVENT-DRIVEN: it runs only when a dependency edge or
a blocker's status changes, so a manually-Blocked task with no task
blockers is never touched. The known ambiguity: a task blocked both by
a task AND a non-task reason will auto-revert to Open when the task
blocker clears — the system can't see the second reason.

Auto-transitions only apply to plain tasks in active states:

  * `is_milestone` backing rows are skipped (milestones keep their own
    Open/WIP/Pending/Closed vocabulary).
  * Soft-deleted and init-draft rows are skipped.
  * Auto-Blocked fires only from Open/WIP/Pending — it never reopens a
    Closed/Deleted task.
  * Auto-Open fires only from exactly "Blocked" — it never stomps a
    manual WIP/Pending override.

Writes go through `model.save(update_fields=...)` so the existing
task_signals machinery emits TaskActivity STATUS rows and the search
index / caches stay in sync. Recursion terminates structurally: the
only statuses this module writes are "Blocked" and "Open", both of
which are blocking-capable (non-closed), so a synced task never changes
its own dependents' blocked-ness.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

log = logging.getLogger(__name__)

# A blocker in any of these states no longer blocks. Kept aligned with
# the agent tools' closed set and the FE's isCurrentlyBlocked (which
# checks `status !== "Closed"`; we additionally treat the "Deleted"
# status as non-blocking — a tombstoned task must not hold work hostage).
CLOSED_STATUSES = ("Closed", "Deleted")

# States from which a task may be auto-moved to "Blocked". Deliberately
# excludes Closed/Deleted (adding a dependency to a finished task must
# not reopen it) and "Blocked" itself (no-op).
AUTO_BLOCKABLE_STATUSES = ("Open", "WIP", "Pending")


def _has_open_blocker(task_id: int) -> bool:
    from origin.models.task.task_models import TaskDependency  # noqa: PLC0415

    return (
        TaskDependency.objects.filter(blocked_task_id=task_id)
        .exclude(blocker_task__status__in=CLOSED_STATUSES)
        .exclude(blocker_task__is_deleted=True)
        .exists()
    )


def sync_blocked_status(blocked_task_ids: Iterable[int]) -> int:
    """Recompute the auto-Blocked state for the given tasks.

    Returns the number of tasks whose status was changed. Never raises —
    a failed sync must not break the dependency/status write that
    triggered it (the next dependency event self-heals).
    """
    from origin.models.task.task_models import TaskMaster  # noqa: PLC0415

    changed = 0
    for task_id in set(blocked_task_ids):
        try:
            task = (
                TaskMaster.objects.filter(task_id=task_id)
                .only("task_id", "status", "is_deleted", "is_init_task", "is_milestone")
                .first()
            )
            if task is None or task.is_deleted or task.is_init_task or task.is_milestone:
                continue
            blocked = _has_open_blocker(task_id)
            if blocked and task.status in AUTO_BLOCKABLE_STATUSES:
                task.status = "Blocked"
                task.save(update_fields=["status", "ts_updated_at"])
                changed += 1
            elif not blocked and task.status == "Blocked":
                task.status = "Open"
                task.save(update_fields=["status", "ts_updated_at"])
                changed += 1
        except Exception:  # noqa: BLE001 — see docstring
            log.exception("sync_blocked_status failed for task %s", task_id)
    return changed
