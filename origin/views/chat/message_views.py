"""
Message-level REST endpoints for the unified messaging schema.

`/api/v3/channels/{channel_id}/messages/?since=ISO` — delta sync, top-level messages.
`/api/v3/channels/{channel_id}/threads/?since=ISO` — delta sync, thread replies.
`/api/v3/messages/{message_id}/` — single message detail.

Send/edit/delete and reaction add/remove will live here in a follow-up
commit — those are intentionally paired with the unified SocketIO
handler rewrite so the REST + WS contracts ship together (the WS layer
proxies to the REST layer; see plan §3 in the plan file).

Delta envelope shape (matches `serializers.DeltaEnvelopeSerializer`):
    {
      "server_time": "<iso>",
      "force_full_reload": <bool>,        // omitted when false
      "data": {
        "messages":           [<MessageSerializer>, ...],
        "deletes":            ["<message_uuid>", ...],
        "reactions_changed":  [{ "message_id": "...", "reactions": [...] }],
      }
    }

The frontend persists `server_time` as the next `?since=` value. Soft-deleted
messages come back in `deletes` (the array of UUIDs that disappeared since
the checkpoint) so the client can apply tombstones without parsing every
message row.
"""

from django.http import Http404
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    Channel,
    ChannelMember,
    Message,
    MessageReaction,
)
from origin.serializers.chat.unified_serializers import (
    MessageReactionSerializer,
    MessageSerializer,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.incremental import (
    build_delta_response,
    capture_server_time,
    check_since,
)


def _apply_message_since_filter(qs, since):
    """Like `apply_since_filter` but adapted for the new schema's
    `deleted_at` timestamp (vs the legacy `is_deleted` boolean).

    - Full load (since=None): exclude deleted rows entirely (`deleted_at
      IS NULL`).
    - Incremental (since=<ts>): rows whose ts_updated_at > since,
      INCLUDING soft-deleted ones (so the client can apply tombstones
      via the row's `deletedAt` field).
    """
    if since is None:
        return qs.filter(deleted_at__isnull=True)
    return qs.filter(ts_updated_at__gt=since)


def _verify_member_or_404(channel_id, user):
    """Return Channel iff the user is an active member; else raise 404.

    404-not-403 so we don't leak channel existence to non-members. The
    same pattern is used in `channel_views._get_channel_for_user`; this
    helper is duplicated rather than imported to keep the two view files
    independently deletable when one of them is rewritten later.
    """
    try:
        channel = Channel.objects.get(id=channel_id, is_deleted=False)
    except Channel.DoesNotExist:
        raise Http404("Channel not found.")
    is_member = ChannelMember.objects.filter(channel=channel, user=user, is_deleted=False).exists()
    if not is_member:
        raise Http404("Channel not found.")
    return channel


def _prefetched_messages(qs):
    """Apply the prefetch set every messages serializer needs.

    Centralized so the delta endpoint and the single-message endpoint
    produce identical wire shapes — the legacy code's serialize divergence
    between `*SingleMessageView` and `*MessagesDeltaView` is exactly the
    class of bug this rewrite eliminates.
    """
    return qs.select_related("sender", "channel").prefetch_related(
        "reactions__user",
        "mentions",
        "attachments",
    )


class MessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/messages/?since=ISO

    Incremental sync of top-level messages (NOT thread replies — those
    have their own endpoint). The contract:

    - First load (no `since`): all non-deleted top-level messages.
    - Incremental (`since=<ts>`): rows updated since `since`, INCLUDING
      soft-deleted rows (so the client can apply tombstones). Plus any
      messages whose reactions changed (so the reaction chip refreshes
      without the client re-fetching the whole row).
    - Catastrophic delta (`since` older than 60 days): respond as a full
      load AND set `force_full_reload=true` so the client clears its
      IDB store before applying.
    """

    def get(self, request, channel_id):
        channel = _verify_member_or_404(channel_id, request.user)
        server_time = capture_server_time()
        since, force_full = check_since(request)

        qs = Message.objects.filter(channel=channel, is_thread_reply=False)
        qs = _apply_message_since_filter(qs, since)

        # Indirect change: messages whose reactions changed since checkpoint
        # need to be re-served so the chip count refreshes. Compute the
        # set of message ids with recent reaction activity and union them
        # in.
        if since is not None and not force_full:
            recent_reaction_msg_ids = set(
                MessageReaction.objects.filter(
                    message__channel=channel,
                    message__is_thread_reply=False,
                    ts_updated_at__gt=since,
                ).values_list("message_id", flat=True)
            )
            if recent_reaction_msg_ids:
                already_included = set(qs.values_list("id", flat=True))
                missing_ids = recent_reaction_msg_ids - already_included
                if missing_ids:
                    extra = Message.objects.filter(
                        channel=channel,
                        is_thread_reply=False,
                        id__in=missing_ids,
                    )
                    qs = qs | extra

        qs = _prefetched_messages(qs.distinct().order_by("ts_sent_at"))

        messages_data = MessageSerializer(qs, many=True).data
        # `deletes` is a separate array per the envelope spec, but since
        # `apply_since_filter` already returns soft-deleted rows in the
        # `messages` array (with deletedAt set), the client can read
        # tombstones directly. We keep `deletes` for hard-deletes (e.g.
        # post-purge) — currently always empty.
        envelope_data = {
            "messages": messages_data,
            "deletes": [],
        }
        return Response(build_delta_response(envelope_data, server_time, force_full))


class ThreadMessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/threads/?since=ISO

    Same shape as `MessagesDeltaView` but for `is_thread_reply=True`
    messages. Split so a client that hasn't opened any thread doesn't
    pay the cost of streaming thread replies on every sync.
    """

    def get(self, request, channel_id):
        channel = _verify_member_or_404(channel_id, request.user)
        server_time = capture_server_time()
        since, force_full = check_since(request)

        qs = Message.objects.filter(channel=channel, is_thread_reply=True)
        qs = _apply_message_since_filter(qs, since)

        if since is not None and not force_full:
            recent_reaction_msg_ids = set(
                MessageReaction.objects.filter(
                    message__channel=channel,
                    message__is_thread_reply=True,
                    ts_updated_at__gt=since,
                ).values_list("message_id", flat=True)
            )
            if recent_reaction_msg_ids:
                already_included = set(qs.values_list("id", flat=True))
                missing_ids = recent_reaction_msg_ids - already_included
                if missing_ids:
                    extra = Message.objects.filter(
                        channel=channel,
                        is_thread_reply=True,
                        id__in=missing_ids,
                    )
                    qs = qs | extra

        qs = _prefetched_messages(qs.distinct().order_by("ts_sent_at"))
        return Response(
            build_delta_response(
                {"messages": MessageSerializer(qs, many=True).data, "deletes": []},
                server_time,
                force_full,
            )
        )


class MessageDetailView(AuthenticatedAPIView):
    """GET /api/v3/messages/{message_id}/

    Fetch a single message by id. Used by deep-links (e.g. notification
    click → load this specific message) and by the test harness.
    """

    def get(self, request, message_id):
        try:
            message = (
                Message.objects.select_related("channel", "sender")
                .prefetch_related("reactions__user", "mentions", "attachments")
                .get(id=message_id)
            )
        except Message.DoesNotExist:
            raise Http404("Message not found.")

        # Verify the user is a member of the message's channel; 404 (not
        # 403) so we don't leak the existence of messages they can't see.
        is_member = ChannelMember.objects.filter(
            channel=message.channel, user=request.user, is_deleted=False
        ).exists()
        if not is_member:
            raise Http404("Message not found.")

        return Response(MessageSerializer(message).data)
