from collections import defaultdict
from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.gm_models import GMMaster, GMMembers, GMMessages, GMThreadMessages
from origin.serializers.chat.gm_serializers import (
    GMMasterSerializer,
    GMMembersSerializer,
    GMMessagesSerializer,
    GMThreadMessagesSerializer,
)


#############################
# GM Master views
#############################
class GMMasterView(AuthenticatedAPIView):
    def post(self, request):
        owner_team = request.data.get("owner_team", None)
        group_name = request.data.get("group_name", None)

        if not group_name:
            return Response(
                {"error": "Both group_name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a GM exists in any order
        exists = GMMaster.objects.filter(
            Q(owner_team=owner_team, group_name=group_name)
        ).values_list("gm_id", flat=True)

        if len(exists) == 0:
            serializer = GMMasterSerializer(data=request.data)
            if serializer.is_valid():
                serializer.save()
                data = {
                    "chatName": serializer.data["group_name"],
                    "chatId": serializer.data["gm_id"],
                    "message": "Completed GM creation",
                }
                return Response(data, status=status.HTTP_201_CREATED)
            # Extract error messages and convert them into a string
            error_messages = " ".join(
                [f"{field}: {' '.join(errors)}" for field, errors in serializer.errors.items()]
            )
            return Response({"message": error_messages}, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response({"gm_exists": True, "gm_id": exists[0]}, status=status.HTTP_200_OK)


class CheckGMExistsView(AuthenticatedAPIView):
    def get(self, request):
        gm_id = request.GET.get("gm_id", None)

        if not gm_id:
            return Response(
                {"error": "Both gm_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a GM exists in any order
        exists = GMMaster.objects.filter(Q(gm_id=gm_id)).exists()

        return Response({"gm_exists": exists}, status=status.HTTP_200_OK)


class GetGMIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id", None)
        group_name = request.GET.get("group_name", None)

        if not team_id or not group_name:
            return Response(
                {"error": "Both team_id and group_name are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gm = GMMaster.objects.filter(Q(owner_team=team_id, group_name=group_name)).values_list(
            "gm_id", flat=True
        )

        if len(gm) == 1:
            return Response({"gm_id": gm[0]}, status=status.HTTP_200_OK)
        else:
            return Response({"gm_id": None}, status=status.HTTP_200_OK)


class GMMembersView(AuthenticatedAPIView):
    def post(self, request):
        data = {"gm": request.data["gm_id"], "attendee": request.data["attendee_id"]}

        already_joined = GMMembers.objects.filter(
            Q(gm_id=data["gm"], attendee_id=data["attendee"])
        ).exists()

        if already_joined:
            return Response(data, status=status.HTTP_201_CREATED)
        else:
            serializer = GMMembersSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GetAllMyGMIdsView(AuthenticatedAPIView):
    def get(self, request):
        attendee_id = request.GET.get("attendee_id")

        if not attendee_id:
            return Response(
                {"error": "attendee_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gm_ids = GMMembers.objects.filter(Q(attendee=attendee_id)).values_list("gm")

        connected_set = set()
        for (group_id,) in gm_ids:
            connected_set.add(group_id)

        return Response({"gm_ids": list(connected_set)}, status=status.HTTP_200_OK)


#############################
# GM Messages views
#############################
class GMAllMyMessagesView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        attendee_id = request.GET.get("user_id")

        if not attendee_id:
            return Response(
                {"error": "team_id, team_name and attendee_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch all gm_ids linked to the user
        gm_ids = list(
            GMMembers.objects.filter(Q(attendee=attendee_id)).values_list("gm_id", flat=True)
        )

        if not gm_ids:
            return Response({"messages": []}, status=status.HTTP_200_OK)

        # Fetch all messages where the gm_id matches and the user is involved
        raw_messages = GMMessages.objects.filter(gm_id__in=gm_ids)

        # Group by dm_id and parent_message_id, then count the replies in each group
        thread_reply_counts = GMThreadMessages.objects.values(
            "parent_message_uid__gm__gm_id", "parent_message_uid__message_id"
        ).annotate(num_of_replies=Count("thread_message_id"))

        thread_reply_count_map = {}
        for reply_count_info in thread_reply_counts:
            gm_id = reply_count_info["parent_message_uid__gm__gm_id"]
            message_id = reply_count_info["parent_message_uid__message_id"]
            reply_count = reply_count_info["num_of_replies"]
            thread_reply_count_map[f"{gm_id}-{message_id}"] = reply_count

        message_history_dict = {}
        last_message_dict = {}
        ts_last_message_dict = {}
        for raw_message in raw_messages:
            chat_id = int(raw_message.gm.gm_id)
            chat_name = str(raw_message.gm.group_name)
            message_id = int(raw_message.message_id)
            content = raw_message.message_body
            sender_id = str(raw_message.sender.id)
            sender_name = str(raw_message.sender.username)
            sender_email = str(raw_message.sender.email)
            sender_avatar_img_path = raw_message.sender.profile_image_url
            ts_sent = str(raw_message.ts_sent_at)

            messageIdWithChatId = f"{chat_id}-{message_id}"
            new_message = {
                "messageIdWithChatId": messageIdWithChatId,
                "chatId": chat_id,
                "messageId": message_id,
                "content": content,
                "sender": {
                    "teamId": team_id,
                    "teamName": team_name,
                    "userName": sender_name,
                    "userEmail": sender_email,
                    "userId": sender_id,
                    "avatar_img_path": sender_avatar_img_path,
                },
                "numReplies": thread_reply_count_map.get(
                    f"{raw_message.gm.gm_id}-{message_id}", None
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

            try:
                # TODO: Need to consider the case that the first line
                # (i.e., message_body[0]) is empty but later exists.
                latest_message_text = last_message_dict[chat_id]["content"][0]["content"][-1][
                    "text"
                ]
            except:
                print("gm_views", last_message_dict[chat_id]["content"])
                latest_message_text = "Failed to get text..."

            if chat_id in message_history_dict:
                message_history_dict[chat_id]["messages"].append(new_message)
                message_history_dict[chat_id]["latestMessage"] = last_message_dict[chat_id]
                message_history_dict[chat_id]["latestMessageText"] = latest_message_text
                message_history_dict[chat_id]["TSLastMessage"] = ts_last_message_dict[chat_id]
            else:
                message_history_dict[chat_id] = {
                    "chatId": chat_id,
                    "chatName": chat_name,
                    "isDm": False,
                    "dmPartnerUser": {"userName": "", "userId": "", "avatarImgPath": ""},
                    "messages": [new_message],
                    "latestMessage": last_message_dict[chat_id],
                    "latestMessageText": latest_message_text,
                    "TSLastMessage": ts_last_message_dict[chat_id],
                }

        message_history = list(message_history_dict.values())

        return Response(message_history, status=status.HTTP_200_OK)


class GMSingleMessageView(AuthenticatedAPIView):
    def post(self, request):
        gm = GMMaster.objects.filter(gm_id=request.data["gm_id"])
        if len(gm) > 0:
            current_message_count = GMMessages.objects.filter(gm=gm[0]).count()
        else:
            current_message_count = 0

        is_init = request.data.get("is_init")
        if (is_init in [None, False]) or (is_init == True and current_message_count == 0):
            data = {
                "gm": request.data["gm_id"],
                "sender": request.data["sender_id"],
                "message_id": current_message_count + 1,
                "message_body": request.data["message_body"],
            }
            serializer = GMMessagesSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response(
                {"message": "Nothing to do cause it's already initialized"},
                status=status.HTTP_201_CREATED,
            )


class GMMessagesByIdView(AuthenticatedAPIView):
    def get(self, request):
        gm_id = request.GET.get("gm_id", None)
        if gm_id:
            messages = GMMessages.objects.filter(gm_id=int(gm_id))
            serializer = GMMessagesSerializer(messages, many=True)  # Serialize data
            return Response(serializer.data)  # Return JSON response
        else:
            return Response("gm_id is not found", status=status.HTTP_400_BAD_REQUEST)


#############################
# GM Thread Messages views
#############################
class CheckGMThreadExistsView(AuthenticatedAPIView):
    def get(self, request):
        gm_id = request.GET.get("gm_id", None)
        thread_id = request.GET.get("thread_id", None)

        if not gm_id or not thread_id:
            return Response(
                {"error": "Both gm_id and thread_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a DM exists in any order
        exists = GMThreadMessages.objects.filter(Q(gm=gm_id, thread_id=thread_id)).exists()

        return Response({"gm_thread_exists": exists}, status=status.HTTP_200_OK)


class GMSingleThreadMessageView(AuthenticatedAPIView):
    def post(self, request):
        gm = GMMaster.objects.filter(gm_id=request.data["gm_id"])
        if len(gm) > 0:
            current_thread_message_count = GMThreadMessages.objects.filter(
                gm=gm[0], thread_id=request.data["thread_id"]
            ).count()
        else:
            Response("gm is not found", status=status.HTTP_400_BAD_REQUEST)

        data = {
            "gm": request.data["gm_id"],
            "thread_id": request.data["thread_id"],
            "sender": request.data["sender_id"],
            "thread_message_id": current_thread_message_count + 1,
            "thread_message_body": request.data["message_body"],
            "parent_message_uid": "{gm_id}-{parent_message_id}".format(
                gm_id=request.data["gm_id"], parent_message_id=request.data["parent_message_id"]
            ),
            "task": request.data["task"],
        }

        serializer = GMThreadMessagesSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GMThreadMessagesByIdView(AuthenticatedAPIView):
    def get(self, request):
        gm_id = request.GET.get("gm_id", None)
        thread_id = request.GET.get("thread_id", None)
        print(gm_id, thread_id)
        if gm_id and thread_id:
            messages = GMThreadMessages.objects.filter(gm_id=int(gm_id), thread_id=int(thread_id))
            serializer = GMThreadMessagesSerializer(messages, many=True)  # Serialize data
            return Response(serializer.data)  # Return JSON response
        else:
            return Response(
                "gm_id and/or thread_id is not found", status=status.HTTP_400_BAD_REQUEST
            )
