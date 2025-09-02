from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.mention_models import *
from origin.serializers.chat.mention_serializers import *


class ChatMentionView(AuthenticatedAPIView):
    def post(self, request):
        res = []
        try:
            is_thread = int(request.data["is_thread_binary"]) == 1
            for mentioned_user_id in list(request.data["mentioned_user_ids"]):
                data = {
                    "team": request.data["team_id"],
                    "chat_type": request.data["chat_type"],
                    "chat_id": request.data["chat_id"],
                    "message_id": int(request.data["message_id"]),
                    "is_thread": is_thread,
                    "thread_id": int(request.data["thread_id"]),
                    "mentioned_user": mentioned_user_id,
                }

                serializer = MentionFactSerializer(data=data)
                if serializer.is_valid():
                    serializer.save()
                    res.append(serializer.data)
        except:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        return Response(res, status=status.HTTP_201_CREATED)

    def get(self, request):
        team_id = request.GET.get("team_id")
        chat_type = request.GET.get("chat_type")
        chat_id = request.GET.get("chat_id")
        is_thread_binary = request.GET.get("is_thread_binary")
        is_thread = int(is_thread_binary) == 1
        thread_id = request.GET.get("thread_id")
        message_id = request.GET.get("message_id")

        if (
            not team_id
            or not chat_type
            or not is_thread_binary
            or not chat_id
            or not message_id
            or not thread_id
        ):
            return Response(
                {
                    "error": "team_id, chat_type, is_thread_binary, chat_id, thread_id, and message_id are required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        mentions = MentionFact.objects.filter(
            team_id=team_id,
            chat_type=chat_type,
            chat_id=chat_id,
            is_thread=is_thread,
            thread_id=thread_id,
            message_id=message_id,
        ).values()

        mentioned_user_ids = []
        for mention in mentions:
            mentioned_user_ids.append(mention["mentioned_user_id"])

        return Response(mentioned_user_ids, status=status.HTTP_200_OK)

    def delete(self, request):
        team_id = request.GET.get("team_id")
        chat_type = request.GET.get("chat_type")
        chat_id = request.GET.get("chat_id")
        is_thread_binary = request.GET.get("is_thread_binary")
        is_thread = int(is_thread_binary) == 1
        thread_id = request.GET.get("thread_id")
        message_id = request.GET.get("message_id")
        mentioned_user_ids = request.GET.get("mentioned_user_ids")

        if (
            not team_id
            or not mentioned_user_ids
            or not chat_type
            or not chat_id
            or not is_thread_binary
            or not thread_id
            or not message_id
        ):
            return Response(
                {
                    "error": "`team_id`, `mentioned_user_ids`, `chat_type`, `chat_id`, `message_id`, `thread_id`, and `is_thread_binary` are required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            for mentioned_user_id in list(str(mentioned_user_ids).split(",")):
                reaction = MentionFact.objects.get(
                    team=team_id,
                    chat_type=int(chat_type),
                    chat_id=int(chat_id),
                    is_thread=is_thread,
                    thread_id=thread_id,
                    message_id=message_id,
                    mentioned_user=mentioned_user_id,
                )
                reaction.delete()
            return Response(
                {"message": f"Mention deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except MentionFact.DoesNotExist:
            return Response(
                {"error": "Mention not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
