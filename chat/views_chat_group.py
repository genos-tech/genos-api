from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated

from .serializer import ChatGroupSerializer, ChatGroupMemberSerializer
from .models import ChatGroup, ChatGroupMember
from datetime import datetime
import pytz

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
        response=add_user_to_chat_group(chat_group.id_chat_group, chat_group.id_owner)
        if response.status_code!=201:
            return response
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

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def join_chat_group(request):
    data=request.data
    if "st_chat_group_name" not in data:
        return Response({"message":"Missing field: 'st_chat_group_name'"}, status=status.HTTP_400_BAD_REQUEST)
    cg=ChatGroup.objects.filter(st_chat_group_name=data["st_chat_group_name"])
    if len(cg)==0:
        return Response({"message":"Chat group not found"}, status=status.HTTP_400_BAD_REQUEST)
    return add_user_to_chat_group(cg[0].id_chat_group,request.user.id_user)


def add_user_to_chat_group(id_chat_group, id_user) -> Response:
    cgm_data={}
    cgm_data["id_chat_group"]=id_chat_group
    cgm_data["id_user"]=id_user
    cgm_data["dt_last_read"]=datetime.now(tz=pytz.utc).isoformat()
    cgm_serializer = ChatGroupMemberSerializer(data=cgm_data)
    if cgm_serializer.is_valid():
        cgm = cgm_serializer.save()
        return Response(
            {
                "message": "User added to Chat Group!",
                "chat_group_member": {
                    "id_chat_group": cgm.id_chat_group,
                    "id_user": cgm.id_user,
                    "dt_last_read": cgm.dt_last_read
                },
            },
            status=status.HTTP_201_CREATED,
        )
    return Response(cgm_serializer.error_messages, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_user_chat_groups(request):
    """
    Was api_myChatGroups
    """
    id_user=request.user.id_user
    chat_groups=ChatGroup.objects.filter(
        id_chat_group__in=ChatGroupMember.objects.filter(id_user=id_user).values('id_chat_group')
    )
    data=[]
    for chat_group in chat_groups:
        sub_data={}
        sub_data["id_chat_group"]=chat_group.id_chat_group
        sub_data["st_chat_group_name"]=chat_group.st_chat_group_name
        sub_data["id_owner"]=chat_group.id_owner
        data.append(sub_data)
    return Response(data=data, status=status.HTTP_200_OK)