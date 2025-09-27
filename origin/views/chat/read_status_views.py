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
            "user": request.data.get("user"),
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
                user=data["user"],
                chat_type=data["chat_type"],
                chat_id=data["chat_id"],
                is_thread=data["is_thread"],
                thread_id=data["thread_id"],
            )
            serializer = ReadStatusSerializer(prev_status, data=request.data, partial=True)
            if serializer.is_valid():
                # Update only when the message id is larger than the prev one.
                if int(prev_status.last_read_message_id) < int(data["last_read_message_id"]):
                    serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
        except:
            serializer = ReadStatusSerializer(data=request.data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
