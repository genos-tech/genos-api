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
This module counts the two terms of the app's number that have simple rules, for ONE team:

  * unread REQUESTS in the inbox: item types outside
    `ACTIVITY_INBOX_ITEM_TYPES` (`useInboxManagement`);
  * CHATS (DM, GM, PM; not MDM) with something new, one per chat, not per
    message (`countUnreadChats`, over the channel list the app builds from
    `ChannelListView`).

NOT counted: the app also adds its Activity feed (`unReadActivityMessageCounts`),
whose rows it groups by topic with rules that live in the frontend. The
server does not copy those rules (MIP-929): a second copy would drift from
the app and is logic for one client. So while the app is closed the icon can
read lower than the app does by the number of unread Activity topics, and the
app writes its own number when it comes back to the foreground (`useAppBadge`).

The team is the one the push is about; the app shows the team the person
selected, which the server cannot know, so that is the closest it gets.
With no team (a caller that cannot name one) every term runs across all of
the person's teams: an upper bound that the app corrects when it opens.

A few queries however large the account (requests 1, chats 1 plus the
team's two share lookups); no per-chat or per-row work.

Best-effort: any failure returns None and the client falls back to a
local increment.
"""

import logging
import uuid

from django.db.models import Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce

from origin.models.chat.unified_models import ChannelKind, Message, ReadCursor
from origin.models.common.inbox_models import ACTIVITY_INBOX_ITEM_TYPES, InboxItems
from origin.services.channel_scope import team_scope_q, user_channels_qs

logger = logging.getLogger(__name__)

# The chat kinds the app adds up: `unReadChatCounts[1] + [2] + [3]`. A
# multi-person DM (MDM, 4) is in the chat list but not in the count.
_COUNTED_CHAT_KINDS = (ChannelKind.DM, ChannelKind.GM, ChannelKind.PM)


def unread_badge_total(user_id, team_id=None) -> int | None:
    """What the app icon should say for `user_id` in `team_id`, or None if
    it can't be computed. Never raises — a badge is not worth failing a
    push over.

    `team_id` None counts every team of the user (see the module docstring);
    an unreadable one is treated as None.
    """
    try:
        team_id = _team_uuid(team_id)
        return int(_unread_requests(user_id, team_id) + _unread_chats(user_id, team_id))
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
