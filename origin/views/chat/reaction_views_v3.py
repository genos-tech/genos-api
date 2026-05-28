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
from origin.serializers.chat.unified_serializers import MessageReactionSerializer
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
        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(MessageReactionSerializer(reaction).data, status=status_code)

    def delete(self, request, message_id):
        message = _verify_message_member(message_id, request.user)
        emoji = (request.data or {}).get("emoji")
        if not emoji or not isinstance(emoji, str):
            return Response(
                {"error": "emoji must be a non-empty string."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        MessageReaction.objects.filter(message=message, user=request.user, emoji=emoji).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
