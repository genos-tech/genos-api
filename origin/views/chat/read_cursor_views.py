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

import uuid

from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Message,
    ReadCursor,
)
from origin.serializers.chat.unified_serializers import ReadCursorSerializer
from origin.views.chat.message_views import _verify_member_or_404
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

# Activity types whose backing message's `seq` faithfully marks "when the
# recipient could first see it" — so reading the channel up to that seq
# genuinely means they saw the activity. MESSAGE / MENTION / THREAD_REPLY /
# TASK_ASSIGN are all created at the same instant as the message they point
# at, so their message always sits at or above the recipient's prior cursor.
#
# REACTION is deliberately excluded: a reaction to an OLD message mints a
# NEW activity that points back at that old (low-seq) message. The
# recipient's cursor is already past it, so a seq-based sweep would wrongly
# clear a reaction notification they never actually saw. Reaction activities
# stay clickable-to-read (their own endpoint) — they are never auto-cleared
# by a read-cursor advance.
_SEQ_CLEARABLE_ACTIVITY_TYPES = (
    ActivityType.MESSAGE,
    ActivityType.MENTION,
    ActivityType.THREAD_REPLY,
    ActivityType.TASK_ASSIGN,
)


def _clear_activities_up_to(*, user, channel, target, thread_root_id):
    """Mark the user's unread channel activities read once their read
    cursor reaches `target`.

    Complements the presence layer's `_drop_active_viewers`, which
    suppresses CREATING an activity for a message the recipient is
    actively viewing: this clears the ones that were created while they
    were away, the moment their cursor sweeps past the backing message.

    Scoped three ways so it only ever clears what the cursor advance
    actually covered:
      - `recipient=user, is_read=False` — never touch another user's rows
        or redo already-read ones.
      - `message__seq__lte=target.seq` — only activities at or before the
        message just read. `seq` (not ts) is the comparator, matching the
        cursor's own forward-only ordering.
      - thread scope — a MAIN-timeline cursor (thread_root_id is None)
        clears only main-timeline activities (`message__thread_root__isnull`);
        a THREAD cursor clears only that thread's activities
        (`message__thread_root_id=thread_root_id`). Without this a thread
        read would wrongly clear main-timeline activities that happen to
        have a lower seq, and vice-versa.

    Surface activities (task-body / note @-mentions) have `message=NULL`
    and are excluded by the `message__seq` filter — they only clear via
    their own read endpoints. Returns the list of cleared activity ids
    (as strings) so the caller can echo them to the client for a live
    sidebar update; empty when nothing matched.
    """
    qs = Activity.objects.filter(
        recipient=user,
        channel=channel,
        is_read=False,
        activity_type__in=_SEQ_CLEARABLE_ACTIVITY_TYPES,
        message__seq__lte=target.seq,
    )
    if thread_root_id is None:
        qs = qs.filter(message__thread_root__isnull=True)
    else:
        qs = qs.filter(message__thread_root_id=thread_root_id)

    cleared_ids = [str(pk) for pk in qs.values_list("id", flat=True)]
    if cleared_ids:
        Activity.objects.filter(id__in=cleared_ids).update(is_read=True)
    return cleared_ids


def _as_uuid(value):
    """Parse a client-supplied message id, or None if it isn't a UUID.

    `Message.id` is a `UUIDField`, so handing it a non-UUID string makes
    Django raise `ValidationError` from `get_prep_value` — an uncaught
    500, not the 400 a malformed request deserves. This is reachable in
    normal use: the web client renders an optimistic echo keyed by its
    `corr-<random>` correlation id, and a mark-read that fires before
    the server ack lands forwards that placeholder as the cursor.
    """
    try:
        return uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


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

        target_id = _as_uuid(last_read_message_id)
        if target_id is None:
            return Response(
                {"error": "last_read_message_id must be a UUID."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Verify the target message exists and belongs to this channel.
        try:
            target = Message.objects.get(id=target_id, channel=channel)
        except Message.DoesNotExist:
            return Response(
                {"error": "last_read_message_id not found in this channel."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # For thread cursors: enforce that the target is inside the thread.
        if thread_root_id is not None:
            root_id = _as_uuid(thread_root_id)
            if root_id is None:
                return Response(
                    {"error": "thread_root_id must be a UUID."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            thread_root_id = root_id
            try:
                Message.objects.get(id=root_id, channel=channel)
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

        # Clear the recipient's unread activities up to the cursor's FINAL
        # position (the just-set target, or the existing higher seq on a
        # no-op rewind). Reading `cursor.last_read_message` — not `target` —
        # is deliberate: it also covers the get_or_create first-advance case,
        # where the row is created with `target` via defaults and the
        # forward-only block above is skipped (target.seq == existing_seq).
        # Idempotent (filters is_read=False), so a rewind no-op re-clears an
        # already-cleared subset harmlessly and self-heals a prior failure.
        read_activity_ids = _clear_activities_up_to(
            user=request.user,
            channel=channel,
            target=cursor.last_read_message,
            thread_root_id=thread_root_id,
        )

        # `readActivityIds` rides along on the cursor payload the socket
        # layer already broadcasts to the user's own room (`read.advanced`),
        # so every tab clears the same sidebar rows without an extra fetch.
        # Absent/empty ⇒ nothing to clear; older clients simply ignore it.
        data = ReadCursorSerializer(cursor).data
        data["readActivityIds"] = read_activity_ids
        return Response(data)
