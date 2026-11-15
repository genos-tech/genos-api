"""Take one file off a task: what the Attachments tab's delete and the agent's tool share (MIP-1563).

A task attachment is a `TaskAttachments` row and the file it points at in media storage. The REST
view (`TaskAttachmentsView.delete`) and the agent's `delete_task_attachment` tool call the same
function, so "the tool does what the page does" holds by construction rather than by two copies
that agree today. The permission check is NOT here: the page asks `can_access_task`, the tool asks
`task_access.load_task`, and each does it before it gets here.

What the delete does, in order:

  1. the file leaves storage (`attached_file.delete(save=False)`), best effort: a file that is
     already gone (an orphan of a half-finished earlier delete) must not keep the row alive;
  2. the row is deleted, which fires `attachment_record_remove` (`signals/task_signals.py`): one
     `attachment_removed` row in the task's history, with the actor and how the write arrived.

It touches no search index: a task's attachments are not part of what Spotlight indexes (the
chunkers read the title, body and comments), so there is nothing to purge, and the page never did.
"""

from __future__ import annotations

import logging

from origin.models.task.task_models import TaskAttachments

logger = logging.getLogger(__name__)


def delete_attachment(attachment: TaskAttachments) -> None:
    """Remove the attachment's file from storage, then its row.

    `model.delete()` only drops the DB row; the FileField does not purge the backing file, so
    without the first step the upload directory would grow with orphans. `save=False` skips
    re-saving the row that is about to go. A failure to remove the file is logged (the attachment
    id and the error's type, never the path, which holds the uploaded file's name) and does not
    stop the row going: the row is the user-facing contract, a stray file a cleanup concern.
    """
    attachment_id = attachment.attachment_id
    if attachment.attached_file:
        try:
            attachment.attached_file.delete(save=False)
        except Exception as file_err:  # noqa: BLE001 - see the docstring
            logger.warning(
                "[delete_attachment] file cleanup failed for attachment_id=%s: %s",
                attachment_id,
                type(file_err).__name__,
            )
    attachment.delete()
