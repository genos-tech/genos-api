"""
Messages pinned in a chat for everyone (MIP-2487).

`GET    /api/v3/channels/{channel_id}/message_pins/`  the chat's pins, newest first
`POST   /api/v3/messages/{message_id}/message_pin/`   pin it for everyone
`DELETE /api/v3/messages/{message_id}/message_pin/`   unpin it for everyone

Not `pin_flag_views.PinView`, which pins a whole CHAT in one person's sidebar.

Any member of the chat may pin and unpin. The two writes answer with ids only, and genos-sockets
broadcasts only those: each member then reads the list, which applies THEIR history window, so a
pinned message's text never reaches somebody whose plan hides it.
"""

from django.db import transaction
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import Channel, Message, MessagePin
from origin.serializers.chat.unified_serializers import MessagePinSerializer, MessageSerializer
from origin.services.archive import archived_channel_response
from origin.views.chat.message_views import (
    _prefetched_messages,
    _retention_cutoff,
    _verify_member_or_404,
)
from origin.views.chat.reaction_views_v3 import _verify_message_member
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


def _coordinates(message):
    """What genos-sockets needs to tell the right room, taken from the row, never the client."""
    return {
        "messageId": str(message.id),
        "channelId": str(message.channel_id),
        "channelKind": message.channel.kind,
    }


class ChannelMessagePinListView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/message_pins/"""

    def get(self, request, channel_id):
        channel = _verify_member_or_404(channel_id, request.user)
        pins = MessagePin.objects.filter(
            channel=channel, message__channel=channel, message__deleted_at__isnull=True
        )
        _days, cutoff = _retention_cutoff(request.user)
        hidden = 0
        if cutoff is not None:
            hidden = pins.filter(message__ts_sent_at__lt=cutoff).count()
            pins = pins.filter(message__ts_sent_at__gte=cutoff)
        pins = list(pins.select_related("pinned_by").order_by("-ts_created_at", "-id"))
        messages = {
            message.id: message
            for message in _prefetched_messages(
                Message.objects.filter(id__in=[pin.message_id for pin in pins])
            )
        }
        rows = [
            {
                **MessagePinSerializer(pin).data,
                "message": MessageSerializer(messages[pin.message_id]).data,
            }
            for pin in pins
            if pin.message_id in messages
        ]
        return Response(
            {
                "pins": rows,
                "hiddenCount": hidden,
                "limit": MessagePin.LIMIT_PER_CHANNEL,
                "server_time": timezone.now().isoformat(),
            }
        )


class MessagePinView(AuthenticatedAPIView):
    """POST / DELETE /api/v3/messages/{message_id}/message_pin/

    Both are idempotent. 404 to a non-member (and, for a pin, for a message older than the
    caller's history window); 409 in an archived chat or project. A pin is refused with 400 for a
    deleted message or a thread reply, and with 409 once the chat holds the most it can.
    """

    def post(self, request, message_id):
        message = _verify_message_member(message_id, request.user)
        _days, cutoff = _retention_cutoff(request.user)
        if cutoff is not None and message.ts_sent_at < cutoff:
            raise Http404("Message not found.")
        if res := archived_channel_response(message.channel):
            return res
        if message.deleted_at is not None:
            return Response(
                {"error": "MESSAGE_DELETED: a deleted message cannot be pinned."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if message.is_thread_reply:
            return Response(
                {"error": "THREAD_REPLY_NOT_PINNABLE: a thread reply cannot be pinned."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        with transaction.atomic():
            # Two members pinning at 49 must not make 51.
            Channel.objects.select_for_update().filter(id=message.channel_id).first()
            created = False
            if not MessagePin.objects.filter(message=message).exists():
                held = MessagePin.objects.filter(
                    channel_id=message.channel_id, message__channel_id=message.channel_id
                ).count()
                if held >= MessagePin.LIMIT_PER_CHANNEL:
                    # The code leads the text: genos-sockets passes only the text on.
                    return Response(
                        {
                            "error": "PIN_LIMIT_REACHED: a chat can hold "
                            f"{MessagePin.LIMIT_PER_CHANNEL} pinned messages.",
                            "limit": MessagePin.LIMIT_PER_CHANNEL,
                        },
                        status=status.HTTP_409_CONFLICT,
                    )
                _pin, created = MessagePin.objects.get_or_create(
                    message=message,
                    defaults={"channel_id": message.channel_id, "pinned_by": request.user},
                )
        return Response(
            _coordinates(message),
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request, message_id):
        message = _verify_message_member(message_id, request.user)
        if res := archived_channel_response(message.channel):
            return res
        removed, _ = MessagePin.objects.filter(message=message).delete()
        return Response({**_coordinates(message), "removed": bool(removed)})
