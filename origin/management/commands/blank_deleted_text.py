"""Clear the text a deleted chat message or task comment still holds (MIP-1784).

A delete now blanks the text in the same save (`services/message_tombstone`,
`task_comment_manage.soft_delete_comment`). Rows deleted BEFORE that still hold it. This command
clears them.

    python manage.py blank_deleted_text                 # DRY RUN: counts only, writes nothing
    python manage.py blank_deleted_text --apply --yes   # blank them

What it does, to DELETED rows only (a live message or comment is never touched):

  * `Message` with `deleted_at` set and a `body` / `body_text` that still says something:
    `body` to `[]`, `body_text` to `""`, `metadata` cut to its id keys, its
    `MessageMention` rows deleted, its attachments' `original_name` cleared. The tombstone
    (id, `seq`, sender, `deleted_at`, thread links, reply count) stays; `ts_updated_at` is
    stamped, so a client that holds the old text is served the blank row on its next sync and
    the search reindexer re-chunks the channel without it.
  * the same for a deleted message's mentions and attachment names when its body is already
    blank (a row the retention policy emptied).
  * `TaskComments` with `is_deleted` and a `comment_body` still there: `comment_body` to `[]`,
    stamped.
  * the mirror `Message` of a deleted comment that an older delete left LIVE: tombstoned the way
    the delete does (and its parent's `reply_count` counted down once).

It does NOT delete a row, a file, a reaction or a history row, and it changes no retention
period. Output is COUNTS ONLY: no id, name or word of any message is printed. Safe to re-run: the
second `--apply` finds nothing. Each batch is one transaction.

Run it after the release that blanks on delete is deployed, not before (an older release would
keep writing deleted rows with their text). The search index follows on the next
`opensearch_reindex` pass (cron cadence). Production: count first (the dry run), then apply.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from origin.models.chat.unified_models import Message, MessageAttachment, MessageMention
from origin.models.task.task_models import TaskComments
from origin.services import message_tombstone, unified_writer

DEFAULT_BATCH = 200


def deleted_messages_with_text():
    """Deleted messages that still say something (their metadata is checked per batch)."""
    return Message.objects.filter(deleted_at__isnull=False).filter(message_tombstone.has_text_q())


def deleted_message_ids_with_loose_ends() -> set:
    """Deleted messages, blank or not, that still have mention rows or attachment names."""
    mentioned = MessageMention.objects.filter(message__deleted_at__isnull=False).values_list(
        "message_id", flat=True
    )
    named = (
        MessageAttachment.objects.filter(message__deleted_at__isnull=False)
        .exclude(original_name="")
        .values_list("message_id", flat=True)
    )
    return set(mentioned) | set(named)


def deleted_comments_with_text():
    return (
        TaskComments.objects.filter(is_deleted=True)
        .exclude(comment_body=[])
        .exclude(comment_body={})
    )


def live_mirrors_of_deleted_comments() -> list:
    """Primary keys of mirror messages that are live although their comment is deleted."""
    ids = {
        unified_writer.task_comment_message_uuid(task_id, comment_id): None
        for task_id, comment_id in TaskComments.objects.filter(is_deleted=True, task__isnull=False)
        .values_list("task_id", "comment_id")
        .iterator(chunk_size=2000)
    }
    keys = list(ids)
    found: list = []
    for start in range(0, len(keys), 2000):
        found.extend(
            Message.objects.filter(
                id__in=keys[start : start + 2000], deleted_at__isnull=True
            ).values_list("id", flat=True)
        )
    return found


def _batches(pks: list, size: int):
    for start in range(0, len(pks), size):
        yield pks[start : start + size]


class Command(BaseCommand):
    help = (
        "Find (dry run, the default) or blank (--apply --yes) the text of chat messages and "
        "task comments that were deleted before a delete cleared it. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Blank the rows (needs --yes).")
        parser.add_argument("--yes", action="store_true", help="Confirm --apply.")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH,
            help=f"Rows per transaction (default {DEFAULT_BATCH}).",
        )

    def handle(self, *args, **opts):
        if opts["apply"] and not opts["yes"]:
            raise CommandError("--apply changes rows: add --yes to confirm (or drop --apply).")
        if opts["batch_size"] < 1:
            raise CommandError("--batch-size must be >= 1.")
        apply = bool(opts["apply"])
        size = opts["batch_size"]
        self.stdout.write("blank_deleted_text: " + ("APPLY" if apply else "DRY RUN"))

        message_pks = list(deleted_messages_with_text().values_list("pk", flat=True))
        loose_pks = list(deleted_message_ids_with_loose_ends() - set(message_pks))
        comment_pks = list(deleted_comments_with_text().values_list("pk", flat=True))
        mirror_pks = live_mirrors_of_deleted_comments()

        if apply:
            for batch in _batches(message_pks, size):
                with transaction.atomic():
                    for message in Message.objects.filter(pk__in=batch, deleted_at__isnull=False):
                        message_tombstone.tombstone_message(message)
            for batch in _batches(loose_pks, size):
                with transaction.atomic():
                    MessageMention.objects.filter(message_id__in=batch).delete()
                    MessageAttachment.objects.filter(message_id__in=batch).exclude(
                        original_name=""
                    ).update(original_name="")
            for batch in _batches(comment_pks, size):
                with transaction.atomic():
                    TaskComments.objects.filter(pk__in=batch, is_deleted=True).update(
                        comment_body=[], ts_updated_at=timezone.now()
                    )
            for batch in _batches(mirror_pks, size):
                with transaction.atomic():
                    for message in Message.objects.filter(pk__in=batch, deleted_at__isnull=True):
                        message_tombstone.tombstone_message(message)
                        if message.parent_id is not None:
                            Message.objects.filter(pk=message.parent_id, reply_count__gt=0).update(
                                reply_count=F("reply_count") - 1,
                                ts_updated_at=timezone.now(),
                            )

        verb = "blanked" if apply else "would blank"
        self.stdout.write(
            f"{verb}: {len(message_pks)} deleted message(s) with text, "
            f"{len(loose_pks)} deleted message(s) with mentions or attachment names only, "
            f"{len(comment_pks)} deleted task comment(s), "
            f"{len(mirror_pks)} comment mirror(s) left live by a deleted comment"
        )
        if not apply and (message_pks or loose_pks or comment_pks or mirror_pks):
            self.stdout.write("Nothing was written. Re-run with --apply --yes to blank them.")
        if apply:
            self.stdout.write(
                "Done. The search index follows on the next opensearch_reindex pass; "
                "a connected client is served the blank rows on its next sync."
            )
