from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.serializers.chat.chat_attachment_serializers import ChatAttachmentFactSerializer
from origin.views.utils.request_validators import validate_request_data, validate_request_user


class ChatAttachmentView(AuthenticatedAPIView):
    def post(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "chat_type": request.data.get("chat_type"),
            "chat_id": request.data.get("chat_id"),
            "message_id": request.data.get("message_id"),
            "thread_id": request.data.get("thread_id"),
            "uploader": request.data.get("uploader"),
            "chat_attachment_url": request.FILES.get("chat_attachment_file"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["uploader"])):
            return res

        serializer = ChatAttachmentFactSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            res = {
                "chat_type": serializer.data["chat_type"],
                "chat_id": serializer.data["chat_id"],
                "message_id": serializer.data["message_id"],
                "thread_id": serializer.data["thread_id"],
                "uploader": serializer.data["uploader"],
                "attachmentId": serializer.data["attachment_id"],
                "chatAttachmentUrl": serializer.data["chat_attachment_url"],
                "tsCreated": serializer.data["ts_created_at"],
                "tsUpdated": serializer.data["ts_updated_at"],
            }
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
