from django.db import transaction

from rest_framework.response import Response
from rest_framework import status

from origin.services import unified_writer
from origin.views.utils.request_validators import validate_request_data, validate_request_user
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.activity_models import ActivityFact
from origin.models.chat.read_status_models import *
from origin.serializers.chat.read_status_serializers import *


def _dual_write_read_cursor(data: dict) -> None:
    """Track B dual-write helper called from both create and update
    branches below. Mirrors a legacy ReadStatus upsert into the unified
    ReadCursor schema. No-op when the flag is off; never raises."""
    unified_writer.write_read_cursor(
        chat_type=int(data["chat_type"]),
        chat_id=int(data["chat_id"]),
        user_id=data["user"],
        last_read_message_id=int(data["last_read_message_id"]),
        is_thread=bool(data["is_thread"]),
        thread_id=int(data["thread_id"]) if data.get("thread_id") else None,
    )


class ReadStatusView(AuthenticatedAPIView):
    def put(self, request):
        """Upsert Operation, not a simple PUT"""

        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "user": request.data.get("user_id"),
            "chat_type": request.data.get("chat_type"),
            "chat_id": request.data.get("chat_id"),
            "is_thread": request.data.get("is_thread"),
            "thread_id": request.data.get("thread_id"),
            "last_read_message_id": request.data.get("last_read_message_id"),
        }

        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request_user_id), str(data["user"])):
            return res

        try:
            prev_status = ReadStatus.objects.get(
                team=data["team"],
                user=data["user"],
                chat_type=data["chat_type"],
                chat_id=data["chat_id"],
                is_thread=data["is_thread"],
                thread_id=data["thread_id"],
            )
            serializer = ReadStatusSerializer(prev_status, data=data, partial=True)
            if serializer.is_valid():
                # Update only when the message id is larger than the prev one.
                if int(prev_status.last_read_message_id) < int(data["last_read_message_id"]):
                    serializer.save()
                    _dual_write_read_cursor(data)
                return Response(serializer.data, status=status.HTTP_200_OK)
        except ReadStatus.DoesNotExist:
            serializer = ReadStatusSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                _dual_write_read_cursor(data)
                return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ActivityReadStatusView(AuthenticatedAPIView):
    def put(self, request):
        """Upsert Operation, not a simple PUT"""

        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "user": request.data.get("user_id"),
            "activity": request.data.get("activity_id"),
            "is_read": request.data.get("is_read"),
        }

        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request_user_id), str(data["user"])):
            return res

        # For mention messages, the activity_id is
        # for non-thread messages: <activity_type>-<chat_type>-<chat_id>-<message_id>.
        # for thread messages: <activity_type>-<chat_type>-<chat_id>-<thread_id>-<message_id>.
        # But, the activity_type is always 1 in the database.
        # When we response the activities, we'll change it to 3 if the request user is mentioned in the message.
        # So, we need to change it to 1 if the activity_type is 3 to keep the activity_id consistent in the database.
        if data["activity"] and data["activity"][0] == "3":
            data["activity"] = "1" + data["activity"][1:]

        try:
            prev_status = ActivityReadStatus.objects.get(
                team=data["team"],
                user=data["user"],
                activity=data["activity"],
            )
            serializer = ActivityReadStatusSerializer(prev_status, data=data, partial=True)
            if serializer.is_valid():
                # NOTE: the original implementation forgot `.save()` here, so any
                # PUT that hit an existing row (e.g. flipping is_read from False
                # to True) was silently discarded. That latent bug never bit the
                # main read flow — the only caller always sends is_read=True
                # against a row that's also already True — but it broke as soon
                # as we needed to mutate an existing row.
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
        except ActivityReadStatus.DoesNotExist:
            serializer = ActivityReadStatusSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class MarkAllActivityAsReadView(AuthenticatedAPIView):
    def put(self, request):
        """Mark every activity for the given chat (DM/GM/PM/MDM) as read.

        Thread-message activities are included automatically: `is_thread` and
        `thread_id` live on `ActivityFact` and we deliberately do NOT filter
        on them, so a single call clears both inline and threaded activity
        badges for the chat.

        Implementation note — why this isn't just a `.update()`:
        the activity-history endpoint computes `isRead` as
        `EXISTS(ActivityReadStatus row with is_read=True for this user/activity)`
        (see `get_message_activities.py` etc.). Rows are only created lazily
        by `ActivityReadStatusView.put` when the user opens an individual
        activity. So an activity the user has never opened has *no* row at
        all, and a naive `.filter(is_read=False).update(is_read=True)` would
        match zero rows for it — the next history fetch then still reports
        it unread (the bug that motivated this fix).

        We therefore do an upsert in one transaction:
          1. `bulk_create` an `is_read=True` row for every activity in the
             chat. The unique `(user, activity_id)` constraint + the
             `ignore_conflicts=True` flag make this a no-op for activities
             the user has already opened.
          2. Run a single `.update(is_read=True)` to flip any pre-existing
             `is_read=False` rows (rows that exist but were left False —
             rare but possible).
        """

        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "user": request.data.get("user_id"),
            "chat_type": request.data.get("chat_type"),
            "chat_id": request.data.get("chat_id"),
        }

        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request_user_id), str(data["user"])):
            return res

        try:
            with transaction.atomic():
                activity_ids = list(
                    ActivityFact.objects.filter(
                        team=data["team"],
                        chat_type=data["chat_type"],
                        chat_id=data["chat_id"],
                    ).values_list("activity_id", flat=True)
                )

                if activity_ids:
                    ActivityReadStatus.objects.bulk_create(
                        [
                            ActivityReadStatus(
                                team_id=data["team"],
                                user_id=data["user"],
                                activity_id=aid,
                                is_read=True,
                            )
                            for aid in activity_ids
                        ],
                        ignore_conflicts=True,
                    )
                    ActivityReadStatus.objects.filter(
                        team=data["team"],
                        user=data["user"],
                        is_read=False,
                        activity_id__in=activity_ids,
                    ).update(is_read=True)
        except Exception:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_200_OK)
