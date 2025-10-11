from rest_framework.response import Response
from rest_framework import status
from django.db.models import F

from origin.models.chat.chat_master_models import UserChatMaster
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.serializers.chat.chat_master_serializers import *
from origin.views.utils.request_validators import validate_request_data, validate_request_user


class UserChatMasterView(AuthenticatedAPIView):
    def _toggle_list_item(self, current_list, item):
        """
        Helper method to toggle an item in a list.
        If item exists, remove it. If it doesn't exist, add it.
        """
        if current_list is None:
            current_list = []

        # Use set for O(1) lookup instead of O(n) list lookup for large lists
        current_set = set(str(i) if isinstance(i, dict) else i for i in current_list)
        item_str = str(item) if isinstance(item, dict) else item

        if item_str in current_set:
            return [i for i in current_list if (str(i) if isinstance(i, dict) else i) != item_str]
        else:
            return current_list + [item]

    def put(self, request):
        # Cache frequently accessed request data
        team = request.data.get("team")
        user = request.data.get("user")
        flagged_message = request.data.get("flagged_message")
        pinned_chat = request.data.get("pinned_chat")

        data = {"team": team, "user": user}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(user)):
            return res

        # Use get_or_create for better performance instead of try/except
        chat_master, created = UserChatMaster.objects.get_or_create(
            team=team, user=user, defaults={"flagged_messages": [], "pinned_chats": []}
        )

        # Prepare update data - don't modify request.data directly
        update_data = {}

        # Handle flagged messages toggle
        if flagged_message is not None:
            current_flagged = chat_master.flagged_messages or []
            update_data["flagged_messages"] = self._toggle_list_item(
                current_flagged, flagged_message
            )

        # Handle pinned chats toggle
        if pinned_chat is not None:
            current_pinned = chat_master.pinned_chats or []
            update_data["pinned_chats"] = self._toggle_list_item(current_pinned, pinned_chat)

        # Only proceed with serialization if there's data to update
        if update_data:
            # Merge request data with our computed update data
            serializer_data = {**request.data, **update_data}
            serializer = UserChatMasterSerializer(chat_master, data=serializer_data, partial=True)

            if serializer.is_valid():
                serializer.save()
                return Response(
                    serializer.data,
                    status=status.HTTP_200_OK if not created else status.HTTP_201_CREATED,
                )
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # If no updates needed, return current data
        serializer = UserChatMasterSerializer(chat_master)
        return Response(serializer.data, status=status.HTTP_200_OK)

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
                flaggedMessages=F("flagged_messages"),
                pinnedChats=F("pinned_chats"),
                tsLastAllReadActivity=F("ts_last_all_read_activity"),
            )
            .values(
                "flaggedMessages",
                "pinnedChats",
                "tsLastAllReadActivity",
            )
        )

        return Response(chat_master, status=status.HTTP_200_OK)
