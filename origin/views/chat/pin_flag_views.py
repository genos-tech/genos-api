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
`GET    /api/v3/pins/`                          list the user's pins

`POST   /api/v3/messages/{message_id}/flag/`    flag a message
`PATCH  /api/v3/messages/{message_id}/flag/`    mark done / reopen
`DELETE /api/v3/messages/{message_id}/flag/`    unflag (hard delete)
`GET    /api/v3/flags/?status=active|completed` list the user's flags

`POST   /api/v3/messages/{message_id}/reminder/` remind me at T (and flag)
`DELETE /api/v3/messages/{message_id}/reminder/` cancel the reminder
`GET    /api/v3/reminders/`                      list pending reminders

Reminders live here rather than in their own module because they are a
flag with a time on it: setting one flags the message, and both ways of
finishing with a flag (unflag, mark done) cancel it — which is why the
flag handlers below call into `message_reminders` too.
"""

from django.utils import dateparse, timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    Flag,
    Pin,
)
from origin.serializers.chat.unified_serializers import (
    FlagSerializer,
    MessageReminderSerializer,
    PinSerializer,
)
from origin.services import message_reminders
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


class PinListView(AuthenticatedAPIView):
    """GET /api/v3/pins/

    List the requesting user's pinned channels, newest pin first.

    Pins were write-only before this: `PinView` persisted them, and the
    client learned about new ones from the `pin.added` / `pin.removed`
    socket broadcasts, but there was no way to READ the existing set
    back. The client's only source was its own IndexedDB cache, so a
    fresh browser, a second device, or a cleared cache showed no pins at
    all even though the rows were sitting in the database — the data was
    durable but unreachable. Mirrors `FlagListView`.
    """

    def get(self, request):
        qs = (
            Pin.objects.filter(user=request.user)
            .select_related("channel")
            .order_by("-ts_created_at")
        )
        return Response(
            {
                "pins": PinSerializer(qs, many=True).data,
                "server_time": timezone.now().isoformat(),
            }
        )


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
            return Response({"error": "Flag not found."}, status=status.HTTP_404_NOT_FOUND)
        completed = bool(request.data.get("completed", False))
        flag.completed_at = timezone.now() if completed else None
        flag.save(update_fields=["completed_at"])
        # Marking it done retires any reminder: the reminder's job was to
        # bring this back to you, and you just said you're finished with
        # it. Reopening does NOT resurrect the reminder — the time the user
        # picked has usually passed by then, so they set a new one.
        if completed:
            message_reminders.cancel_pending(user=request.user, message=message)
        return Response(FlagSerializer(flag).data, status=status.HTTP_200_OK)

    def delete(self, request, message_id):
        # Verify membership before delete so we don't leak existence.
        message = _verify_message_member(message_id, request.user)
        Flag.objects.filter(user=request.user, message=message).delete()
        # Unflagging is the other way of being done with it (see PATCH).
        # Without this the reminder would arrive pointing at a message the
        # user has already dismissed from the list it would send them to.
        message_reminders.cancel_pending(user=request.user, message=message)
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
        qs = Flag.objects.filter(user=request.user).select_related("message", "message__sender")
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


class MessageReminderView(AuthenticatedAPIView):
    """POST   /api/v3/messages/{message_id}/reminder/  remind me at T
    DELETE /api/v3/messages/{message_id}/reminder/  cancel

    POST body: `{"remindAt": "<ISO-8601 instant>"}`. Flags the message as a
    side effect, and returns the flag alongside the reminder so the client
    can reflect both from one round trip.

    `remindAt` is an absolute instant computed by the CLIENT, presets
    included ("tomorrow 9am" is 9am wherever the user is, and the browser
    is the only party that reliably knows that). The server's job is to
    refuse instants it cannot honour: the past, and beyond a year.
    """

    def post(self, request, message_id):
        message = _verify_message_member(message_id, request.user)
        raw = (request.data or {}).get("remindAt")
        remind_at = dateparse.parse_datetime(raw) if isinstance(raw, str) else None
        if remind_at is None:
            return Response(
                {"error": "remindAt must be an ISO-8601 datetime."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timezone.is_naive(remind_at):
            # A naive instant is ambiguous, and guessing costs the user the
            # one thing this feature sells: arriving at the right moment.
            return Response(
                {"error": "remindAt must carry a UTC offset."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            message_reminders.validate_remind_at(remind_at)
        except message_reminders.ReminderWindowError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        reminder, flag = message_reminders.set_reminder(
            user=request.user, message=message, remind_at=remind_at
        )
        return Response(
            {
                "reminder": MessageReminderSerializer(reminder).data,
                "flag": FlagSerializer(flag).data,
            },
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request, message_id):
        # Cancelling the reminder leaves the flag alone: "stop nagging me"
        # is not "forget about this". Idempotent, like unflagging.
        message = _verify_message_member(message_id, request.user)
        message_reminders.cancel_pending(user=request.user, message=message)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MessageReminderListView(AuthenticatedAPIView):
    """GET /api/v3/reminders/

    The requesting user's pending reminders, soonest first. Read at boot so
    a message bubble can say "reminder set for …" — the client cannot infer
    that from its flag cache, and a reminder you can't see is one you set
    twice.
    """

    def get(self, request):
        qs = message_reminders.pending_for_user(request.user)
        return Response(
            {
                "reminders": MessageReminderSerializer(qs, many=True).data,
                "server_time": timezone.now().isoformat(),
            }
        )
