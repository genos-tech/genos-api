"""A task moved to another project takes its chat card and comment thread along (MIP-2505).

The card is a message in the old project's PM channel and the task's comments are replies under it.
The card refresh and the comment mirror look only in the NEW project's chat, so a card left behind
was never updated again, and the next comment started a second card.

The decision: both move. The card and every reply go to the end of the new project's PM channel (new
`seq`, under the same per-channel lock a send takes), keep their ids (the comment mirror's ids are
derived from the task and comment), and the old chat keeps nothing. The comments belong to the task,
and the new project's members already read them on the task page. A stub left behind would tell the
old members where the task went.

What hangs off the moved messages follows them: reactions and attachments with the row, activities,
flags, reminders, `@` rows and thread read cursors only for people in the new chat, the main read
cursors of the old chat step back to what is still there, the cached thread summary and the thread's
search chunks are dropped.

When the new chat already holds a card of the task (it was moved before this change and a comment
made a second one), the replies join that card and the surplus card is removed: one card per task.

With no PM channel in the new project the card stays where it is. Best effort, like the card itself:
a failure is logged and never costs the move.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import F, Max
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    Channel,
    ChannelKind,
    Flag,
    Message,
    MessageMention,
    MessageReminder,
    ReadCursor,
)
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import realtime_push, task_card_post, thread_summary_cleanup

log = logging.getLogger(__name__)

#: Replies pushed one by one to the open new chat, per move: each push is a request of its own made
#: while the person waits. The rest arrive with that chat's next sync (their stamp is new).
LIVE_REPLY_LIMIT = 20
#: Message ids in one "these are gone" push to the old chat.
REMOVAL_BATCH = 200


def move_task_cards(task_ids, *, project_id) -> None:
    """Move the cards (and their threads) of `task_ids` into the PM channel of `project_id`.

    Call after the task rows carry their new project. Never raises.
    """
    try:
        with transaction.atomic():
            _move(set(task_ids), int(project_id))
    except Exception:  # noqa: BLE001 — the task move stands; the card is an observer of it
        log.warning(
            "task card move failed tasks=%s project=%s",
            sorted(task_ids),
            project_id,
            exc_info=True,
        )


def _move(task_ids: set, project_id: int) -> None:
    def cards_elsewhere():
        return Message.objects.filter(
            task_id__in=task_ids, is_thread_reply=False, channel__kind=ChannelKind.PM
        ).exclude(channel__legacy_chat_id=project_id)

    sources = set(cards_elsewhere().values_list("channel_id", flat=True))
    if not sources:
        return
    destination = Channel.objects.filter(kind=ChannelKind.PM, legacy_chat_id=project_id).first()
    if destination is None:
        log.warning(
            "task card move: project %s has no PM channel; the cards of tasks %s stay",
            project_id,
            sorted(task_ids),
        )
        return

    # Both ends locked as a send locks one (`message_views`), in id order so two moves crossing
    # each other cannot deadlock.
    list(
        Channel.objects.select_for_update().filter(pk__in=sources | {destination.pk}).order_by("pk")
    )

    by_task: dict[int, list[Message]] = {}
    for card in cards_elsewhere().select_related("channel").order_by("ts_sent_at", "seq"):
        by_task.setdefault(card.task_id, []).append(card)

    members = ProjectMembers.objects.filter(
        project_id=destination.project_id, attendee_id__isnull=False
    ).values("attendee_id")
    next_seq = (Message.objects.filter(channel=destination).aggregate(m=Max("seq"))["m"] or 0) + 1

    live_budget = LIVE_REPLY_LIMIT
    for task_id, cards in by_task.items():
        next_seq, shown = _move_one(task_id, cards, destination, members, next_seq, live_budget)
        live_budget -= shown

    from origin.search_engine.acl_dirty import mark_projects_dirty  # noqa: PLC0415

    # The old chats are re-chunked whole, so their main timeline no longer says what left.
    mark_projects_dirty(
        Channel.objects.filter(pk__in=sources, project_id__isnull=False).values_list(
            "project_id", flat=True
        )
    )


def _move_one(task_id, cards, destination, members, next_seq, live_budget) -> tuple[int, int]:
    """One task's cards and threads into `destination`. Returns the next free `seq` there and how
    many replies will be pushed live."""
    target = (
        Message.objects.filter(channel=destination, task_id=task_id, is_thread_reply=False)
        .order_by(F("deleted_at").asc(nulls_first=True), "seq")
        .first()
    )
    # A live card first, so a deleted one never becomes the task's card.
    cards = sorted(cards, key=lambda c: c.deleted_at is not None)
    gone: dict[Channel, list[Message]] = {}
    arrived: list = []
    for card in cards:
        source = card.channel
        replies = list(Message.objects.filter(thread_root_id=card.pk).order_by("seq"))
        becomes_card = target is None
        moving = ([card] if becomes_card else []) + replies
        _step_back_read_cursors(source, [card, *replies])
        thread_summary_cleanup.forget_thread(channel_id=source.pk, thread_id=card.pk)
        _purge_thread_chunks(source.pk, card.pk)

        for message in moving:
            fields = {"channel": destination, "seq": next_seq, "ts_updated_at": timezone.now()}
            if target is not None:
                fields["thread_root_id"] = target.pk
                if message.parent_id == card.pk:
                    fields["parent_id"] = target.pk
            Message.objects.filter(pk=message.pk).update(**fields)
            arrived.append(message.pk)
            next_seq += 1
        _carry_satellites([m.pk for m in moving], destination, members)

        if becomes_card:
            target = card
            threads = ReadCursor.objects.filter(thread_root_id=card.pk)
            threads.exclude(user_id__in=members).delete()
            threads.update(channel=destination)
        else:
            Message.objects.filter(pk=target.pk).update(
                reply_count=F("reply_count") + card.reply_count, ts_updated_at=timezone.now()
            )
            thread_summary_cleanup.forget_thread(channel_id=destination.pk, thread_id=target.pk)
        # A task made from a message of this thread points at it (`thread_link`).
        TaskMaster.objects.filter(chat_id=str(source.pk), thread_id=str(card.pk)).update(
            chat_id=str(destination.pk), thread_id=str(target.pk), ts_updated_at=timezone.now()
        )
        gone.setdefault(source, []).extend([card, *replies])
        if target.pk != card.pk:
            # Not `card.delete()`: that clears the instance's id, which the removal push needs.
            Message.objects.filter(pk=card.pk).delete()

    for source, messages in gone.items():
        for start in range(0, len(messages), REMOVAL_BATCH):
            realtime_push.push_messages_deleted(source, messages[start : start + REMOVAL_BATCH])
    card_id = target.pk
    reply_ids = [pk for pk in arrived if pk != card_id]
    reply_ids = reply_ids[-live_budget:] if live_budget > 0 else []
    # After the removals above (callbacks run in order): someone in both chats sees the rows leave
    # the old one and appear in the new one, never the other way round.
    transaction.on_commit(lambda: _announce(task_id, card_id, reply_ids), robust=True)
    return next_seq, len(reply_ids)


def _step_back_read_cursors(source, leaving) -> None:
    """A main-timeline cursor of the old chat that points at a row about to leave moves to the
    newest top-level row before it that stays, so the unread count there stays right."""
    ids = [m.pk for m in leaving]
    seqs = {m.pk: m.seq for m in leaving}
    cursors = ReadCursor.objects.filter(
        channel=source, thread_root__isnull=True, last_read_message_id__in=ids
    )
    for cursor in cursors:
        previous = (
            Message.objects.filter(
                channel=source,
                is_thread_reply=False,
                seq__lt=seqs[cursor.last_read_message_id],
            )
            .exclude(pk__in=ids)
            .order_by("-seq")
            .first()
        )
        ReadCursor.objects.filter(pk=cursor.pk).update(
            last_read_message=previous, ts_updated_at=timezone.now()
        )


def _carry_satellites(message_ids, destination, members) -> None:
    """What people hold on the moved rows stays only with people of the new chat; their activity
    rows now name the new chat (and its team), or the deep link would open the old one."""
    activities = Activity.objects.filter(message_id__in=message_ids)
    activities.exclude(recipient_id__in=members).delete()
    activities.update(channel=destination, team_id=destination.team_id)
    Flag.objects.filter(message_id__in=message_ids).exclude(user_id__in=members).delete()
    MessageReminder.objects.filter(message_id__in=message_ids).exclude(user_id__in=members).delete()
    MessageMention.objects.filter(message_id__in=message_ids).exclude(
        mentioned_user_id__in=members
    ).delete()


def _purge_thread_chunks(channel_id, root_id) -> None:
    """The old chat's search entry for this thread: nothing re-emits it once the rows have left."""
    from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL, chat_entity_id  # noqa: PLC0415
    from origin.search_engine.purge import purge_entities  # noqa: PLC0415

    entity = chat_entity_id(CHAT_TYPE_LABEL[int(ChannelKind.PM)], str(channel_id), str(root_id))
    transaction.on_commit(lambda: purge_entities([("chat", entity)]), robust=True)


def _announce(task_id, card_id, reply_ids) -> None:
    """Rebuild the card from the task as it is now and show it, with its thread, to the open new
    chat. `UPDATED`, not `CREATED`: nothing here is a new message to be notified about."""
    task_card_post.refresh_task_card(task_id)
    card = Message.objects.filter(pk=card_id).first()
    if card is None:
        return
    realtime_push.push_message(card, realtime_push.UPDATED)
    for reply in Message.objects.filter(pk__in=reply_ids).order_by("seq"):
        realtime_push.push_message(reply, realtime_push.UPDATED)
