"""Activity-feed producer for the unified messaging schema.

Called from `message_views.MessagesView.post` and
`reaction_views_v3.MessageReactionsView.post` after the underlying row
(Message / MessageReaction) is committed. Writes `Activity` rows for
every user who should see a sidebar entry — direct @-mentions on a new
message, the message sender when someone reacts to it, and the thread
root's sender on a thread reply.

Returned `Activity` rows are picked up by the WS layer
(`socketio_events_v3/message_handlers` and `reaction_handlers`) and
broadcast as `activity.created` to each recipient's `user:{id}` room
so their sidebar updates without a refresh.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    Message,
)
from origin.models.common.user_models import CustomUser


def _team_id_for_channel(channel: Channel):
    """`Activity.team` mirrors the channel's team. Cheap accessor that
    works whether `channel` was prefetched via select_related or not."""
    return channel.team_id


def create_mention_activities(
    *, message: Message, mentioned_user_ids: Iterable[str], actor: CustomUser
) -> List[Activity]:
    """One `Activity(type=MENTION)` per directly @-mentioned user.

    Skips:
      - the actor themselves (mentioning yourself doesn't ping)
      - empty / falsy ids
      - users that don't exist (silent skip — the mention row was already
        rejected at the MessageMention.bulk_create boundary if the id
        wasn't valid).
    """
    actor_id = str(actor.id)
    targets = [str(uid) for uid in mentioned_user_ids if uid and str(uid) != actor_id]
    if not targets:
        return []
    channel = message.channel
    rows = [
        Activity(
            team_id=_team_id_for_channel(channel),
            recipient_id=uid,
            actor=actor,
            activity_type=ActivityType.MENTION,
            channel=channel,
            message=message,
            meta={},
        )
        for uid in targets
    ]
    Activity.objects.bulk_create(rows)
    return rows


def create_thread_reply_activity(
    *, reply: Message, parent: Optional[Message], actor: CustomUser
) -> List[Activity]:
    """Single `Activity(type=THREAD_REPLY)` for the thread root's sender
    when a different user posts a reply.

    Returns `[]` when:
      - `parent` is null (the reply has no parent — shouldn't happen
        for a thread reply, but defend against the caller)
      - the parent's sender is the same user posting the reply
      - the parent's sender was hard-deleted (sender FK is null)
    """
    if parent is None or parent.sender_id is None:
        return []
    actor_id = str(actor.id)
    parent_sender_id = str(parent.sender_id)
    if parent_sender_id == actor_id:
        return []
    channel = reply.channel
    row = Activity.objects.create(
        team_id=_team_id_for_channel(channel),
        recipient_id=parent_sender_id,
        actor=actor,
        activity_type=ActivityType.THREAD_REPLY,
        channel=channel,
        message=reply,
        meta={"parent_message_id": str(parent.id)},
    )
    return [row]


def create_reaction_activity(*, message: Message, emoji: str, actor: CustomUser) -> List[Activity]:
    """Single `Activity(type=REACTION)` for the message sender when
    someone else reacts. Self-reactions produce no row."""
    if message.sender_id is None:
        return []
    actor_id = str(actor.id)
    sender_id = str(message.sender_id)
    if sender_id == actor_id:
        return []
    channel = message.channel
    row = Activity.objects.create(
        team_id=_team_id_for_channel(channel),
        recipient_id=sender_id,
        actor=actor,
        activity_type=ActivityType.REACTION,
        channel=channel,
        message=message,
        meta={"emoji": emoji},
    )
    return [row]
