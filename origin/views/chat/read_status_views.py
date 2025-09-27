from rest_framework.response import Response
from rest_framework import status

from origin.views.utils.request_validators import validate_request_data, validate_request_user
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.read_status_models import *
from origin.serializers.chat.read_status_serializers import *


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
                return Response(serializer.data, status=status.HTTP_200_OK)
        except:
            serializer = ReadStatusSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
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

        try:
            prev_status = ActivityReadStatus.objects.get(
                team=data["team"],
                user=data["user"],
                activity=data["activity"],
            )
            serializer = ActivityReadStatusSerializer(prev_status, data=data, partial=True)
            if serializer.is_valid():
                return Response(serializer.data, status=status.HTTP_200_OK)
        except:
            serializer = ActivityReadStatusSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class MarkAllActivityAsReadView(AuthenticatedAPIView):
    def put(self, request):
        """Mark all activity as read"""

        request_user_id = request.user.id

        data = {"team": request.data.get("team_id"), "user": request.data.get("user_id")}

        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request_user_id), str(data["user"])):
            return res

        try:
            ActivityReadStatus.objects.filter(
                team=data["team"], user=data["user"], is_read=False
            ).update(is_read=True)
        except:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        return Response(status=status.HTTP_200_OK)
