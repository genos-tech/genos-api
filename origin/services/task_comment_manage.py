"""Edit and delete a task comment: what the page and the agent share (MIP-880).

A task comment is a `TaskComments` row, and the PM task thread shows it through
a mirror `Message`. Changing one without the other leaves the two places the
same comment appears disagreeing, so both are kept here, once. The REST view
(`TaskCommentsView.delete`) and the agent's `delete_comment` tool call the same
delete; the agent's `update_comment` calls the edit, which also notifies anyone
the edit newly @-mentions, as a new comment would.
"""

from __future__ import annotations

import logging
from typing import Any

from origin.models.task.task_models import TaskComments
from origin.search_engine.purge import purge_task_comment
from origin.services import unified_writer, v3_activity
from origin.services.task_comment_fanout import mentioned_ids
from origin.services.webpush_dispatch import schedule_push_for_activities

logger = logging.getLogger(__name__)


def live_comment_count(task_id: int) -> int:
    """The task's comments that have not been deleted. The page sets its chip
    from this number, so it must be counted after a delete, not before."""
    return TaskComments.objects.filter(task=task_id, is_deleted=False).count()


def soft_delete_comment(comment: TaskComments) -> int:
    """Soft-delete one comment, and return the task's live comment count.

    Soft, not hard: `comment_id` is a per-task sequence taken from a row COUNT
    when a comment is added, so removing the row would make the next comment
    reuse the id. Flipping `is_deleted` also drives the `comment_record` signal
    (a COMMENT_DELETED activity), and every live-comment reader already filters
    on the flag. Idempotent: deleting a deleted comment writes nothing again.
    """
    if not comment.is_deleted:
        # `ts_updated_at` is `auto_now`, so it must be listed for the save to
        # refresh it.
        comment.is_deleted = True
        comment.save(update_fields=["is_deleted", "ts_updated_at"])
        # Tombstone the v3 mirror too: the PM task thread renders comments only
        # through it, so skipping this leaves the deleted comment on screen.
        unified_writer.soft_delete_task_comment_as_thread_reply(
            task_id=int(comment.task_id), comment_id=int(comment.comment_id)
        )
        # Best-effort: drop the comment's chunk from OpenSearch now. The
        # `ts_updated_at` bump also marks the task dirty for the incremental
        # reindexer, which re-chunks without it.
        purge_task_comment(comment.task_id, comment.comment_id)
    return live_comment_count(comment.task_id)


def edit_comment(comment: TaskComments, body: list[dict[str, Any]]) -> list[str]:
    """Replace a comment's body, carry the edit to the thread, and notify.

    Returns the ids of the people this edit newly @-mentioned (they are
    notified as a new comment would notify them); people who were already
    mentioned are not told twice. Nothing after the save can undo it: the
    mirror and the notifications are best-effort.
    """
    before = mentioned_ids(comment.comment_body)
    comment.comment_body = body
    comment.save(update_fields=["comment_body", "ts_updated_at"])

    mirror = unified_writer.update_task_comment_thread_reply(
        task_id=int(comment.task_id), comment_id=int(comment.comment_id), body=body
    )
    added = sorted(mentioned_ids(body) - before)
    if not added or mirror is None or mirror.sender is None:
        return []
    try:
        activities = v3_activity.create_mention_activities(
            message=mirror,
            mentioned_user_ids=added,
            actor=mirror.sender,
            skip_actor=False,
        )
        schedule_push_for_activities(list(activities))
    except Exception as exc:  # noqa: BLE001 — the edit is already saved
        logger.warning("task-comment edit mention fan-out failed: %s", exc)
    return added
