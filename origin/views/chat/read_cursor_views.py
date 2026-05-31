"""
Read-cursor endpoint for the unified messaging schema.

`PUT /api/v3/channels/{channel_id}/read_cursor/`

Advance the requesting user's read pointer in a channel (or in a
specific thread within a channel). Replaces the legacy
`PUT /api/v2/chat/read/` endpoint which keyed cursors by composite
(chat_type, chat_id, thread_id) — see `ReadStatus`.

Semantics: forward-only. If `last_read_message_id` would lower the
existing cursor, the update is silently a no-op (the server-side
truth wins). This matches the legacy behaviour and prevents a stale
client from rewinding the user's read state.
"""

from django.http import Http404
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    ChannelMember,
    Message,
    ReadCursor,
)
from origin.serializers.chat.unified_serializers import ReadCursorSerializer
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.chat.message_views import _verify_member_or_404


class ReadCursorView(AuthenticatedAPIView):
    """PUT /api/v3/channels/{channel_id}/read_cursor/

    Request body:
        {
          "last_read_message_id": "<uuid>",   # required
          "thread_root_id": "<uuid>" | null,  # null for main timeline
        }

    The server validates that the target message is in this channel
    and (for thread cursors) in this thread. Returns the updated
    `ReadCursor` row.
    """

    def put(self, request, channel_id):
        channel = _verify_member_or_404(channel_id, request.user)
        body = request.data or {}
        last_read_message_id = body.get("last_read_message_id")
        thread_root_id = body.get("thread_root_id") or None

        if not last_read_message_id:
            return Response(
                {"error": "last_read_message_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Verify the target message exists and belongs to this channel.
        try:
            target = Message.objects.get(id=last_read_message_id, channel=channel)
        except Message.DoesNotExist:
            return Response(
                {"error": "last_read_message_id not found in this channel."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # For thread cursors: enforce that the target is inside the thread.
        if thread_root_id is not None:
            try:
                Message.objects.get(id=thread_root_id, channel=channel)
            except Message.DoesNotExist:
                return Response(
                    {"error": "thread_root_id not found in this channel."},
                    status=status.HTTP_404_NOT_FOUND,
                )
            target_root = target.thread_root_id or target.id
            if str(target_root) != str(thread_root_id):
                return Response(
                    {"error": "last_read_message_id is not in this thread."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Forward-only advance: if the existing cursor's last_read seq
        # is higher, leave it alone. Compared by seq (not by ts) because
        # seq is a monotonic per-channel id; ts could regress with clock
        # skew or backdated edits.
        cursor, _ = ReadCursor.objects.get_or_create(
            user=request.user,
            channel=channel,
            thread_root_id=thread_root_id,
            defaults={"last_read_message": target},
        )
        existing_seq = cursor.last_read_message.seq if cursor.last_read_message_id else -1
        if target.seq > existing_seq:
            cursor.last_read_message = target
            cursor.save(update_fields=["last_read_message", "last_read_at", "ts_updated_at"])

        return Response(ReadCursorSerializer(cursor).data)
