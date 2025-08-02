from django.db.models import Max
from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.common.reaction_models import *
from origin.serializers.common.reaction_serializers import *


class ReactionView(AuthenticatedAPIView):
    def post(self, request):

        is_thread = int(request.data["is_thread_binary"]) == 1

        current_max_reaction_id = ReactionFact.objects.filter(
            team_id=request.data["team_id"],
            chat_type=request.data["chat_type"],
            chat_id=request.data["chat_id"],
            message_id=request.data["message_id"],
            is_thread=is_thread,
        ).aggregate(max_id=Max("reaction_id"))["max_id"]

        data = {
            "team": request.data["team_id"],
            "chat_type": request.data["chat_type"],
            "chat_id": request.data["chat_id"],
            "message_id": int(request.data["message_id"]),
            "is_thread": is_thread,
            "reaction_id": current_max_reaction_id + 1 if current_max_reaction_id else 1,
            "reaction_emoji": request.data["reaction_emoji"],
            "sender": request.data["sender_id"],
        }

        serializer = ReactionFactSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        team_id = request.GET.get("team_id")
        sender_id = request.GET.get("sender_id")
        chat_type = request.GET.get("chat_type")
        chat_id = request.GET.get("chat_id")
        message_id = int(request.GET.get("message_id"))
        is_thread_binary = request.GET.get("is_thread_binary")
        reaction_emoji = request.GET.get("reaction_emoji")

        if (
            not team_id
            or not sender_id
            or not chat_type
            or not chat_id
            or not message_id
            or not is_thread_binary
            or not reaction_emoji
        ):
            return Response(
                {
                    "error": "`team_id`, `sender_id`, `chat_type`, `chat_id`, `message_id`, `is_thread_binary`, and `reaction_emoji` are required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            reaction = ReactionFact.objects.get(
                team=team_id,
                sender=sender_id,
                chat_type=int(chat_type),
                chat_id=int(chat_id),
                message_id=int(message_id),
                is_thread=int(is_thread_binary) == 1,
                reaction_emoji=reaction_emoji,
            )
            reaction.delete()
            return Response(
                {"message": f"Reaction deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except ReactionFact.DoesNotExist:
            return Response(
                {"error": "Reaction not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
