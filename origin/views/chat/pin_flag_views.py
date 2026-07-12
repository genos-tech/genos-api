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
`PATCH  /api/v3/messages/{message_id}/flag/`    mark done / reopen
`DELETE /api/v3/messages/{message_id}/flag/`    unflag (hard delete)
`GET    /api/v3/flags/?status=active|completed` list the user's flags
"""

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    Flag,
    Pin,
)
from origin.serializers.chat.unified_serializers import FlagSerializer, PinSerializer
from origin.views.chat.message_views import _verify_member_or_404
from origin.views.chat.reaction_views_v3 import _verify_message_member
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


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
    """POST   /api/v3/messages/{message_id}/flag/   flag / re-flag
    PATCH  /api/v3/messages/{message_id}/flag/   mark done / reopen
    DELETE /api/v3/messages/{message_id}/flag/   unflag (hard delete)

    Flag a message for the requesting user. Same per-user semantics as
    Pin. "Done" is a soft state (`completed_at`) via PATCH, distinct from
    the hard-delete DELETE, so a completed flag is retained for the
    past-flags view.
    """

    def post(self, request, message_id):
        message = _verify_message_member(message_id, request.user)
        flag, created = Flag.objects.get_or_create(user=request.user, message=message)
        # Re-flagging a completed message returns the existing row
        # (created=False, `uniq_flag`) — reactivate it so it re-enters the
        # active list instead of silently staying done.
        if not created and flag.completed_at is not None:
            flag.completed_at = None
            flag.save(update_fields=["completed_at"])
        status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
        return Response(FlagSerializer(flag).data, status=status_code)

    def patch(self, request, message_id):
        # Toggle the done state. `completed=true` marks done; `false`
        # reopens. Kept separate from DELETE so completing retains the row.
        message = _verify_message_member(message_id, request.user)
        try:
            flag = Flag.objects.get(user=request.user, message=message)
        except Flag.DoesNotExist:
            return Response(
                {"error": "Flag not found."}, status=status.HTTP_404_NOT_FOUND
            )
        completed = bool(request.data.get("completed", False))
        flag.completed_at = timezone.now() if completed else None
        flag.save(update_fields=["completed_at"])
        return Response(FlagSerializer(flag).data, status=status.HTTP_200_OK)

    def delete(self, request, message_id):
        # Verify membership before delete so we don't leak existence.
        message = _verify_message_member(message_id, request.user)
        Flag.objects.filter(user=request.user, message=message).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class FlagListView(AuthenticatedAPIView):
    """GET /api/v3/flags/?status=active|completed

    List the requesting user's flags. `active` (default) returns
    outstanding flags newest-first; `completed` returns done flags
    ordered by completion time. Per-user; the client back-fills host
    message bodies separately (the serializer only carries message_id).
    """

    def get(self, request):
        status_param = request.GET.get("status", "active")
        qs = Flag.objects.filter(user=request.user).select_related(
            "message", "message__sender"
        )
        if status_param == "completed":
            qs = qs.filter(completed_at__isnull=False).order_by("-completed_at")
        else:
            qs = qs.filter(completed_at__isnull=True).order_by("-ts_created_at")
        return Response(
            {
                "flags": FlagSerializer(qs, many=True).data,
                "server_time": timezone.now().isoformat(),
            }
        )
