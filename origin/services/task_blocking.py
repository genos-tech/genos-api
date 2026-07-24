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

Auto-transitions apply to plain tasks AND milestones in active states:

  * **Milestones ARE auto-transitioned** (changed 2026-07-25, fe request
    — reverses the original "milestones keep their own vocabulary"
    exclusion). A milestone's canonical status lives on
    `MilestoneMaster.status`, NOT the backing `TaskMaster.status` (which
    is a mirror the table reads and `sync_backing_task` overwrites on the
    next PATCH), so we write BOTH: the milestone's own status field and
    its backing row, keeping preview (reads MilestoneMaster) and table
    (reads backing) in agreement. Milestone aggregations already treat
    "Blocked" as a valid milestone status (`get_milestone_*` tools).
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


def _next_status(current: str | None, blocked: bool) -> str | None:
    """The auto-transition target for `current`, or None for a no-op.

    Blocked gain → "Blocked" (only from Open/WIP/Pending); last blocker
    cleared → "Open" (only from exactly "Blocked"). Everything else is a
    no-op so a manual override (WIP, hand-set Blocked, Closed) survives.
    """
    if blocked and current in AUTO_BLOCKABLE_STATUSES:
        return "Blocked"
    if not blocked and current == "Blocked":
        return "Open"
    return None


def sync_blocked_status(blocked_task_ids: Iterable[int]) -> int:
    """Recompute the auto-Blocked state for the given tasks / milestones.

    Returns the number of rows whose status was changed. Never raises —
    a failed sync must not break the dependency/status write that
    triggered it (the next dependency event self-heals).
    """
    from origin.models.task.milestone_models import MilestoneMaster  # noqa: PLC0415
    from origin.models.task.task_models import TaskMaster  # noqa: PLC0415

    changed = 0
    for task_id in set(blocked_task_ids):
        try:
            task = (
                TaskMaster.objects.filter(task_id=task_id)
                .only("task_id", "status", "is_deleted", "is_init_task", "is_milestone")
                .first()
            )
            if task is None or task.is_deleted or task.is_init_task:
                continue
            blocked = _has_open_blocker(task_id)

            if task.is_milestone:
                # Canonical milestone status is MilestoneMaster.status; the
                # backing TaskMaster.status is a mirror. Transition off the
                # milestone's own status, then write BOTH so preview and
                # table agree. `Open↔Blocked` doesn't cross the closed
                # boundary, so the backing save's blocker signal is a
                # no-op — recursion still terminates.
                milestone = (
                    MilestoneMaster.objects.filter(task_id=task_id, is_deleted=False)
                    .only("milestone_id", "status")
                    .first()
                )
                if milestone is None:
                    continue
                new_status = _next_status(milestone.status, blocked)
                if new_status is None:
                    continue
                milestone.status = new_status
                milestone.save(update_fields=["status", "ts_updated_at"])
                task.status = new_status
                task.save(update_fields=["status", "ts_updated_at"])
                changed += 1
            else:
                new_status = _next_status(task.status, blocked)
                if new_status is None:
                    continue
                task.status = new_status
                task.save(update_fields=["status", "ts_updated_at"])
                changed += 1
        except Exception:  # noqa: BLE001 — see docstring
            log.exception("sync_blocked_status failed for task %s", task_id)
    return changed
