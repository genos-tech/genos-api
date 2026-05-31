"""
Pin + Flag endpoints for the unified messaging schema.

PinView replaces the `UserChatMaster.pinned_chats` JSON list.
FlagView replaces `UserChatMaster.flagged_messages` JSON list.

Both endpoints are idempotent: re-pinning an already-pinned channel
returns the existing row; un-flagging a not-flagged message returns
204 anyway. The new schema has real FK constraints (vs the legacy
JSON lists), so a deleted channel's pins are auto-removed on cascade.

`POST   /api/v3/channels/{channel_id}/pin/`     pin a channel
`DELETE /api/v3/channels/{channel_id}/pin/`     unpin

`POST   /api/v3/messages/{message_id}/flag/`    flag a message
`DELETE /api/v3/messages/{message_id}/flag/`    unflag
"""

from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    Flag,
    Pin,
)
from origin.serializers.chat.unified_serializers import FlagSerializer, PinSerializer
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.chat.message_views import _verify_member_or_404
from origin.views.chat.reaction_views_v3 import _verify_message_member


class PinView(AuthenticatedAPIView):
    """POST   /api/v3/channels/{channel_id}/pin/
    DELETE /api/v3/channels/{channel_id}/pin/

    Pin / unpin a channel for the requesting user. Pins are per-user
    (not global) — pinning a channel only changes its position in YOUR
    chat list, not other members' lists.
    """

    def post(self, request, channel_id):
        channel = _verify_member_or_404(channel_id, request.user)
        pin, created = Pin.objects.get_or_create(user=request.user, channel=channel)
        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(PinSerializer(pin).data, status=status_code)

    def delete(self, request, channel_id):
        # Verify membership via the same 404 path as the other channel-
        # scoped endpoints, so the unpin doesn't leak channel existence
        # to non-members.
        channel = _verify_member_or_404(channel_id, request.user)
        Pin.objects.filter(user=request.user, channel=channel).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FlagView(AuthenticatedAPIView):
    """POST   /api/v3/messages/{message_id}/flag/
    DELETE /api/v3/messages/{message_id}/flag/

    Flag / unflag a message for the requesting user. Same per-user
    semantics as Pin.
    """

    def post(self, request, message_id):
        message = _verify_message_member(message_id, request.user)
        flag, created = Flag.objects.get_or_create(user=request.user, message=message)
        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(FlagSerializer(flag).data, status=status_code)

    def delete(self, request, message_id):
        # Verify membership before delete so we don't leak existence.
        message = _verify_message_member(message_id, request.user)
        Flag.objects.filter(user=request.user, message=message).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
