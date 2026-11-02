"""The number on the installed app's icon, for the team a push is about.

Sent in the Web Push payload as `badge_count`, because the service worker
has no way to compute it while the app is closed — that window is
precisely when the icon matters. With the app open the page overwrites it
from its own counts (`useAppBadge(unReadInboxItemCount +
unReadChatAndActivityCounts)` in `App.tsx`), so the two must count the
same things or the icon jumps every time the app opens or closes.

They did not. The server added up every unread inbox item of every team
(including the daily Genos digests and reminders the app files under
Activities) and every unread message of every chat of every team, so a
pile of unread digests held the icon at 121 while the open app said 0.
This module counts what the app counts, three terms, ONE team:

  * unread REQUESTS in the inbox: item types outside
    `ACTIVITY_INBOX_ITEM_TYPES` (`useInboxManagement`);
  * CHATS (DM, GM, PM; not MDM) with something new, one per chat, not per
    message (`countUnreadChats`, over the channel list the app builds from
    `ChannelListView`);
  * unread Activity feed TOPICS, one per topic, not per row
    (`countUnreadActivityTopics`, over the feed `ActivityListView` feeds).

The team is the one the push is about; the app shows the team the person
selected, which the server cannot know, so that is the closest it gets.
With no team (a caller that cannot name one) every term runs across all of
the person's teams: an upper bound that the app corrects when it opens.

A few queries however large the account (requests 1, chats 1 plus the
team's two share lookups, activity 1); no per-chat or per-row work.

Best-effort: any failure returns None and the client falls back to a
local increment.
"""

import logging
import uuid
from datetime import timedelta

from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    ChannelKind,
    Message,
    ReadCursor,
)
from origin.models.common.inbox_models import ACTIVITY_INBOX_ITEM_TYPES, InboxItems
from origin.services.channel_scope import team_scope_q, user_channels_qs

logger = logging.getLogger(__name__)

# The chat kinds the app adds up: `unReadChatCounts[1] + [2] + [3]`. A
# multi-person DM (MDM, 4) is in the chat list but not in the count.
_COUNTED_CHAT_KINDS = (ChannelKind.DM, ChannelKind.GM, ChannelKind.PM)

# The app keeps 30 days of the feed (`loadActivityHistory` asks for 30 days
# and `ACTIVITY_RETENTION_DAYS` prunes to it), so anything older never
# reaches its badge.
_ACTIVITY_WINDOW = timedelta(days=30)


def unread_badge_total(user_id, team_id=None) -> int | None:
    """What the app icon should say for `user_id` in `team_id`, or None if
    it can't be computed. Never raises — a badge is not worth failing a
    push over.

    `team_id` None counts every team of the user (see the module docstring);
    an unreadable one is treated as None.
    """
    try:
        team_id = _team_uuid(team_id)
        return int(
            _unread_requests(user_id, team_id)
            + _unread_chats(user_id, team_id)
            + _unread_activity_topics(user_id, team_id)
        )
    except Exception as exc:  # noqa: BLE001 — badge must never break push
        logger.warning("[webpush] badge count failed for %s: %s", user_id, exc)
        return None


def _team_uuid(team_id) -> str | None:
    if team_id is None or team_id == "":
        return None
    try:
        return str(uuid.UUID(str(team_id)))
    except (ValueError, AttributeError, TypeError):
        # A caller handed over something that is not a team. Count every
        # team (over-count) rather than none (an icon that goes dark).
        logger.warning("[webpush] badge: %r is not a team id; counting every team", team_id)
        return None


def _unread_requests(user_id, team_id) -> int:
    """Unread, live requests addressed to the user (`useInboxManagement`).

    A digest, a message reminder and a plain notice are things that
    happened, not things somebody is waiting on; the app lists them under
    Activities and keeps them out of this badge.
    """
    items = InboxItems.objects.filter(receiver_id=user_id, is_read=False, is_deleted=False).exclude(
        item_type__in=ACTIVITY_INBOX_ITEM_TYPES
    )
    if team_id:
        items = items.filter(team_id=team_id)
    return items.count()


