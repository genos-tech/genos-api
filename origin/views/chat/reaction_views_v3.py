"""
Reaction endpoints for the unified messaging schema.

`/api/v3/messages/{message_id}/reactions/`

  POST   add a reaction to a message
  DELETE remove a reaction (DELETE body includes `emoji`)

Suffix `_v3` is on the filename so the legacy `reaction_views.py`
(consumed by `urls.py` for the old `/api/v2/chat/reaction/` route)
keeps working until we delete the v2 surface.
"""

from django.http import Http404
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    ChannelMember,
    Message,
    MessageReaction,
)
from origin.serializers.chat.unified_serializers import (
    ActivitySerializer,
    MessageReactionSerializer,
)
from origin.services import v3_activity
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


def _verify_message_member(message_id, user):
    """Fetch a Message scoped to the user's channel membership.

    404 for non-members so we don't leak message existence.
    """
    try:
        message = Message.objects.select_related("channel").get(id=message_id)
    except Message.DoesNotExist:
        raise Http404("Message not found.")
    is_member = ChannelMember.objects.filter(
        channel=message.channel, user=user, is_deleted=False
    ).exists()
    if not is_member:
        raise Http404("Message not found.")
    return message


class MessageReactionsView(AuthenticatedAPIView):
    """POST   /api/v3/messages/{message_id}/reactions/
    DELETE /api/v3/messages/{message_id}/reactions/

    POST body: `{"emoji": "<str>"}`. Idempotent — re-adding the same
    (user, message, emoji) returns the existing row.

    DELETE body: `{"emoji": "<str>"}`. Idempotent — removing a reaction
    that doesn't exist returns 204 anyway.

    Why DELETE-with-body rather than DELETE on a dedicated emoji
    sub-path: emojis are arbitrary unicode (👍, ❤️, plus skin-tone
    modifiers) and URL-encoding them produces fragile, hard-to-debug
    paths. Putting the emoji in the body keeps the route stable.
    """

    def post(self, request, message_id):
        message = _verify_message_member(message_id, request.user)
        emoji = (request.data or {}).get("emoji")
        if not emoji or not isinstance(emoji, str):
            return Response(
                {"error": "emoji must be a non-empty string."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        reaction, created = MessageReaction.objects.get_or_create(
            message=message, user=request.user, emoji=emoji
        )
        # Fan out an Activity row to the message's sender (skipped when
        # `actor == sender`, i.e. self-reactions). Only fire on a real
        # new reaction — re-adding the same emoji is a no-op for the
        # activity sidebar too.
        activities = []
        if created:
            activities = v3_activity.create_reaction_activity(
                message=message, emoji=emoji, actor=request.user
            )
            # Bump the parent message's ts_updated_at so the reaction
            # change propagates through the `?since=` delta sync, which
            # keys on Message.ts_updated_at. (The delta also unions rows
            # by recent MessageReaction.ts_updated_at, but a REMOVE
            # deletes that row — see delete() — so bumping the message is
            # the reliable, symmetric signal for both add and remove.)
            message.save(update_fields=["ts_updated_at"])
        response_data = MessageReactionSerializer(reaction).data
        # Same `_v3_activities` proxy convention as message_views.post —
        # the WS reaction handler reads this and broadcasts
        # `activity.created` to the sender's `user:{id}` room.
        response_data["_v3_activities"] = ActivitySerializer(activities, many=True).data
        # Server-derived channel coordinates so the WS layer broadcasts
        # `reaction.added` to the message's REAL channel room — never a
        # client-asserted one (which could misroute the reaction into, or
        # inject a phantom reaction into, another channel's room). The
        # Flask handler pops these before forwarding the clean reaction.
        response_data["channelId"] = str(message.channel_id)
        response_data["channelKind"] = message.channel.kind
        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(response_data, status=status_code)

    def delete(self, request, message_id):
        message = _verify_message_member(message_id, request.user)
        emoji = (request.data or {}).get("emoji")
        if not emoji or not isinstance(emoji, str):
            return Response(
                {"error": "emoji must be a non-empty string."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        deleted_count, _ = MessageReaction.objects.filter(
            message=message, user=request.user, emoji=emoji
        ).delete()
        # Bump the parent message so the removal propagates via the
        # `?since=` delta sync. The delta unions rows by recent
        # MessageReaction.ts_updated_at, but a remove DELETES that row, so
        # an offline client would otherwise never catch up the removal
        # (stale chip until a full reload). Bumping Message.ts_updated_at
        # is the reliable signal. Only when something was actually removed.
        if deleted_count:
            message.save(update_fields=["ts_updated_at"])
        # Return the message's channel (instead of 204) so the WS layer
        # broadcasts `reaction.removed` to the server-derived room rather
        # than trusting the client-asserted channel coordinates.
        return Response(
            {"channelId": str(message.channel_id), "channelKind": message.channel.kind},
            status=status.HTTP_200_OK,
        )
