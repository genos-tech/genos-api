"""Take one file off a task, and number the next one: what the Attachments tab and the agent share.

A task attachment is a `TaskAttachments` row and the file it points at in media storage. The REST
view (`TaskAttachmentsView.delete`) and the agent's `delete_task_attachment` tool (MIP-1563) call
the same function, so "the tool does what the page does" holds by construction rather than by two
copies that agree today. The permission check is NOT here: the page asks `can_access_task`, the tool
asks `task_access.load_task`, and each does it before it gets here.

THE RULE (MIP-1582), as it has always been and is now written down: anyone who can open the task
(a project member, its assignee, its reporter) may delete any of its files, whoever added it.
`TaskAttachments` has no uploader column; the task's history row `attachment_added` names who did it.
Changing that is a product decision (it would need the column, and a rule for files from before it).

What the delete does, in order:

  1. in ONE transaction: the row is deleted, which fires `attachment_record_remove`
     (`signals/task_signals.py`: one `attachment_removed` row in the task's history, with the actor
     and how the write arrived), and the task is stamped (`ts_updated_at`) and the change feed told,
     so a teammate's browser that syncs "tasks changed since my checkpoint" drops the file;
  2. only once that has COMMITTED, the file leaves storage (`transaction.on_commit`).

Why the row first: the two steps cannot be one atomic act, so pick the order whose failure is
harmless. A file removed first and a row that then fails to delete (or a transaction that rolls
back) leaves a LISTED file that is gone: a broken download for everyone, and nothing that retries.
A row removed first and a file that then fails to go leaves an orphan nobody can reach, logged
(attachment id and the error's type, never the path) and left to a cleanup. Media is served by path
through the row's task, so an orphan is not listed anywhere.

It touches no search index: a task's attachments are not part of what Spotlight indexes (the
chunkers read the title, body and comments), so there is nothing to purge, and the page never did.

Numbering: `next_attachment_id`. A number is never handed out twice on a task (see there).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import IntegerField, Max
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.utils import timezone

from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskAttachments, TaskMaster
from origin.services import task_cache

logger = logging.getLogger(__name__)


def next_attachment_id(task_id) -> int:
    """The number the task's next file takes: one more than any number it has EVER had.

    It was one more than the largest LIVE number, so deleting the newest file freed its number for
    the next upload. Whoever still held the old number (a tab that had not reloaded, an agent
    approval that waited) then deleted the replacement, a different file that happened to take the
    same number.

    "Ever had" is read from the task's history, which records every file added and removed with its
    number (`attachmentId`): the larger of that and the live rows. A task whose history is gone (a
    retention period, or files from before the history existed) falls back to the live rows, the
    old rule, and no worse.
    """
    live = TaskAttachments.objects.filter(task=task_id).aggregate(m=Max("attachment_id"))["m"]
    remembered = (
        TaskActivity.objects.filter(
            task_id=task_id,
            action_type__in=(
                TaskActivityActionType.ATTACHMENT_ADDED,
                TaskActivityActionType.ATTACHMENT_REMOVED,
            ),
            metadata__attachmentId__isnull=False,
        )
        .annotate(number=Cast(KeyTextTransform("attachmentId", "metadata"), IntegerField()))
        .aggregate(m=Max("number"))["m"]
    )
    return max(live or 0, remembered or 0) + 1


def delete_attachment(attachment: TaskAttachments) -> None:
    """Delete the attachment's row (and stamp the task), then its file once that has committed.

    `model.delete()` only drops the DB row; the FileField does not purge the backing file, so
    without the file step the upload directory would grow with orphans. `save=False` skips
    re-saving a row that is gone. A failure to remove the file is logged and raises nothing: the
    row is the user-facing contract, a stray file a cleanup concern.
    """
    attachment_id = attachment.attachment_id
    task_id = attachment.task_id
    stored = attachment.attached_file
    with transaction.atomic():
        attachment.delete()
        _stamp_task(task_id)
        if stored:
            transaction.on_commit(lambda: _remove_file(stored, attachment_id))


def _stamp_task(task_id) -> None:
    """The task changed (a file left it): move its `ts_updated_at` and tell the change feed.

    A queryset `.update()` skips `auto_now`, so it is stamped by hand; `post_save` is not wanted
    here (the task's own history and notices are for edits of the task). The task's team and
    project are read for the feed's keys and the cached project-tasks response.
    """
    if task_id is None:
        return
    task = TaskMaster.objects.filter(task_id=task_id).only("team_id", "project_id").first()
    if task is None:
        return
    TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=timezone.now())
    task_cache.invalidate_for_task(task)


def _remove_file(stored, attachment_id) -> None:
    try:
        stored.delete(save=False)
    except Exception as file_err:  # noqa: BLE001 - see `delete_attachment`
        logger.warning(
            "[delete_attachment] file cleanup failed for attachment_id=%s: %s",
            attachment_id,
            type(file_err).__name__,
        )
