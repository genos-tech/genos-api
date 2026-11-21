"""Hard-delete the tasks that have been "Deleted" for more than 30 days (MIP-1785).

The app's "delete a task" writes `status = "Deleted"` and nothing else (`genos-frontend`
`deleteTaskCommit.ts`); the row, its comments, its files, its history and its collaborative body
stayed for ever. A deleted task stays restorable for `GRACE_DAYS` days (the maintainer's decision,
2026-11-21); after that it is purged here. `python manage.py purge_deleted_tasks` is the only
caller.

WHAT "DELETED FOR MORE THAN 30 DAYS" RELIES ON. A task has no `deleted_at` column. The moment it
became Deleted is recorded in its history: the `status_changed` row whose `new_value` is
"Deleted" (the post_save diff in `signals/task_signals.py`), and a task that is restored and
deleted again has a newer row, so the LATEST such row is the clock. The task's `ts_updated_at`
must also be older than the grace: any save since the deletion moves it, so a task somebody
touched, or flipped by a write that left no history row, waits. A task with NO such history row
is KEPT: its deletion time is not recorded anywhere, and it is not guessed.

WHAT IS NEVER PURGED (each is counted by reason, never listed):
  * a task that is not Deleted any more (re-checked under a row lock in its own transaction, so a
    restore that races the run survives), an `is_init_task` scaffold, a milestone's backing task or
    any task a milestone points at (`task` / `converted_to_task`, both `SET_NULL`);
  * a task with sub-tasks left (`parent_task_id` / `root_task_id` are plain integers, no cascade:
    purging the parent would strand them, and a child that reads as Deleted only because of its
    parent would come back). Children are purged first, so a parent goes on a later run;
  * a task a dependency still ties to a task that is not Deleted: removing the edge re-derives the
    other task's status (`dependency_signals`), and that is a write to something live;
  * the task that holds its project's highest `project_task_number`: the next number is MAX + 1
    over the rows left, so purging it would hand its display id to the next new task, and a stale
    branch carrying the old id would then link to, and close, the new one.

WHAT GOES WITH A PURGED TASK, through the machinery that already exists:
  * the task row, and what cascades with it (comments and their reactions and mention rows,
    history, tags, progress rows, recents, deliveries, dependencies between deleted tasks, the
    task's notes with their permission / version / summary rows via `batched_note_cleanup`);
  * the uploaded files: the task's attachments and body images (only when the path is under the
    task's own folder and no other row names the same file), and the task notes' files
    (`note_delete.forget_note_attachments`, `detached_note_attachments._classify`). Files go AFTER
    the commit, best-effort, like `task_attachment_manage` and `note_delete`;
  * the project chat's copy of it: the task card and the comment mirrors are tombstoned
    (`message_tombstone`, the chat's own delete: text gone, row kept for the thread and the sync),
    and the inbox rows (`Activity`) that point at them. People's own chat replies in the card's
    thread are not comments and are left;
  * the search chunks, after the commit (`project_delete._forget_search`, the same call a project
    delete makes); the reindexer's orphan sweep retries one that fails.

WHAT IS LEFT, and counted so it is on the page: `AgentSession` rows scoped to the task ("Ask Genos
about this task" history), `MemberItemDay` / `MemberImportanceMark` rows (ids and counters, no
text), and the collaborative bodies: `yjs_documents` belongs to genos-collab, which this process
does not call. Once the task row is gone, collab's `npm run sweep:orphans` finds `task-body:<id>`
and `task-note:<id>` parentless and deletes them with `--delete`.

Output is COUNTS ONLY: no title, id, file name or text is printed or logged.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Max, OuterRef, Q, Subquery
from django.utils import timezone

from origin.models.chat.unified_models import Activity, Message
from origin.models.common.item_activity_models import MemberItemDay
from origin.models.common.member_importance_models import MemberImportanceMark
from origin.models.note.task_note_models import TaskNoteAttachmentFact, TaskNoteMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_delivery_models import TaskDelivery
from origin.models.task.task_models import (
    TaskAttachments,
    TaskBodyAttachmentFact,
    TaskComments,
    TaskDependency,
    TaskMaster,
)
from origin.search_engine.models import AgentSession, RagChunk
from origin.services import message_tombstone
from origin.services.activity_suppression import suppress_task_activity
from origin.services.detached_note_attachments import DetachedReport, _classify
from origin.services.note_delete import (
    forget_deleted_notes,
    forget_note_attachments,
)
from origin.services.note_satellites import batched_note_cleanup
from origin.services.task_files import remove_now, stored_files_of_tasks
from origin.services.v3_activity import SURFACE_TASK_BODY
from origin.views.utils.note_role import NOTE_TYPE_TASK

logger = logging.getLogger(__name__)

GRACE_DAYS = 30
DEFAULT_LIMIT = 20

# Why a task was not purged. The text is printed, so it says it plainly.
KEPT_NOT_DELETED = "no longer deleted"
KEPT_NO_TIME = "no deletion time recorded"
KEPT_RECENT = "deleted within 30 days"
KEPT_TOUCHED = "changed within 30 days"
KEPT_MILESTONE = "milestone or milestone-linked"
KEPT_SUBTASKS = "has sub-tasks left"
KEPT_LINKED = "still linked to a live task"
KEPT_HIGHEST = "highest number in its project"

_GONE = "gone"


@dataclass
class PurgeReport:
    apply: bool
    limit: int
    grace_days: int = GRACE_DAYS
    scanned: int = 0  # tasks in status Deleted
    selected: int = 0  # taken (or, in a dry run, that would be) in this run
    waiting: int = 0  # past every guard, but beyond this run's limit
    purged: int = 0
    kept: Counter = field(default_factory=Counter)
    counts: Counter = field(default_factory=Counter)  # rows per kind, for the selected tasks
    left: Counter = field(default_factory=Counter)  # what a purge does not remove
    files_removed: int = 0
    files_kept: int = 0  # a file whose path or sharing made it unsafe to delete
    file_errors: int = 0


# ---------------------------------------------------------------------------------------------
# Which tasks
# ---------------------------------------------------------------------------------------------


def _deleted_at():
    """The moment the task became Deleted: its latest `status_changed` history row to "Deleted"."""
    return Subquery(
        TaskActivity.objects.filter(
            task=OuterRef("pk"),
            action_type=TaskActivityActionType.STATUS,
            field_name="status",
            new_value="Deleted",
        )
        .order_by()
        .values("task")
        .annotate(m=Max("ts_created_at"))
        .values("m")[:1]
    )


def _reason(task_id: int, cutoff: datetime) -> str | None:
    """Why this task must NOT be purged now, or None when it may. Reads the row fresh."""
    row = (
        TaskMaster.objects.filter(task_id=task_id)
        .annotate(deleted_at=_deleted_at())
        .values(
            "status",
            "is_init_task",
            "is_milestone",
            "ts_updated_at",
            "deleted_at",
            "project_id",
            "project_task_number",
        )
        .first()
    )
    if row is None:
        return _GONE
    if row["status"] != "Deleted" or row["is_init_task"]:
        return KEPT_NOT_DELETED
    if row["deleted_at"] is None:
        return KEPT_NO_TIME
    if row["deleted_at"] >= cutoff:
        return KEPT_RECENT
    if row["ts_updated_at"] >= cutoff:
        return KEPT_TOUCHED
    if (
        row["is_milestone"]
        or MilestoneMaster.objects.filter(
            Q(task_id=task_id) | Q(converted_to_task_id=task_id)
        ).exists()
    ):
        return KEPT_MILESTONE
    if (
        TaskMaster.objects.filter(Q(parent_task_id=task_id) | Q(root_task_id=task_id))
        .exclude(task_id=task_id)
        .exists()
    ):
        return KEPT_SUBTASKS
    edges = TaskDependency.objects.filter(Q(blocker_task_id=task_id) | Q(blocked_task_id=task_id))
    other_ends = set(edges.values_list("blocker_task_id", flat=True)) | set(
        edges.values_list("blocked_task_id", flat=True)
    )
    other_ends.discard(task_id)
    if (
        other_ends
        and TaskMaster.objects.filter(task_id__in=other_ends).exclude(status="Deleted").exists()
    ):
        return KEPT_LINKED
    number = row["project_task_number"]
    if (
        number is not None
        and row["project_id"] is not None
        and not TaskMaster.objects.filter(
            project_id=row["project_id"], project_task_number__gt=number
        ).exists()
    ):
        return KEPT_HIGHEST
    return None


# ---------------------------------------------------------------------------------------------
# What hangs off a task
# ---------------------------------------------------------------------------------------------


def _inbox_rows(task_id: int):
    """The `Activity` rows that point at the task: on its card / comment messages, and the
    task-body @mentions (surface 5, `meta.taskId` as a number or as text)."""
    return Activity.objects.filter(
        Q(message__task_id=task_id)
        | Q(surface_type=SURFACE_TASK_BODY, meta__taskId=task_id)
        | Q(surface_type=SURFACE_TASK_BODY, meta__taskId=str(task_id))
    )


def _inventory(task_id: int) -> tuple[Counter, Counter]:
    """(what a purge removes, what it leaves) for one task, as row counts."""
    cards = Message.objects.filter(task_id=task_id, is_thread_reply=False)
    removes = Counter(
        {
            "attachments": TaskAttachments.objects.filter(task_id=task_id).count(),
            "body images": TaskBodyAttachmentFact.objects.filter(task_id=task_id).count(),
            "note files": TaskNoteAttachmentFact.objects.filter(task_id=task_id).count(),
            "notes": TaskNoteMaster.objects.filter(task_id=task_id).count(),
            "comments": TaskComments.objects.filter(task_id=task_id).count(),
            "chat messages": Message.objects.filter(task_id=task_id).count(),
            "inbox rows": _inbox_rows(task_id).count(),
            "dependencies": TaskDependency.objects.filter(
                Q(blocker_task_id=task_id) | Q(blocked_task_id=task_id)
            ).count(),
            "delivery links": TaskDelivery.objects.filter(task_id=task_id).count(),
            "history rows": TaskActivity.objects.filter(task_id=task_id).count(),
            "search chunks": RagChunk.objects.filter(
                entity_type="task", entity_id=f"task:{task_id}"
            ).count(),
        }
    )
    left = Counter(
        {
            "agent conversations": AgentSession.objects.filter(task_id=task_id).count(),
            "member counters": (
                MemberItemDay.objects.filter(task_id=task_id).count()
                + MemberImportanceMark.objects.filter(task_id=task_id).count()
            ),
            "chat replies that are not comments": Message.objects.filter(
                thread_root_id__in=cards.values("id")
            )
            .exclude(task_id=task_id)
            .count(),
        }
    )
    return removes, left


def _files_to_remove(task_id: int, report: PurgeReport) -> list[str]:
    """The stored files of the task's own rows that are safe to delete after the rows go.

    The attachments and body images follow the rule every task delete uses (`task_files`, MIP-1824):
    under the task's own folder, and no other row names the same file. Anything else is counted and
    left: the row goes either way, and a file we cannot prove is ours is never removed."""
    names, kept = stored_files_of_tasks([task_id])
    report.files_kept += kept
    # What is left of the task notes' files after `forget_note_attachments` took the ones whose note
    # is alive: rows whose note was already gone, judged by the same rule the detached-file purge uses.
    for name in TaskNoteAttachmentFact.objects.filter(task_id=task_id).values_list(
        "note_attachment_url", flat=True
    ):
        if name and _classify("task", name, TaskNoteMaster, DetachedReport(removable={})):
            names.append(name)
        elif name:
            report.files_kept += 1
    return names


# ---------------------------------------------------------------------------------------------
# The purge
# ---------------------------------------------------------------------------------------------


def _purge_one(task_id: int, cutoff: datetime, report: PurgeReport) -> str | None:
    """Purge one task in its own transaction. Returns why it was NOT purged, or None."""
    with transaction.atomic():
        task = TaskMaster.objects.select_for_update().filter(task_id=task_id).first()
        if task is None:
            return _GONE
        # Read again under the lock: a restore, an edit or a new sub-task since the scan wins.
        reason = _reason(task_id, cutoff)
        if reason:
            return reason
        removes, left = _inventory(task_id)
        note_ids = list(
            TaskNoteMaster.objects.filter(task_id=task_id).values_list("note_id", flat=True)
        )

        _inbox_rows(task_id).delete()
        # The chat's copy of the task (its card and its comments' mirrors): the chat's own delete.
        for message in Message.objects.filter(task_id=task_id):
            message_tombstone.tombstone_message(message)

        forget_note_attachments(NOTE_TYPE_TASK, note_ids)  # BEFORE the notes cascade away
        files = _files_to_remove(task_id, report)
        # Under `suppress_task_activity`: a post_delete that writes a history row mid-cascade
        # (attachment_removed, comment_deleted) lands after the collector has run, names a task
        # that is going, and fails the commit on the deferred foreign key. The task's whole
        # history goes with it anyway. No dependency to a live task gets here (`KEPT_LINKED`).
        with batched_note_cleanup(), suppress_task_activity():
            task.delete()
        forget_deleted_notes(NOTE_TYPE_TASK, note_ids)

        report.counts.update(removes)
        report.left.update(left)
        transaction.on_commit(lambda: _after_commit(task_id, note_ids, files, report), robust=True)
    return None


def _after_commit(task_id: int, note_ids: list[int], files: list[str], report: PurgeReport) -> None:
    """Only once the delete is committed: the files, and the search index. Best-effort both."""
    if files:
        failed = remove_now(files, family="task")
        report.files_removed += len(files) - failed
        report.file_errors += failed
    from origin.services.project_delete import _forget_search

    _forget_search([task_id], [], note_ids, [])


def purge_deleted_tasks(
    *, apply: bool, limit: int = DEFAULT_LIMIT, now: datetime | None = None
) -> PurgeReport:
    """Find (and, with `apply`, purge) the tasks Deleted for more than `GRACE_DAYS` days.

    Dry run by default: nothing is written; `selected` is what the next `apply` would take (at
    most `limit`), `waiting` what is past every guard but beyond the limit, and `kept` says why the
    rest stays. At most `limit` tasks are purged per run, newest id first, so a child goes before
    its parent. Safe to run twice: the second run finds only what was kept."""
    cutoff = (now or timezone.now()) - timedelta(days=GRACE_DAYS)
    report = PurgeReport(apply=apply, limit=limit)
    ids = list(
        TaskMaster.objects.filter(status="Deleted", is_init_task=False)
        .order_by("-task_id")
        .values_list("task_id", flat=True)
    )
    report.scanned = len(ids)
    for task_id in ids:
        reason = _reason(task_id, cutoff)
        if reason:
            report.kept[reason] += 1
            continue
        if report.selected >= limit:
            report.waiting += 1
            continue
        if not apply:
            removes, left = _inventory(task_id)
            report.counts.update(removes)
            report.left.update(left)
            report.selected += 1
            continue
        reason = _purge_one(task_id, cutoff, report)
        if reason:
            if reason != _GONE:
                report.kept[reason] += 1
            continue
        report.selected += 1
        report.purged += 1
    return report
