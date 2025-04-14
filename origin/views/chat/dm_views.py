from collections import defaultdict
from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.dm_models import DMMaster, UserDMMapping, DMMessages, DMThreadMessages
from origin.serializers.chat.dm_serializers import (
    DMMasterSerializer,
    DMMessagesSerializer,
    DMThreadMessagesSerializer,
)


#############################
# DM Master views
#############################
class DMMasterView(AuthenticatedAPIView):
    def post(self, request):
        user_1_id = request.data.get("user_1_id", None)
        user_2_id = request.data.get("user_2_id", None)

        if not user_1_id or not user_2_id:
            return Response(
                {"error": "Both user_1_id and user_2_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a DM exists in any order
        exists = DMMaster.objects.filter(
            Q(user_1_id=user_1_id, user_2_id=user_2_id)
            | Q(user_1_id=user_2_id, user_2_id=user_1_id)
        ).values_list("dm_id", flat=True)

        if len(exists) == 0:
            serializer = DMMasterSerializer(data=request.data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)
        elif len(exists) == 1:
            return Response({"dm_exists": True, "dm_id": exists[0]}, status=status.HTTP_200_OK)
        else:
            return Response(
                {"dm_exists": True, "dm_id": None, "error": "Duplicated DMs found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class CheckDMExistsView(AuthenticatedAPIView):
    def get(self, request):
        user_1_id = request.GET.get("user_1_id", None)
        user_2_id = request.GET.get("user_2_id", None)

        if not user_1_id or not user_2_id:
            return Response(
                {"error": "Both user_1_id and user_2_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a DM exists in any order
        exists = DMMaster.objects.filter(
            Q(user_1_id=user_1_id, user_2_id=user_2_id)
            | Q(user_1_id=user_2_id, user_2_id=user_1_id)
        ).values_list("dm_id", flat=True)

        if exists == []:
            return Response({"dm_exists": False, "dm_id": None}, status=status.HTTP_200_OK)
        elif len(exists) == 1:
            return Response({"dm_exists": True, "dm_id": exists[0]}, status=status.HTTP_200_OK)
        else:
            return Response(
                {"dm_exists": True, "dm_id": None, "error": "Duplicated DMs found"},
                status=status.HTTP_400_BAD_REQUEST,
            )


class GetDMIdView(AuthenticatedAPIView):
    def get(self, request):
        user_1_id = request.GET.get("user_1_id", None)
        user_2_id = request.GET.get("user_2_id", None)

        if not user_1_id or not user_2_id:
            return Response(
                {"error": "Both user_1_id and user_2_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a DM exists in any order
        dm = DMMaster.objects.filter(
            Q(user_1_id=user_1_id, user_2_id=user_2_id)
            | Q(user_1_id=user_2_id, user_2_id=user_1_id)
        ).values_list("dm_id", flat=True)

        if len(dm) == 1:
            return Response({"dm_id": dm[0]}, status=status.HTTP_200_OK)
        else:
            return Response({"dm_id": None}, status=status.HTTP_200_OK)


class GetAllMyDMIdsView(AuthenticatedAPIView):
    def get(self, request):
        user_id = request.GET.get("user_id")

        if not user_id:
            return Response(
                {"error": "user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        dm_ids = UserDMMapping.objects.filter(user_id=user_id).values_list("dm_id", flat=True)

        return Response({"dm_ids": list(dm_ids)}, status=status.HTTP_200_OK)


#############################
# DM Messages views
#############################
class DMAllMyMessagesView(AuthenticatedAPIView):
    def get(self, request):
        user_id = request.GET.get("user_id")

        if not user_id:
            return Response(
                {"error": "user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch all dm_ids linked to the user
        dm_ids = list(
            UserDMMapping.objects.filter(user_id=user_id).values_list("dm_id", flat=True)
        )

        if not dm_ids:
            return Response({"messages": []}, status=status.HTTP_200_OK)

        # Fetch all messages where the dm_id matches and the user is involved
        raw_messages = DMMessages.objects.filter(dm_id__in=dm_ids)

        # Group by dm_id and parent_message_id, then count the replies in each group
        thread_reply_counts = DMThreadMessages.objects.values(
            "parent_message_uid__dm__dm_id", "parent_message_uid__message_id"
        ).annotate(num_of_replies=Count("thread_message_id"))

        thread_reply_count_map = {}
        for reply_count_info in thread_reply_counts:
            dm_id = reply_count_info["parent_message_uid__dm__dm_id"]
            message_id = reply_count_info["parent_message_uid__message_id"]
            reply_count = reply_count_info["num_of_replies"]
            thread_reply_count_map[f"{dm_id}-{message_id}"] = reply_count

        message_history_dict = {}
        last_message_dict = {}
        ts_last_message_dict = {}
        for raw_message in raw_messages:
            chat_id = int(raw_message.dm.dm_id)
            sender_id = str(raw_message.sender.id)
            sender_name = str(raw_message.sender.username)
            receiver_id = str(raw_message.receiver.id)
            receiver_name = str(raw_message.receiver.username)
            message_id = int(raw_message.message_id)
            content = str(raw_message.message_body)
            ts_sent = str(raw_message.ts_sent_at)

            if sender_id == user_id:
                dm_partner_user_id = receiver_id
                chat_name = receiver_name
            else:
                dm_partner_user_id = sender_id
                chat_name = sender_name

            messageIdWithChatId = f"{chat_id}-{message_id}"
            new_message = {
                "messageIdWithChatId": messageIdWithChatId,
                "chatId": chat_id,
                "messageId": message_id,
                "content": content,
                "sender": {
                    "userName": sender_name,
                    "userId": sender_id,
                    "avatarImgPath": f"/path/to/user/{chat_id}.jpg",
                },
                "numReplies": thread_reply_count_map.get(
                    f"{raw_message.dm.dm_id}-{message_id}", None
                ),
                "tsSent": ts_sent,
            }

            if chat_id in ts_last_message_dict:
                prev_ts_last_message = ts_last_message_dict[chat_id]
                if ts_sent > prev_ts_last_message:
                    last_message_dict[chat_id] = new_message
                    ts_last_message_dict[chat_id] = ts_sent
            else:
                last_message_dict[chat_id] = new_message
                ts_last_message_dict[chat_id] = ts_sent

            if chat_id in message_history_dict:
                message_history_dict[chat_id]["messages"].append(new_message)
                message_history_dict[chat_id]["latestMessage"] = last_message_dict[chat_id]
                message_history_dict[chat_id]["TSLastMessage"] = ts_last_message_dict[chat_id]
            else:
                message_history_dict[chat_id] = {
                    "chatId": chat_id,
                    "chatName": chat_name,
                    "isDm": True,
                    "dmPartnerUserId": dm_partner_user_id,
                    "messages": [new_message],
                    "latestMessage": last_message_dict[chat_id],
                    "TSLastMessage": ts_last_message_dict[chat_id],
                }

        message_history = list(message_history_dict.values())

        return Response({"messageHistory": message_history}, status=status.HTTP_200_OK)


class DMSingleMessageView(AuthenticatedAPIView):
    def post(self, request):
        dm = DMMaster.objects.filter(dm_id=request.data["dm_id"])
        if len(dm) > 0:
            current_message_count = DMMessages.objects.filter(dm=dm[0]).count()
        else:
            current_message_count = 0

        is_init = request.data.get("is_init")
        if (is_init in [None, False]) or (is_init == True and current_message_count == 0):
            data = {
                "dm": request.data["dm_id"],
                "sender": request.data["sender_id"],
                "receiver": request.data["receiver_id"],
                "message_id": current_message_count + 1,
                "message_body": request.data["message_body"],
            }
            serializer = DMMessagesSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response(
                {"message": "Nothing to do cause it's already initialized"},
                status=status.HTTP_201_CREATED,
            )


class DMMessagesByIdView(AuthenticatedAPIView):
    def get(self, request):
        dm_id = request.GET.get("dm_id", None)
        if dm_id:
            messages = DMMessages.objects.filter(dm_id=int(dm_id))
            serializer = DMMessagesSerializer(messages, many=True)  # Serialize data
            return Response(serializer.data)  # Return JSON response
        else:
            return Response("dm_id is not found", status=status.HTTP_400_BAD_REQUEST)


#############################
# DM Thread Messages views
#############################
class CheckDMThreadExistsView(AuthenticatedAPIView):
    def get(self, request):
        dm_id = request.GET.get("dm_id", None)
        thread_id = request.GET.get("thread_id", None)

        if not dm_id or not thread_id:
            return Response(
                {"error": "Both dm_id and thread_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a DM exists in any order
        exists = DMThreadMessages.objects.filter(Q(dm=dm_id, thread_id=thread_id)).exists()

        return Response({"dm_thread_exists": exists}, status=status.HTTP_200_OK)


class DMSingleThreadMessageView(AuthenticatedAPIView):
    def post(self, request):
        dm = DMMaster.objects.filter(dm_id=request.data["dm_id"])
        if len(dm) > 0:
            current_thread_message_count = DMThreadMessages.objects.filter(
                dm=dm[0], thread_id=request.data["thread_id"]
            ).count()
        else:
            Response("dm is not found", status=status.HTTP_400_BAD_REQUEST)

        data = {
            "dm": request.data["dm_id"],
            "thread_id": request.data["thread_id"],
            "sender": request.data["sender_id"],
            "receiver": request.data["receiver_id"],
            "thread_message_id": current_thread_message_count + 1,
            "thread_message_body": request.data["message_body"],
            "parent_message_uid": "{dm_id}-{parent_message_id}".format(
                dm_id=request.data["dm_id"], parent_message_id=request.data["parent_message_id"]
            ),
            "task": request.data["task"],
        }

        serializer = DMThreadMessagesSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DMThreadMessagesByIdView(AuthenticatedAPIView):
    def get(self, request):
        dm_id = request.GET.get("dm_id", None)
        thread_id = request.GET.get("thread_id", None)

        if dm_id and thread_id:
            messages = DMThreadMessages.objects.filter(dm_id=int(dm_id), thread_id=int(thread_id))
            serializer = DMThreadMessagesSerializer(messages, many=True)  # Serialize data
            return Response(serializer.data)  # Return JSON response
        else:
            return Response(
                "dm_id and/or thread_id is not found", status=status.HTTP_400_BAD_REQUEST
            )
