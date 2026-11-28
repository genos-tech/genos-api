"""What a deleted message is: a tombstone with no text (MIP-1784).

A person who deletes a message expects it gone, and a team that must not keep personal data it
does not need agrees. The row stays (the thread, its `seq`, its reply count and the client's
incremental sync all hang on it), so what a delete removes is everything that SAYS something:

  * `body` and `body_text`;
  * `metadata` except the id keys a tombstone is still found by (`KEPT_METADATA_KEYS`): the
    client posts `metadata` as it likes, so a key we do not know is dropped, not trusted;
  * the `MessageMention` rows (who was named is part of what was said);
  * its `MessagePin` row, so it leaves the chat's pinned messages (MIP-2487);
  * the original file names of the `MessageAttachment` rows. The rows and the stored files stay:
    removing files is a purge with its own decision (MIP-1785);
  * the cached summary of the message's thread (`ThreadSummary`, an LLM digest of the replies),
    and its search chunk after the commit (MIP-1802).

What stays: id, channel, `seq`, sender, parent / thread root, reply count, reactions, `edited_at`,
`deleted_at` and the timestamps. `ts_updated_at` is stamped, so a client that already holds the row
(with its text) is served the blank tombstone on its next `?since=` sync and the search reindexer
re-chunks the channel without the message.

Every writer of `deleted_at` goes through `tombstone_message`, and `MessageSerializer` blanks the
same fields on the way out (`public_tombstone`), so a row deleted before this existed, or by a
writer that forgot, still leaves no text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db.models import Q
from django.utils import timezone

from origin.models.chat.unified_models import (
    Message,
    MessageAttachment,
    MessageMention,
    MessagePin,
)
from origin.models.common.erasure_models import ErasureObjectType
from origin.services import erasure_log, message_via, thread_summary_cleanup

#: The `metadata` keys a tombstone keeps: ids that link it to its task or comment, and the
#: connected-app stamp (`message_via`, already reduced to a kind and a family). Nothing in them is
#: text somebody wrote.
KEPT_METADATA_KEYS = ("taskId", "taskCommentId", message_via.KEY)


def tombstone_metadata(metadata: Any) -> dict:
    """`metadata` reduced to the keys a deleted message keeps."""
    if not isinstance(metadata, dict):
        return {}
    return {k: metadata[k] for k in KEPT_METADATA_KEYS if k in metadata}


def has_text(message: Message) -> bool:
    """Does this row still say anything? `{}` counts as empty: the retention policy writes it."""
    return (
        bool(message.body_text)
        or bool(message.body)
        or (tombstone_metadata(message.metadata) != (message.metadata or {}))
    )


def has_text_q() -> Q:
    """The rows `has_text` is true for, as a query. Metadata is checked per row by the caller."""
    return ~Q(body_text="") | (~Q(body=[]) & ~Q(body={}))


def tombstone_message(message: Message, *, now: datetime | None = None) -> bool:
    """Delete `message` the way every writer must: flag it AND clear its text, in one save.

    Returns True when this call newly deleted it, so a caller counts the parent's `reply_count`
    down once. Idempotent, and cleans a row an older delete left with its text: calling it on a
    deleted message that still says something blanks it (and stamps it) without reporting a new
    delete. Call it inside the caller's transaction; it opens none of its own.
    """
    newly_deleted = message.deleted_at is None
    if not newly_deleted and not has_text(message):
        return False
    now = now or timezone.now()
    if newly_deleted:
        message.deleted_at = now
    message.body = []
    message.body_text = ""
    message.metadata = tombstone_metadata(message.metadata)
    message.save(update_fields=["deleted_at", "body", "body_text", "metadata", "ts_updated_at"])
    MessageMention.objects.filter(message=message).delete()
    # A deleted message is not pinned any more (MIP-2487).
    MessagePin.objects.filter(message=message).delete()
    MessageAttachment.objects.filter(message=message).exclude(original_name="").update(
        original_name=""
    )
    # The cached digest of this message's thread quotes its text (MIP-1802).
    thread_summary_cleanup.forget_message_thread(message)
    if newly_deleted:
        erasure_log.record(
            ErasureObjectType.MESSAGE, message.pk
        )  # MIP-2249: replayed after a restore
    return newly_deleted


def public_tombstone(data: dict) -> dict:
    """`MessageSerializer`'s output for a deleted message, with the text taken out.

    The same fields `tombstone_message` clears, so a reader never depends on the writer having
    run. Wire keys: `body`, `bodyText`, `attachments`, `mentions`, `metadata`.
    """
    data["body"] = []
    data["bodyText"] = ""
    data["attachments"] = []
    data["mentions"] = []
    data["metadata"] = tombstone_metadata(data.get("metadata"))
    return data
