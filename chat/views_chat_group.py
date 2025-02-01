from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .serializer import ChatGroupSerializer
from .models import ChatGroup


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_chat_group(request):
    data = request.data
    if ChatGroup.objects.filter(st_chat_group_name=data["st_chat_group_name"]).exists():
        return Response(
            {"message": "A chat group with this name already exists."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # Extract id_user from JWT
    data["id_owner"] = request.user.id_user
    serializer = ChatGroupSerializer(data=data)
    if serializer.is_valid():
        chat_group = serializer.save()
        return Response(
            {
                "message": "Chat Group created!",
                "chat_group": {
                    "id_chat_group": chat_group.id_chat_group,
                    "st_chat_group_name": chat_group.st_chat_group_name,
                },
            },
            status=status.HTTP_201_CREATED,
        )
    return Response(serializer.error_messages, status=status.HTTP_400_BAD_REQUEST)
