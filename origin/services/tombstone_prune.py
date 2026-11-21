"""Purge the empty tombstone rows of deleted messages (MIP-1937).

A deleted message stays as a row with no text (`message_tombstone`): its thread, its `seq`, its
reply count and the client's incremental sync hang on it. After `MAX_DELTA_AGE_DAYS` (60) a client
whose checkpoint is older than that reloads the chat in full, so no client can still need the
tombstone to learn of the delete. This removes such rows once nothing else needs them.

A row is purged only when ALL of these hold:
  * deleted, and last touched, before the cutoff;
  * it says nothing (`message_tombstone.has_text` is false) and has no attachment rows;
  * it is not the chat's highest `seq` (a new message takes `max(seq) + 1`: removing the top row
    would hand its number out again);
  * no read cursor points at it (`last_read_message` is SET_NULL: removing it would reset a
    person's unread count);
  * no reply of its thread is live or was deleted inside the window (replies cascade with their
    root; they are checked because the cascade must never take a live message).
Its `Activity` rows go with it, and the outbox rows and reminder cards that quote them first
(`erase_notification_traces`), because those links are SET_NULL.

Task comments are NOT purged here: the `TaskComments` row stays as the slot of its `comment_id`
(allocated as a row count + 1 in three places), so only the comment's mirror `Message` goes.

No model change. Dry run by default; counts only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from origin.models.chat.unified_models import Activity, Message, MessageAttachment, ReadCursor
from origin.services import message_tombstone
from origin.services.deleted_task_purge import erase_notification_traces
from origin.views.utils.incremental import MAX_DELTA_AGE_DAYS

DEFAULT_LIMIT = 500
_SCAN = 1000


@dataclass
class PruneResult:
    matched: int = 0
    changed: int = 0
    dry_run: bool = True
    kept: dict[str, int] = field(default_factory=dict)


def _candidates(cutoff: datetime):
    """Deleted, quiet, empty-looking rows: the cheap part of the rule, in SQL."""
    newer_seq = Message.objects.filter(channel=OuterRef("channel"), seq__gt=OuterRef("seq"))
    return (
        Message.objects.filter(deleted_at__lt=cutoff, ts_updated_at__lt=cutoff)
        .exclude(message_tombstone.has_text_q())
        .filter(Exists(newer_seq))
        .exclude(Exists(MessageAttachment.objects.filter(message=OuterRef("pk"))))
        .exclude(Exists(ReadCursor.objects.filter(last_read_message=OuterRef("pk"))))
    )


def _blocked_by_replies(message_ids: list, cutoff: datetime) -> set:
    """Roots with a reply that is live or was deleted inside the window."""
    return set(
        Message.objects.filter(thread_root_id__in=message_ids)
        .filter(
            Q(deleted_at__isnull=True) | Q(deleted_at__gte=cutoff) | Q(ts_updated_at__gte=cutoff)
        )
        .values_list("thread_root_id", flat=True)
    )


def prune(
    *, apply: bool = False, limit: int = DEFAULT_LIMIT, now: datetime | None = None
) -> PruneResult:
    """Count (default) or purge (`apply=True`) at most `limit` rows."""
    cutoff = (now or timezone.now()) - timedelta(days=MAX_DELTA_AGE_DAYS)
    result = PruneResult(dry_run=not apply)
    kept = {"live or recent reply": 0, "has text": 0}
    last_pk = None
    while result.matched < limit:
        qs = _candidates(cutoff).order_by("pk")
        if last_pk is not None:
            qs = qs.filter(pk__gt=last_pk)
        rows = list(qs.only("id", "body", "body_text", "metadata", "deleted_at")[:_SCAN])
        if not rows:
            break
        last_pk = rows[-1].pk
        ids = [m.pk for m in rows]
        blocked = _blocked_by_replies(ids, cutoff)
        kept["live or recent reply"] += len(blocked)
        # the cascade takes the thread: every descendant of a purged root goes too
        take = []
        for m in rows:
            if m.pk in blocked:
                continue
            if message_tombstone.has_text(m):  # metadata that is more than a tombstone keeps
                kept["has text"] += 1
                continue
            take.append(m.pk)
            if result.matched + len(take) >= limit:
                break
        result.matched += len(take)
        if apply and take:
            with transaction.atomic():
                activities = Activity.objects.filter(message_id__in=take)
                erase_notification_traces(activities, take)
                activities.delete()
                result.changed += Message.objects.filter(pk__in=take).count()
                Message.objects.filter(pk__in=take).delete()
    result.kept = kept
    return result