def _unread_chats(user_id, team_id) -> int:
    """DM/GM/PM chats the app would show as having something new.

    The chat list is `ChannelListView`: the user's live memberships,
    narrowed to the team by `team_scope_q` (so a chat another team shared
    into this one counts here, as it is listed here). A chat is new when it
    has a top-level, undeleted message beyond the user's main-timeline read
    cursor AND its newest such message is not the user's own: the app treats
    a chat whose last word is yours as read even when the cursor lags
    (`channelToLegacyChat`'s `latestIsSelfSent`). Thread replies never make a
    chat new.

    One query, no loop over chats: both conditions are correlated
    subqueries on the (channel, seq) index.
    """
    # NB the cursor lookup correlates to the MESSAGE it is nested in
    # (`OuterRef("channel")`), the messages to the CHANNEL (`OuterRef("pk")`).
    read_seq = ReadCursor.objects.filter(
        user_id=user_id, channel=OuterRef("channel"), thread_root__isnull=True
    ).values("last_read_message__seq")[:1]
    top_level = Message.objects.filter(
        channel=OuterRef("pk"), is_thread_reply=False, deleted_at__isnull=True
    )
    beyond_cursor = top_level.filter(seq__gt=Coalesce(Subquery(read_seq), 0))
    newest_sender = top_level.order_by("-seq").values("sender_id")[:1]

    chats = user_channels_qs(user_id).filter(kind__in=_COUNTED_CHAT_KINDS)
    if team_id:
        chats = chats.filter(team_scope_q(team_id, user_id))
    return (
        chats.annotate(_newest_sender=Subquery(newest_sender))
        .filter(Exists(beyond_cursor))
        # A sender who has since been deleted is nobody's own message.
        .filter(Q(_newest_sender__isnull=True) | ~Q(_newest_sender=user_id))
        .order_by()
        .count()
    )


def _unread_activity_topics(user_id, team_id) -> int:
    """Unread Activity feed topics (`countUnreadActivityTopics`).

    The app collapses a burst on one topic into one feed row and counts
    rows, so 100 unread replies in a thread are 1. Which rows it holds, and
    how it groups them, is replicated here:

      * the user's own activities of the team, last 30 days;
      * a reaction only counts for the author of the message reacted to
        (`popActivityMessages`);
      * the synthetic "first message of a thread" is dropped;
      * every unread row of a topic is one bucket whatever its age, so the
        count is the number of distinct topics with an unread row.
    """
    rows = Activity.objects.filter(
        recipient_id=user_id,
        is_read=False,
        ts_created_at__gte=timezone.now() - _ACTIVITY_WINDOW,
    )
    if team_id:
        rows = rows.filter(team_id=team_id)
    rows = (
        rows.filter(~Q(activity_type=ActivityType.REACTION) | Q(message__sender_id=user_id))
        .exclude(message__is_thread_reply=True, message__seq=1)
        .order_by()
        .values_list(*_TOPIC_COLUMNS)
    )
    return len({_topic_key(*row) for row in rows})


# What `_topic_key` reads, in order.
_TOPIC_COLUMNS = (
    "activity_type",
    "surface_type",
    "channel_id",
    "channel__kind",
    "message_id",
    "message__parent_id",
    "message__is_thread_reply",
    "message__task_id",
    "message__metadata__taskCommentId",
    "meta__noteId",
    "meta__taskId",
)


def _json_number(value) -> int | float:
    """`num()` in `v3ActivityToLegacy`: a JSON number, else 0."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _topic_key(
    activity_type,
    surface_type,
    channel_id,
    channel_kind,
    message_id,
    parent_id,
    is_thread_reply,
    message_task_id,
    task_comment_id,
    note_id,
    task_id,
):
    """`activityTopicKey` (`activityAggregation.ts`) for one activity row.

    Which rows the app treats as "the same topic": `activity_type` is
    always part of it, then the surface the activity lives on — the note,
    the task body, the task (for its comments), the reacted-to message, the
    thread, or the channel, in that order of precedence. Keep the two in
    step; `test_unread_badge` checks this against the feed the app is given.
    """
    t = activity_type
    if surface_type is not None:
        # A channel-less mention of a note (6 personal, 7 task, 8 chat) or a
        # task body (5). Anything else falls through to the app's last
        # rule, keyed by whatever `chatId` it would have been given.
        if 6 <= surface_type <= 8:
            return (t, "note", surface_type, _json_number(note_id))
        if surface_type == 5:
            return (t, "taskbody", _json_number(task_id))
        return (t, "channel", _json_number(note_id))
    # A task comment is a PM thread reply carrying `taskCommentId`; the
    # `kind == MDM and has a task` shape is the pre-v3 fallback. Keyed by
    # the task, not the comment or its thread.
    if bool(task_comment_id) or (channel_kind == ChannelKind.MDM and bool(message_task_id)):
        return (t, "taskcomment", message_task_id or 0)
    if t == ActivityType.REACTION:
        return (t, "reaction", channel_id, message_id)
    if is_thread_reply:
        return (t, "thread", channel_id, parent_id or "")
    return (t, "channel", channel_id)
