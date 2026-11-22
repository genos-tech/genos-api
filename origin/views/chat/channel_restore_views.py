"""Recently deleted group chats and restoring one (MIP-1715). See `services/channel_restore.py`
for who may, what comes back and what does not."""

import uuid

from django.http import Http404
from rest_framework import status
from rest_framework.response import Response

from origin.services.channel_restore import (
    member_counts,
    restorable_channels_qs,
    restore_group_chat,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


class DeletedChannelListView(AuthenticatedAPIView):
    """GET /api/v3/channels/deleted/?team_id=<uuid>

    The group chats the caller deleted softly and may still restore, newest first. Only their
    own: a chat someone else owns is not listed, whoever else is in it. `deletedAt` is the time
    of the delete (the row is untouched since, because a deleted chat is invisible to everyone).
    """

    def get(self, request):
        raw = request.GET.get("team_id")
        if not raw:
            return Response(
                {"error": "team_id is required.", "code": "team_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            team_id = str(uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            return Response({"channels": []})
        rows = list(
            restorable_channels_qs(request.user)
            .filter(team_id=team_id)
            .order_by("-ts_updated_at", "id")
        )
        counts = member_counts(c.id for c in rows)
        return Response(
            {
                "channels": [
                    {
                        "id": str(c.id),
                        "title": c.title,
                        "isPrivate": c.is_private,
                        "isHidden": c.is_hidden,
                        "deletedAt": c.ts_updated_at,
                        "memberCount": counts.get(c.id, 0),
                    }
                    for c in rows
                ]
            }
        )


class ChannelRestoreView(AuthenticatedAPIView):
    """POST /api/v3/channels/{channel_id}/restore/

    Restore a soft-deleted group chat. Only its owner may. Anything else is a 404 with no hint
    that the chat exists: someone who is not its owner, a chat that was hard-deleted (no row),
    and a chat that is not deleted (so a second click on Restore is a 404 and changes nothing).
    """

    def post(self, request, channel_id):
        channel = restorable_channels_qs(request.user).filter(id=channel_id).first()
        if channel is None:
            raise Http404("Channel not found.")
        if not restore_group_chat(channel):
            raise Http404("Channel not found.")
        return Response(
            {"restored": True, "id": str(channel.id), "title": channel.title},
            status=status.HTTP_200_OK,
        )
