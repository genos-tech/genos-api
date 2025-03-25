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
        serializer = GMMasterSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            data = {
                "chatName": serializer.data["group_name"],
                "chatEmail": serializer.data["group_email"],
                "gmId": serializer.data["gm_id"],
                "message": "Completed GM creation",
            }
            return Response(data, status=status.HTTP_201_CREATED)
        # Extract error messages and convert them into a string
        error_messages = " ".join(
            [f"{field}: {' '.join(errors)}" for field, errors in serializer.errors.items()]
        )
        return Response({"message": error_messages}, status=status.HTTP_400_BAD_REQUEST)


class CheckGMExistsView(AuthenticatedAPIView):
    def get(self, request):
        group_email = request.GET.get("group_email", None)

        if not group_email:
            return Response(
                {"error": "Both group_email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a GM exists in any order
        exists = GMMaster.objects.filter(Q(group_email=group_email)).exists()

        return Response({"gm_exists": exists}, status=status.HTTP_200_OK)


class GetGMIdView(AuthenticatedAPIView):
    def get(self, request):
        group_email = request.GET.get("group_email", None)

        if not group_email:
            return Response(
                {"error": "group_email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gm = GMMaster.objects.filter(Q(group_email=group_email)).values_list("gm_id", flat=True)

        if len(gm) == 1:
            return Response({"gm_id": gm[0]}, status=status.HTTP_200_OK)
        else:
            return Response({"gm_id": None}, status=status.HTTP_200_OK)


class GMMembersView(AuthenticatedAPIView):
    def post(self, request):
        data = {"gm": request.data["gm_id"], "attendee": request.data["attendee_email"]}
        serializer = GMMembersSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GetAllMyGMIdsView(AuthenticatedAPIView):
    def get(self, request):
        attendee_email = request.GET.get("attendee_email")

        if not attendee_email:
            return Response(
                {"error": "attendee_email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch emails that are connected with the given email
        gm_ids = GMMembers.objects.filter(Q(attendee=attendee_email)).values_list("gm")

        connected_set = set()
        for (group_id,) in gm_ids:
            connected_set.add(group_id)

        return Response({"gm_ids": list(connected_set)}, status=status.HTTP_200_OK)


class GetAllMyGMEmailsView(AuthenticatedAPIView):
    def get(self, request):
        attendee_email = request.GET.get("attendee_email")

        if not attendee_email:
            return Response(
                {"error": "attendee_email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch emails that are connected with the given email
        gm_ids = GMMembers.objects.filter(Q(attendee=attendee_email)).values_list("gm", flat=True)
        gm_emails = GMMaster.objects.filter(Q(gm_id__in=gm_ids)).values_list(
            "group_email", flat=True
        )

        return Response({"gm_emails": list(set(gm_emails))}, status=status.HTTP_200_OK)


#############################
# GM Messages views
#############################
class GMAllMyMessagesView(AuthenticatedAPIView):
    def get(self, request):
        attendee_email = request.GET.get("user_email")

        if not attendee_email:
            return Response(
                {"error": "attendee_email is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch all gm_ids linked to the user
        gm_ids = list(
            GMMembers.objects.filter(Q(attendee=attendee_email)).values_list("gm_id", flat=True)
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
            chat_group_email = str(raw_message.gm.group_email)
            chat_group_name = str(raw_message.gm.group_name)
            message_id = str(raw_message.message_id)
            content = str(raw_message.message_body)
            sender_email = str(raw_message.sender.email)
            sender_name = str(raw_message.sender.username)
            ts_sent = str(raw_message.ts_sent_at)

            messageIdWithChatEmail = f"{chat_group_email}-{message_id}"
            new_message = {
                "messageIdWithChatEmail": messageIdWithChatEmail,
                "messageId": message_id,
                "chatEmail": chat_group_email,
                "content": content,
                "sender": {
                    "userName": sender_name,
                    "userEmail": sender_email,
                    "avatar_img_path": f"/path/to/user/{chat_group_email}.jpg",
                },
                "numReplies": thread_reply_count_map.get(
                    f"{raw_message.gm.gm_id}-{message_id}", None
                ),
                "tsSent": ts_sent,
            }

            if chat_group_email in ts_last_message_dict:
                prev_ts_last_message = ts_last_message_dict[chat_group_email]
                if ts_sent > prev_ts_last_message:
                    last_message_dict[chat_group_email] = new_message
                    ts_last_message_dict[chat_group_email] = ts_sent
            else:
                last_message_dict[chat_group_email] = new_message
                ts_last_message_dict[chat_group_email] = ts_sent

            if chat_group_email in message_history_dict:
                message_history_dict[chat_group_email]["messages"].append(new_message)
                message_history_dict[chat_group_email]["latestMessage"] = last_message_dict[
                    chat_group_email
                ]
                message_history_dict[chat_group_email]["TSLastMessage"] = ts_last_message_dict[
                    chat_group_email
                ]
            else:
                message_history_dict[chat_group_email] = {
                    "chatEmail": chat_group_email,
                    "chatName": chat_group_name,
                    "messages": [new_message],
                    "latestMessage": last_message_dict[chat_group_email],
                    "TSLastMessage": ts_last_message_dict[chat_group_email],
                }

        message_history = list(message_history_dict.values())

        return Response({"messageHistory": message_history}, status=status.HTTP_200_OK)


class GMSingleMessageView(AuthenticatedAPIView):
    def post(self, request):
        gm = GMMaster.objects.filter(gm_id=request.data["gm_id"])
        if len(gm) > 0:
            current_message_count = GMMessages.objects.filter(gm=gm[0]).count()
        else:
            current_message_count = 0

        data = {
            "gm": request.data["gm_id"],
            "sender": request.data["sender_email"],
            "message_id": current_message_count + 1,
            "message_body": request.data["message_body"],
        }

        serializer = GMMessagesSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


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
            "sender": request.data["sender_email"],
            "thread_message_id": current_thread_message_count + 1,
            "thread_message_body": request.data["message_body"],
            "parent_message_uid": "{gm_id}-{parent_message_id}".format(
                gm_id=request.data["gm_id"], parent_message_id=request.data["parent_message_id"]
            ),
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
