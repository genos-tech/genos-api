"""
Channel-level REST endpoints for the unified messaging schema.

`/api/v3/channels/` — list user's channels (chat list).
`/api/v3/channels/{id}/` — single channel detail.

Message-level endpoints (delta sync, send, edit, react) live in
`message_views.py`. Channel creation (DM/GM/MDM-specific create flows)
will live here in a follow-up commit.

Permissions model: every read/write is scoped to channels the requesting
user is an active ChannelMember of. The `_get_channel_for_user` helper
both fetches the channel AND verifies membership in one indexed query —
if the user isn't a member, the response is 404 (not 403) so we don't
leak channel existence.
"""

from django.db.models import OuterRef, Subquery, Prefetch
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    Channel,
    ChannelMember,
    Message,
    ReadCursor,
)
from origin.serializers.chat.unified_serializers import (
    ChannelSerializer,
    ChannelMemberSerializer,
    MessageSerializer,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


def _user_channels_qs(user):
    """Active channels the given user is a member of, with related fields
    needed for serialization preloaded."""
    return (
        Channel.objects.filter(
            members__user=user,
            members__is_deleted=False,
            is_deleted=False,
        )
        .select_related("project", "team", "owner")
        .distinct()
    )


def _get_channel_for_user(channel_id, user):
    """Fetch one channel by id, scoped to the user's membership.

    Returns the Channel or raises Http404 — never 403, so we don't leak
    the existence of channels the user can't see.
    """
    try:
        return _user_channels_qs(user).get(id=channel_id)
    except Channel.DoesNotExist:
        raise Http404("Channel not found.")


def _annotate_latest_and_unread(qs, user):
    """Attach `_latest_message` and `_unread_count` to each channel in qs
    so ChannelSerializer can render them without per-row follow-up queries.

    `_latest_message` is set by a separate prefetch (cleaner than Subquery
    when we need the full Message object for serializer rendering).
    `_unread_count` is annotated via Subquery against ReadCursor +
    Message.seq.
    """
    # Subquery for the user's read cursor seq, per channel.
    cursor_seq = (
        ReadCursor.objects.filter(
            user=user,
            channel=OuterRef("pk"),
            thread_root__isnull=True,
        )
        .select_related("last_read_message")
        .values("last_read_message__seq")[:1]
    )

    # Subquery for "highest seq in this channel".
    latest_seq = (
        Message.objects.filter(
            channel=OuterRef("pk"),
            is_thread_reply=False,
            deleted_at__isnull=True,
        )
        .order_by("-seq")
        .values("seq")[:1]
    )

    qs = qs.annotate(
        _latest_seq=Subquery(latest_seq),
        _read_seq=Subquery(cursor_seq),
    )
    return qs


class ChannelListView(AuthenticatedAPIView):
    """GET /api/v3/channels/

    Returns the requesting user's chat list (all kinds: DM/GM/PM/MDM
    mixed). The client sorts by `latestMessage.tsSent` desc in-memory;
    the API returns them in deterministic id order for cache stability.

    Each row carries a denormalized `latestMessage` and `unreadCount` so
    the chat-list sidebar renders in a single round-trip.
    """

    def get(self, request):
        user = request.user
        qs = _annotate_latest_and_unread(_user_channels_qs(user), user)

        # Eager-load the latest non-thread message per channel for the
        # `latestMessage` serializer slot. We do a follow-up query keyed
        # by the annotated `_latest_seq` to avoid an N+1.
        channels = list(qs)
        if not channels:
            return Response({"channels": []})

        # Build a (channel_id, latest_seq) lookup, then fetch all the
        # corresponding Message rows in one query.
        latest_pairs = [(c.id, c._latest_seq) for c in channels if c._latest_seq is not None]
        if latest_pairs:
            from django.db.models import Q

            q = Q()
            for channel_id, seq in latest_pairs:
                q |= Q(channel_id=channel_id, seq=seq)
            latest_messages = (
                Message.objects.filter(q)
                .select_related("sender", "channel")
                .prefetch_related("reactions__user", "mentions", "attachments")
            )
            latest_by_channel = {m.channel_id: m for m in latest_messages}
        else:
            latest_by_channel = {}

        # Attach the latest message + compute unread count for the serializer.
        for c in channels:
            c._latest_message = latest_by_channel.get(c.id)
            if c._latest_seq is None:
                c._unread_count = 0
            elif c._read_seq is None:
                # Never read this channel — all non-thread messages count.
                # Cheap upper bound is (latest_seq - 0); for an exact count
                # we'd need a second query. Use the upper bound to avoid
                # the N+1; the FE shows "N+" if the count gets large.
                c._unread_count = c._latest_seq
            else:
                c._unread_count = max(0, c._latest_seq - c._read_seq)

        data = ChannelSerializer(channels, many=True, context={"request": request}).data
        return Response({"channels": data})


class ChannelDetailView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/

    Returns a single channel's metadata + member list. Used when the
    frontend opens a specific channel and needs the title/avatar/member
    list to render the header. Messages are fetched separately via the
    messages-delta endpoint.
    """

    def get(self, request, channel_id):
        channel = _get_channel_for_user(channel_id, request.user)

        # Attach the latest message + unread count for parity with list.
        qs = _annotate_latest_and_unread(Channel.objects.filter(id=channel.id), request.user)
        annotated = qs.first()
        if annotated and annotated._latest_seq is not None:
            latest = (
                Message.objects.filter(
                    channel_id=channel.id,
                    seq=annotated._latest_seq,
                )
                .select_related("sender", "channel")
                .prefetch_related("reactions__user", "mentions", "attachments")
                .first()
            )
            channel._latest_message = latest
            channel._unread_count = (
                annotated._latest_seq
                if annotated._read_seq is None
                else max(0, annotated._latest_seq - annotated._read_seq)
            )
        else:
            channel._latest_message = None
            channel._unread_count = 0

        members = ChannelMember.objects.filter(channel=channel, is_deleted=False).select_related(
            "user"
        )

        return Response(
            {
                "channel": ChannelSerializer(channel, context={"request": request}).data,
                "members": ChannelMemberSerializer(members, many=True).data,
            }
        )


class ChannelMembersView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/members/

    Returns the active member list for one channel. Distinct from
    `ChannelDetailView` so the client can refresh the member roster
    without re-fetching the latest-message slot. Member add/remove
    endpoints (POST/DELETE) will live in a follow-up commit because
    DM/GM/MDM have divergent join semantics (DM is a fixed pair, GM/MDM
    accept arbitrary additions).
    """

    def get(self, request, channel_id):
        # Membership check + 404 leak prevention via _get_channel_for_user.
        channel = _get_channel_for_user(channel_id, request.user)
        members = ChannelMember.objects.filter(channel=channel, is_deleted=False).select_related(
            "user"
        )
        return Response({"members": ChannelMemberSerializer(members, many=True).data})
