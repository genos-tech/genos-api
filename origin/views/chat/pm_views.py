from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.project.prj_models import ProjectMembers, ProjectMaster
from origin.models.chat.pm_models import PMMessages, PMThreadMessages
from origin.serializers.chat.pm_serializers import (
    PMMessagesSerializer,
    PMThreadMessagesSerializer,
)


#############################
# PM Messages views
#############################
class PMHistoryView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        attendee_id = request.GET.get("user_id")

        if not attendee_id:
            return Response(
                {"error": "team_id, team_name and attendee_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch all project_ids linked to the user
        project_ids = list(
            ProjectMembers.objects.filter(Q(team=team_id, attendee=attendee_id)).values_list(
                "project_id", flat=True
            )
        )

        if not project_ids:
            return Response({"messages": []}, status=status.HTTP_200_OK)

        # Fetch all messages where the project_id matches and the user is involved
        raw_messages = PMMessages.objects.filter(project__in=project_ids)

        # Group by dm_id and parent_message_id, then count the replies in each group
        thread_reply_counts = PMThreadMessages.objects.values(
            "parent_message_uid__project__project_id", "parent_message_uid__message_id"
        ).annotate(num_of_replies=Count("thread_message_id"))

        thread_reply_count_map = {}
        for reply_count_info in thread_reply_counts:
            project_id = reply_count_info["parent_message_uid__project__project_id"]
            message_id = reply_count_info["parent_message_uid__message_id"]
            reply_count = reply_count_info["num_of_replies"]
            thread_reply_count_map[f"{project_id}-{message_id}"] = reply_count

        message_history_dict = {}
        last_message_dict = {}
        ts_last_message_dict = {}
        for raw_message in raw_messages:
            chat_id = int(raw_message.project.project_id)
            chat_name = str(raw_message.project.project_name)
            project_system_user_id = str(raw_message.project.project_system_user.id)
            message_id = int(raw_message.message_id)
            content = raw_message.message_body
            sender_id = str(raw_message.sender.id)
            sender_name = str(raw_message.sender.username)
            sender_email = str(raw_message.sender.email)
            sender_avatar_img_path = raw_message.sender.profile_image_url
            is_system_user = raw_message.sender.is_system_user
            ts_sent = str(raw_message.ts_sent_at)

            messageIdWithChatId = f"{chat_id}-{message_id}"
            new_message = {
                "messageIdWithChatId": messageIdWithChatId,
                "chatId": chat_id,
                "systemUserId": project_system_user_id,
                "messageId": message_id,
                "content": content,
                "sender": {
                    "teamId": team_id,
                    "teamName": team_name,
                    "userName": sender_name,
                    "userEmail": sender_email,
                    "userId": sender_id,
                    "avatarImgPath": sender_avatar_img_path,
                    "isSystemUser": is_system_user,
                },
                "numReplies": thread_reply_count_map.get(
                    f"{raw_message.project.project_id}-{message_id}", None
                ),
                "taskId": raw_message.task.task_id if raw_message.task else None,
                "taskStatus": raw_message.task.status if raw_message.task else None,
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
                latest_message_text = " ".join(
                    [c["text"] for c in last_message_dict[chat_id]["content"][0]["content"]]
                )
            except:
                print("project_views", last_message_dict[chat_id]["content"])
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
                    "systemUserId": project_system_user_id,
                    "isDm": False,
                    "chatType": 3,
                    "dmPartnerUser": {"userName": "", "userId": "", "avatarImgPath": ""},
                    "messages": [new_message],
                    "latestMessage": last_message_dict[chat_id],
                    "latestMessageText": latest_message_text,
                    "TSLastMessage": ts_last_message_dict[chat_id],
                    "project": {
                        "projectId": int(raw_message.project.project_id),
                        "projectName": raw_message.project.project_name,
                        "isJoined": True,
                        "systemUserId": project_system_user_id,
                    },
                }

        message_history = list(message_history_dict.values())

        return Response(message_history, status=status.HTTP_200_OK)


class PMSingleMessageView(AuthenticatedAPIView):
    def post(self, request):
        project = ProjectMembers.objects.filter(project=request.data["project_id"])

        if len(project) > 0:
            current_message_count = PMMessages.objects.filter(
                project=project[0].project.project_id
            ).count()
        else:
            current_message_count = 0

        is_init = request.data.get("is_init")
        if (is_init in [None, False]) or (is_init == True and current_message_count == 0):
            data = {
                "project": request.data["project_id"],
                "sender": request.data["sender_id"],
                "message_id": current_message_count + 1,
                "message_body": request.data["message_body"],
                "task": request.data["task_id"],
            }
            serializer = PMMessagesSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response(
                {"message": "Nothing to do cause it's already initialized"},
                status=status.HTTP_201_CREATED,
            )

    def put(self, request):
        try:
            if request.data["message_id"] == None:
                message = PMMessages.objects.get(
                    project=request.data["project_id"], task=request.data["task_id"]
                )
            elif request.data["task_id"] == None:
                message = PMMessages.objects.get(
                    project=request.data["project_id"], message_id=request.data["message_id"]
                )
            else:
                Response(
                    "Either message_id or task_id is required.", status=status.HTTP_400_BAD_REQUEST
                )
        except PMMessages.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        data = {
            "message_body": (
                request.data["message_body"]
                if request.data["message_body"]
                else message.message_body
            ),
        }

        serializer = PMMessagesSerializer(message, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


#############################
# PM Thread Messages views
#############################
class CheckPMThreadExistsView(AuthenticatedAPIView):
    def get(self, request):
        project_id = request.GET.get("project_id", None)
        thread_id = request.GET.get("thread_id", None)

        if not project_id or not thread_id:
            return Response(
                {"error": "Both project_id and thread_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a DM exists in any order
        exists = PMThreadMessages.objects.filter(
            Q(project=project_id, thread_id=thread_id)
        ).exists()

        return Response({"pm_thread_exists": exists}, status=status.HTTP_200_OK)


class PMSingleThreadMessageView(AuthenticatedAPIView):
    def post(self, request):
        try:
            if request.data["thread_id"]:
                thread_id = request.data["thread_id"]
            elif request.data["thread_id"] == None and request.data["task_id"]:
                message = PMMessages.objects.get(
                    project=request.data["project_id"], task=request.data["task_id"]
                )
                thread_id = message.message_id
                if not isinstance(thread_id, int):
                    Response(
                        "Failed to get thread_id from task_id.", status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                Response(
                    "Either thread_id or task_id is required.", status=status.HTTP_400_BAD_REQUEST
                )
        except PMMessages.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        project = ProjectMaster.objects.filter(project_id=request.data["project_id"])
        if len(project) > 0:
            current_thread_message_count = PMThreadMessages.objects.filter(
                project=project[0].project_id, thread_id=thread_id
            ).count()
        else:
            Response("project is not found", status=status.HTTP_400_BAD_REQUEST)

        data = {
            "project": request.data["project_id"],
            "thread_id": thread_id,
            "sender": request.data["sender_id"],
            "thread_message_id": current_thread_message_count + 1,
            "thread_message_body": request.data["message_body"],
            "parent_message_uid": "{project_id}-{parent_message_id}".format(
                project_id=request.data["project_id"],
                parent_message_id=thread_id,
            ),
            "task": request.data["task_id"],
        }

        serializer = PMThreadMessagesSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PMThreadMessagesByIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        project_id = request.GET.get("pm_id", None)
        thread_id = request.GET.get("thread_id", None)

        if not team_id or not team_name or not project_id or not thread_id:
            return Response(
                "project_id and/or thread_id is not found", status=status.HTTP_400_BAD_REQUEST
            )

        # Fetch all messages where the project_id matches and the user is involved
        raw_messages = PMThreadMessages.objects.filter(project=project_id, thread_id=thread_id)

        thread_messages = []
        for raw_message in raw_messages:
            chat_id = int(raw_message.project.project_id)
            message_id = int(raw_message.thread_message_id)
            content = raw_message.thread_message_body
            sender_id = str(raw_message.sender.id)
            sender_name = str(raw_message.sender.username)
            sender_email = str(raw_message.sender.email)
            sender_avatar_img_path = raw_message.sender.profile_image_url
            is_system_user = raw_message.sender.is_system_user
            ts_sent = str(raw_message.ts_sent_at)

            try:
                contentText = " ".join([c["text"] for c in content[0]["content"]])
            except:
                print("pm_views", content["content"])
                contentText = "Failed to get text..."

            messageIdWithChatIdAndThreadId = f"{chat_id}-{thread_id}-{message_id}"
            new_message = {
                "chatType": 2,
                "messageIdWithChatIdAndThreadId": messageIdWithChatIdAndThreadId,
                "chatId": chat_id,
                "threadId": thread_id,
                "messageId": message_id,
                "content": content,
                "contentText": contentText,
                "sender": {
                    "teamId": team_id,
                    "teamName": team_name,
                    "userName": sender_name,
                    "userEmail": sender_email,
                    "userId": sender_id,
                    "avatarImgPath": sender_avatar_img_path,
                    "isSystemUser": is_system_user,
                },
                "taskId": None,
                "tsSent": ts_sent,
            }
            thread_messages.append(new_message)

        return Response(thread_messages, status=status.HTTP_200_OK)
