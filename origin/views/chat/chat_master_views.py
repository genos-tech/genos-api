from rest_framework.response import Response
from rest_framework import status
from django.db.models import F

from origin.models.chat.chat_master_models import UserChatMaster
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.serializers.chat.chat_master_serializers import *
from origin.views.utils.request_validators import validate_request_data, validate_request_user


class UserChatMasterView(AuthenticatedAPIView):
    def put(self, request):
        data = {
            "team": request.data.get("team"),
            "user": request.data.get("user"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user"])):
            return res

        try:
            old_chat_master = UserChatMaster.objects.get(team=data["team"], user=data["user"])
            serializer = UserChatMasterSerializer(old_chat_master, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
        except:
            # Insert if not exists
            serializer = UserChatMasterSerializer(data=request.data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        data = {
            "team": request.GET.get("team"),
            "user": request.GET.get("user"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user"])):
            return res

        chat_master = (
            UserChatMaster.objects.filter(team=data["team"], user=data["user"])
            .annotate(
                pinnedChats=F("pinned_chats"),
                tsLastAllReadActivity=F("ts_last_all_read_activity"),
            )
            .values(
                "pinnedChats",
                "tsLastAllReadActivity",
            )
        )

        return Response(chat_master, status=status.HTTP_200_OK)
