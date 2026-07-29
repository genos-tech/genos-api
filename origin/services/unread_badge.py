"""Total unread count for a user, for the installed-app icon badge.

Mirrors what the frontend puts on the mobile tab bar
(`unReadInboxItemCount + unReadChatAndActivityCounts`) so the number on
the home-screen icon and the ones inside the app agree.

Sent in the Web Push payload as `badge_count`, because the service
worker has no way to compute it while the app is closed — that window is
precisely when the badge matters. Best-effort: any failure returns None
and the client falls back to a local increment.
"""

import logging

from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce

from origin.models.chat.unified_models import ChannelMember, Message, ReadCursor
from origin.models.common.inbox_models import InboxItems

logger = logging.getLogger(__name__)


def unread_badge_total(user_id) -> int | None:
    """Unread chat messages + unread inbox items, or None if it can't be
    computed. Never raises — a badge is not worth failing a push over."""
    try:
        inbox = InboxItems.objects.filter(
            receiver_id=user_id, is_read=False, is_deleted=False
        ).count()

        # Per-channel unread beyond the user's main-timeline read cursor,
        # summed across their channels. Same shape as `_annotate_unread`
        # in channel_views, kept as one correlated subquery rather than a
        # per-channel loop.
        read_cursor_seq = ReadCursor.objects.filter(
            user_id=user_id,
            channel=OuterRef("channel"),
            thread_root__isnull=True,
        ).values("last_read_message__seq")[:1]

        unread_per_channel = (
            Message.objects.filter(
                channel=OuterRef("channel"),
                is_thread_reply=False,
                deleted_at__isnull=True,
                seq__gt=Coalesce(Subquery(read_cursor_seq), 0),
            )
            .exclude(sender_id=user_id)
            .order_by()
            .values("channel")
            .annotate(c=Count("id"))
            .values("c")[:1]
        )

        rows = (
            ChannelMember.objects.filter(user_id=user_id, is_deleted=False)
            .annotate(unread=Coalesce(Subquery(unread_per_channel), 0))
            .values_list("unread", flat=True)
        )
        chats = sum(rows)
        return int(inbox + chats)
    except Exception as exc:  # noqa: BLE001 — badge must never break push
        logger.warning("[webpush] badge count failed for %s: %s", user_id, exc)
        return None
