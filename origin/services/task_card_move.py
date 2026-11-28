"""A task moved to another project takes its chat card and comment thread along (MIP-2505).

The card is a message in the old project's PM channel and the task's comments are replies under it.
The card refresh and the comment mirror look only in the NEW project's chat, so a card left behind
was never updated again, and the next comment started a second card.

The decision: both move. The card and every reply go to the end of the new project's PM channel (new
`seq`, under the same per-channel lock a send takes), keep their ids (the comment mirror's ids are
derived from the task and comment), and the old chat keeps nothing. The comments belong to the task,
and the new project's members already read them on the task page. A stub left behind would tell the
old members where the task went.

What hangs off the moved messages follows them: reactions and attachments with the row; the chat
notes on the thread; pasted inline images, which become readable by the new chat's people
(`moved_messages`, which also tells offline devices to drop their copy); activities, flags,
reminders, `@` rows and thread read cursors only for people in the new chat. The main read cursors
of the old chat step back to what is still there, and the cached thread summary and the thread's
search chunks are dropped.

When the new chat already holds a card of the task (it was moved before this change and a comment
made a second one), the replies join that card and the surplus card is removed: one card per task.

With no PM channel in the new project the card stays where it is. Best effort, like the card itself:
a failure is logged and never costs the move.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import partial

from django.db import transaction
from django.db.models import Case, F, Max, Q, Value, When
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    Channel,
    ChannelKind,
    Flag,
    Message,
    MessageMention,
    MessagePin,
    MessageReminder,
    ReadCursor,
)
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.acl_dirty import mark_channels_dirty
from origin.services import (
    moved_messages,
    realtime_push,
    task_card_post,
    thread_summary_cleanup,
)
from origin.services.chat_note_access import rederive_on_move
from origin.views.utils.note_role import NOTE_TYPE_CHAT, ROLE_OWNER

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
    # Rows locked: what a body shows is recorded as it leaves, so no edit may slip in between.
    cards = cards_elsewhere().select_for_update(of=("self",)).select_related("channel")
    for card in cards.order_by("ts_sent_at", "seq"):
        by_task.setdefault(card.task_id, []).append(card)

    members = ProjectMembers.objects.filter(
        project_id=destination.project_id, attendee_id__isnull=False
    ).values("attendee_id")
    next_seq = (Message.objects.filter(channel=destination).aggregate(m=Max("seq"))["m"] or 0) + 1

    # What each task's cards do is worked out first, then written for all of them together, so
    # the queries do not grow with the number of cards (MIP-2539).
    existing: dict = {}
    for message in Message.objects.filter(
        channel=destination, task_id__in=list(by_task), is_thread_reply=False
    ).order_by(F("deleted_at").asc(nulls_first=True), "seq"):
        existing.setdefault(message.task_id, message)
    replies: dict = {}
    for reply in (
        Message.objects.select_for_update()
        .filter(thread_root_id__in=[c.pk for cards in by_task.values() for c in cards])
        .order_by("seq")
    ):
        replies.setdefault(reply.thread_root_id, []).append(reply)

    plan = _Plan(destination=destination, next_seq=next_seq)
    live_budget = LIVE_REPLY_LIMIT
    for task_id, cards in by_task.items():
        live_budget -= _move_one(task_id, cards, existing.get(task_id), replies, plan, live_budget)
    _write(plan, members)

    from origin.search_engine.acl_dirty import mark_projects_dirty  # noqa: PLC0415

    # The old chats are re-chunked whole, so their main timeline no longer says what left.
    mark_projects_dirty(
        Channel.objects.filter(pk__in=sources, project_id__isnull=False).values_list(
            "project_id", flat=True
        )
    )


@dataclass
class _Plan:
    """The writes of one move into `destination`, collected over its cards."""

    destination: Channel
    next_seq: int
    #: Source channel -> the cards and replies leaving it.
    leaving: dict = field(default_factory=dict)
    #: The rows to update, as bare instances carrying the new values.
    updates: list = field(default_factory=list)
    #: The cards that become their task's card in the destination.
    new_cards: list = field(default_factory=list)
    #: `(card, target)` of each card whose replies join another card: the card is deleted.
    merged: list = field(default_factory=list)
    #: `(source, old root, new root)` of each thread.
    threads: list = field(default_factory=list)
    #: `(task id, card id, reply ids)` to show once committed.
    announce: list = field(default_factory=list)


def _move_one(task_id, cards, target, replies, plan, live_budget) -> int:
    """One task's cards and threads into the destination, added to `plan`. Returns how many
    replies will be pushed live."""
    # A live card first, so a deleted one never becomes the task's card.
    cards = sorted(cards, key=lambda c: c.deleted_at is not None)
    arrived: list = []
    for card in cards:
        source = card.channel
        thread = replies.get(card.pk, [])
        becomes_card = target is None
        moving = ([card] if becomes_card else []) + thread
        # The surplus card of a merge leaves too: it is deleted in `_write`.
        plan.leaving.setdefault(source, []).extend([card, *thread])

        for message in moving:
            # A copy, so the instances above keep the old values the removal push reads.
            row = Message(
                pk=message.pk,
                channel=plan.destination,
                seq=plan.next_seq,
                ts_updated_at=timezone.now(),
                thread_root_id=message.thread_root_id,
                parent_id=message.parent_id,
            )
            if target is not None:
                row.thread_root_id = target.pk
                if message.parent_id == card.pk:
                    row.parent_id = target.pk
            plan.updates.append(row)
            arrived.append(message.pk)
            plan.next_seq += 1

        if becomes_card:
            target = card
            plan.new_cards.append(card)
        else:
            plan.merged.append((card, target))
        plan.threads.append((source, card.pk, target.pk))
    card_id = target.pk
    reply_ids = [pk for pk in arrived if pk != card_id]
    reply_ids = reply_ids[-live_budget:] if live_budget > 0 else []
    plan.announce.append((task_id, card_id, reply_ids))
    return len(reply_ids)


def _write(plan, members) -> None:
    destination = plan.destination
    for source, messages in plan.leaving.items():
        card_ids = [m.pk for m in messages if not m.is_thread_reply]
        _step_back_read_cursors(source, messages)
        thread_summary_cleanup.forget_threads(channel_id=source.pk, thread_ids=card_ids)
        moved_messages.record_moved_away(source.pk, messages)
        # A pin belongs to the chat it was made in (MIP-2487).
        MessagePin.objects.filter(message_id__in=[m.pk for m in messages]).delete()
        _purge_thread_chunks(source.pk, card_ids)

    Message.objects.bulk_update(
        plan.updates, ["channel", "seq", "ts_updated_at", "thread_root", "parent"]
    )
    _carry_satellites([m.pk for m in plan.updates], destination, members)

    if plan.new_cards:
        threads = ReadCursor.objects.filter(thread_root_id__in=[c.pk for c in plan.new_cards])
        threads.exclude(user_id__in=members).delete()
        threads.update(channel=destination)
    for card, target in plan.merged:
        Message.objects.filter(pk=target.pk).update(
            reply_count=F("reply_count") + card.reply_count, ts_updated_at=timezone.now()
        )
    thread_summary_cleanup.forget_threads(
        channel_id=destination.pk, thread_ids=[target.pk for _card, target in plan.merged]
    )

    noted = set(
        ChatNoteMaster.objects.filter(
            channel__in=list(plan.leaving),
            thread_root_id__in=[old for _source, old, _new in plan.threads],
        ).values_list("channel_id", "thread_root_id")
    )
    for source, old_root, new_root in plan.threads:
        if (source.pk, old_root) in noted:
            _move_thread_notes(source, old_root, destination, new_root)

    # A task made from a message of one of these threads points at it (`thread_link`).
    for source in plan.leaving:
        roots = {str(old): str(new) for src, old, new in plan.threads if src == source}
        TaskMaster.objects.filter(chat_id=str(source.pk), thread_id__in=list(roots)).update(
            chat_id=str(destination.pk),
            thread_id=Case(*[When(thread_id=old, then=Value(new)) for old, new in roots.items()]),
            ts_updated_at=timezone.now(),
        )

    if plan.merged:
        # Not `card.delete()`: that clears the instance's id, which the removal push needs.
        Message.objects.filter(pk__in=[card.pk for card, _target in plan.merged]).delete()

    for source, messages in plan.leaving.items():
        for start in range(0, len(messages), REMOVAL_BATCH):
            realtime_push.push_messages_deleted(source, messages[start : start + REMOVAL_BATCH])
    # After the removals above (callbacks run in order): someone in both chats sees the rows leave
    # the old one and appear in the new one, never the other way round.
    for task_id, card_id, reply_ids in plan.announce:
        transaction.on_commit(partial(_announce, task_id, card_id, reply_ids), robust=True)


def _move_thread_notes(source, old_root, destination, new_root) -> None:
    """The chat notes written on the thread go with it, sub-notes included: the new chat's people
    read them, and the roles the old chat's membership wrote end (as when a note is moved by hand,
    `ChatNoteMoveView`)."""
    on_source = ChatNoteMaster.objects.filter(channel=source)
    notes = {n.note_id: n for n in on_source.filter(thread_root_id=old_root)}
    frontier = set(notes)
    while frontier:
        # A sub-note filed on another thread stays with that thread.
        children = on_source.filter(
            Q(thread_root_id__isnull=True) | Q(thread_root_id=old_root),
            parent_note_id__in=frontier,
        ).exclude(note_id__in=notes)
        found = {n.note_id: n for n in children}
        notes.update(found)
        frontier = set(found)
    if not notes:
        return
    moves = [(n.note_id, source, n.owner_id) for n in notes.values()]
    fields = {
        "channel": destination,
        "chat_type": destination.kind,
        "team_id": destination.team_id,
        # The stamp is what makes the reindex give the notes their new readers.
        "ts_updated_at": timezone.now(),
    }
    moving = ChatNoteMaster.objects.filter(note_id__in=notes)
    moving.filter(thread_root_id=old_root).update(thread_root_id=new_root, **fields)
    moving.exclude(thread_root_id=new_root).update(**fields)
    # No sub-note link may cross the two chats.
    moving.exclude(parent_note_id__in=notes).update(parent_note_id=None)
    ChatNoteMaster.objects.filter(channel=source, parent_note_id__in=notes).update(
        parent_note_id=None
    )
    rederive_on_move(moves=moves, target=destination)
    # The author keeps no row on a note in a chat they are not in; a later move that brings it to
    # one they are in writes it again, or a move back would leave them without their own note
    # (as `restore_task_roles_on_move` does for task notes).
    in_destination = set(
        ProjectMembers.objects.filter(
            project_id=destination.project_id,
            attendee_id__in=[n.owner_id for n in notes.values() if n.owner_id],
        ).values_list("attendee_id", flat=True)
    )
    for note in notes.values():
        if note.owner_id in in_destination:
            NotePermissionMaster.objects.get_or_create(
                user_id=note.owner_id,
                note_type=NOTE_TYPE_CHAT,
                note_id=note.note_id,
                defaults={"role_id": ROLE_OWNER, "team_id": destination.team_id},
            )
    mark_channels_dirty([source.pk, destination.pk])


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


def _purge_thread_chunks(channel_id, root_ids) -> None:
    """The old chat's search entries for these threads: nothing re-emits them once the rows have
    left."""
    from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL, chat_entity_id  # noqa: PLC0415
    from origin.search_engine.purge import purge_entities  # noqa: PLC0415

    label = CHAT_TYPE_LABEL[int(ChannelKind.PM)]
    pairs = [("chat", chat_entity_id(label, str(channel_id), str(root))) for root in root_ids]
    transaction.on_commit(lambda: purge_entities(pairs), robust=True)


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
