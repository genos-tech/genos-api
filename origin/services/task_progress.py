"""Progress tracking for tasks and milestones (MIP-1351).

`TaskMaster.last_progress_at` answers "has anything happened at or under this
task lately?" without collecting every descendant's activity by hand. Every
audit row `_record` writes (origin/signals/task_signals.py) is offered here;
the ones that count as progress

  * insert a `TaskProgressFact` for the task itself and for each ancestor above
    it (a task's parent chain ends at the milestone's backing task, so one walk
    covers task -> parent task -> milestone), and
  * queue the same set for a `last_progress_at` refresh, applied by the change
    feed's flush after the transaction commits (see below).

What does NOT count:

  * a change carried along by a bulk operation (`metadata.cascade`): moving a
    milestone must not look like progress on each of its 50 tasks;
  * a status flip derived from dependency edges (`derivedFromDependencies`):
    nobody did anything.

Both still move `ts_updated_at`, which is for sync, not for meaning.

How the value moves. It is RECOMPUTED, not carried: the refresh sets each
queued task to the newest fact it has (never lowering it), with a queryset
`.update()` so `ts_updated_at` stays put. Three reasons:

  * After commit, not inside the transaction: one edit under a busy milestone
    would otherwise hold the milestone's row lock until it commits, and two
    transactions touching the same siblings in opposite order could deadlock on
    it.
  * A set of ids drained by "the next flush" strands what a rolled-back
    transaction queued. Carrying a TIME would then apply a change that never
    happened; recomputing from committed facts makes a stranded id a harmless
    no-op.
  * It runs as the first step of `change_feed.flush`, the one place versions
    move. A client that sees the new version must read the new value, and the
    flush also clears the cached project-tasks response, which must not be
    rebuilt from the old one. `offer` queues a bump, so a flush always follows.

Best effort, like the change feed: a failure here is logged and never fails the
write being audited. The facts are the record; `backfill_task_progress`
rebuilds both from `TaskActivity`.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import datetime
from typing import Mapping, Optional

from django.db import transaction
from django.db.models import OuterRef, Subquery

from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.models.task.task_progress_models import TaskProgressFact
from origin.services import change_feed

log = logging.getLogger(__name__)

# Same cap as every other tree walk: a cycle in `parent_task_id` is corrupt
# data, and a walk that stops is better than one that spins.
MAX_DEPTH = 64


def counts_as_progress(metadata: Optional[Mapping]) -> bool:
    """Is an audit row with this metadata progress? See the module docstring."""
    metadata = metadata or {}
    return not metadata.get("cascade") and not metadata.get("derivedFromDependencies")


def ancestor_ids(parent_task_id: Optional[int], lookup) -> list[int]:
    """The chain above a task, nearest first, from its `parent_task_id`.

    `lookup(task_id)` returns None when no such task exists, else a 1-tuple of
    that task's own parent id (which may be None). Stops at a task that does not
    exist (a dangling `parent_task_id` must not become a fact pointing nowhere:
    the foreign key is only checked at COMMIT, past any savepoint), at a cycle,
    and at `MAX_DEPTH`.
    """
    chain: list[int] = []
    seen: set[int] = set()
    current = parent_task_id
    while current is not None and current not in seen and len(chain) < MAX_DEPTH:
        row = lookup(current)
        if row is None:
            break
        chain.append(current)
        seen.add(current)
        (current,) = row
    return chain


def _db_lookup(task_id: int):
    return TaskMaster.objects.filter(task_id=task_id).values_list("parent_task_id").first()


class _Pending(threading.local):
    def __init__(self) -> None:
        self.ids: set[int] = set()


_pending = _Pending()


def reset_pending() -> None:
    """Forget what this thread has queued. For tests."""
    _pending.ids.clear()


def offer(activity: TaskActivity, task: TaskMaster) -> None:
    """Called by `_record` for every audit row it writes. Never raises."""
    try:
        if not counts_as_progress(activity.metadata):
            return
        # A savepoint, so a failing insert cannot poison the caller's
        # transaction (and with it the write being audited).
        with transaction.atomic():
            # dict.fromkeys: a cycle must not list a task twice
            ids = list(
                dict.fromkeys([task.task_id, *ancestor_ids(task.parent_task_id, _db_lookup)])
            )
            TaskProgressFact.objects.bulk_create(
                [
                    TaskProgressFact(
                        ancestor_task_id=ancestor,
                        source_task_id=task.task_id,
                        activity=activity,
                        actor_id=activity.actor_id,
                        action_type=activity.action_type,
                        depth=depth,
                        ts=activity.ts_created_at,
                    )
                    for depth, ancestor in enumerate(ids)
                ],
                ignore_conflicts=True,
            )
        _pending.ids.update(ids)
        # The refresh runs in the change feed's flush, so make sure one follows.
        change_feed.bump_tasks(task.team_id, task.project_id)
    except Exception:  # noqa: BLE001 — an audit side-table must never fail the write
        log.exception("task progress: could not record progress for task %s", task.pk)


def drain() -> None:
    """Refresh `last_progress_at` for everything queued on this thread.

    Called first thing by `change_feed.flush`. Never raises.
    """
    if not _pending.ids:
        return
    ids, _pending.ids = _pending.ids, set()
    try:
        refresh(ids)
    except Exception:  # noqa: BLE001
        log.exception("task progress: could not refresh last_progress_at for %s", sorted(ids))


def refresh(ids) -> int:
    """Raise each task's `last_progress_at` to the newest fact it has.

    Never lowers a value, and never writes `ts_updated_at`. Returns how many
    rows moved.
    """
    newest = (
        TaskProgressFact.objects.filter(ancestor_task_id=OuterRef("task_id"))
        .order_by("-ts")
        .values("ts")[:1]
    )
    by_ts: dict[datetime, list[int]] = defaultdict(list)
    for task_id, current, latest in (
        TaskMaster.objects.filter(task_id__in=ids)
        .annotate(latest=Subquery(newest))
        .values_list("task_id", "last_progress_at", "latest")
    ):
        if latest is not None and (current is None or current < latest):
            by_ts[latest].append(task_id)
    moved = 0
    for ts, task_ids in by_ts.items():
        # A queryset update: no post_save, and `auto_now` is not applied.
        moved += TaskMaster.objects.filter(task_id__in=task_ids).update(last_progress_at=ts)
    return moved
