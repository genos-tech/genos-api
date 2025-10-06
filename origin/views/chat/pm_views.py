from collections import defaultdict

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.reaction_models import *
from origin.models.project.prj_models import ProjectMembers, ProjectMaster
from origin.models.chat.pm_models import PMMessages, PMThreadMessages
from origin.models.chat.read_status_models import *
from origin.serializers.chat.pm_serializers import *
from origin.views.chat.modules.common import generate_first_line
from origin.models.chat.chat_master_models import UserChatMaster

CHAT_TYPE = 3


#############################
# PM Messages views
#############################
class PMHistoryView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        attendee_id = request.GET.get("user_id")

        if not (team_id and team_name and attendee_id):
            return Response(
                {"error": "team_id, team_name and user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Get chat master for this user
        pinned_chats = UserChatMaster.objects.filter(user=attendee_id, team=team_id).values_list(
            "pinned_chats", flat=True
        )
        pinned_pm_ids = (
            set((c["chat_type"], c["chat_id"]) for c in pinned_chats[0])
            if pinned_chats[0]
            else set()
        )

        # Projects this user belongs to
        project_ids = list(
            ProjectMembers.objects.filter(team=team_id, attendee=attendee_id).values_list(
                "project_id", flat=True
            )
        )
        if not project_ids:
            return Response({"messages": []}, status=status.HTTP_200_OK)

        # Messages for these projects (prefetch related sender/project/task)
        raw_messages = (
            PMMessages.objects.filter(project__in=project_ids)
            .select_related("project", "sender", "task")
            .order_by("ts_sent_at")
        )

        # Reply counts (avoid recomputing in loop)
        thread_reply_counts = {
            f"{row['parent_message_uid__project__project_id']}-{row['parent_message_uid__message_id']}": row[
                "num_of_replies"
            ]
            for row in PMThreadMessages.objects.values(
                "parent_message_uid__project__project_id",
                "parent_message_uid__message_id",
            ).annotate(num_of_replies=Count("thread_message_id"))
        }

        # Reactions (grouped by message_id)
        raw_reactions = (
            ReactionFact.objects.filter(
                chat_type=CHAT_TYPE, chat_id__in=project_ids, is_thread=False
            )
            .select_related("sender")
            .values(
                "message_id",
                "reaction_id",
                "reaction_emoji",
                "sender__username",
                "sender__id",
                "sender__profile_image_file_name",
                "ts_created_at",
            )
        )
        reactions_by_message = defaultdict(list)
        for r in raw_reactions:
            reactions_by_message[r["message_id"]].append(
                {
                    "id": r["reaction_id"],
                    "emoji": r["reaction_emoji"],
                    "sender": {
                        "userName": r["sender__username"],
                        "userId": r["sender__id"],
                        "avatarImgPath": r["sender__profile_image_file_name"],
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "tsSent": r["ts_created_at"],
                }
            )

        # Last read messages (dict by chat_id)
        last_read_map = {
            rs.chat_id: rs.last_read_message_id
            for rs in ReadStatus.objects.filter(
                user=attendee_id, chat_type=CHAT_TYPE, chat_id__in=project_ids, is_thread=False
            )
        }

        # Build history
        message_history_dict = {}
        for msg in raw_messages:
            chat_id = msg.project.project_id
            msg_dict = self.serialize_message(
                msg, team_id, team_name, thread_reply_counts, reactions_by_message
            )

            # Track latest message
            if chat_id not in message_history_dict:
                message_history_dict[chat_id] = self.init_chat_dict(msg, msg_dict)
            else:
                message_history_dict[chat_id]["messages"].append(msg_dict)
                if msg.ts_sent_at > message_history_dict[chat_id]["TSLastMessage"]:
                    message_history_dict[chat_id]["latestMessage"] = msg_dict
                    message_history_dict[chat_id]["latestMessageText"] = generate_first_line.get(
                        msg_dict["content"][0]
                    )
                    message_history_dict[chat_id]["TSLastMessage"] = msg.ts_sent_at

        # Add last read info
        for chat_id, chat in message_history_dict.items():
            chat["lastReadMessageId"] = last_read_map.get(chat_id, -1)
            if (CHAT_TYPE, chat_id) in pinned_pm_ids:
                chat["isPinned"] = True
            else:
                chat["isPinned"] = False

        return Response(list(message_history_dict.values()), status=status.HTTP_200_OK)

    def serialize_message(self, msg, team_id, team_name, reply_counts, reactions_by_message):
        project_id = msg.project.project_id
        message_id = msg.message_id
        return {
            "messageIdWithChatId": f"{project_id}-{msg.task.task_id if msg.task else 0}",
            "chatId": project_id,
            "systemUserId": msg.project.project_system_user.id,
            "messageId": message_id,
            "content": msg.message_body,
            "sender": {
                "teamId": team_id,
                "teamName": team_name,
                "userName": msg.sender.username,
                "userEmail": msg.sender.email,
                "userId": msg.sender.id,
                "avatarImgPath": msg.sender.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": msg.sender.is_system_user,
            },
            "receiver": {},  # placeholder for future
            "numReplies": reply_counts.get(f"{project_id}-{message_id}", 0),
            "reactions": reactions_by_message.get(message_id, []),
            "project": {
                "projectId": msg.project.project_id,
                "projectName": msg.project.project_name,
                "isJoined": True,
                "systemUserId": msg.project.project_system_user.id,
            },
            "taskId": msg.task.task_id if msg.task else None,
            "taskExist": True if msg.task else False,
            "taskStatus": msg.task.status if msg.task else None,
            "tsSent": msg.ts_sent_at,
            "tsUpdated": msg.ts_updated_at,
        }

    def init_chat_dict(self, msg, first_message):
        return {
            "chatId": msg.project.project_id,
            "chatName": msg.project.project_name,
            "systemUserId": msg.project.project_system_user.id,
            "chatType": 3,
            "dmPartnerUser": {},  # not used in PM
            "messages": [first_message],
            "latestMessage": first_message,
            "latestMessageText": generate_first_line.get(first_message["content"][0]),
            "TSLastMessage": msg.ts_sent_at,
            "project": {
                "projectId": msg.project.project_id,
                "projectName": msg.project.project_name,
                "isJoined": True,
                "systemUserId": msg.project.project_system_user.id,
            },
            "profileImagePath": msg.project.profile_image_file_name,
        }


class PMSingleMessageView(AuthenticatedAPIView):
    def get(self, request):
        user_id = request.GET.get("user_id")
        project_id = int(request.GET.get("project_id"))
        message_id = int(request.GET.get("message_id"))

        if not user_id or not project_id or not message_id:
            return Response(
                {"error": "user_id, project_id and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        pm = PMMessages.objects.filter(project=project_id, message_id=message_id)
        if len(pm) == 0:
            return Response(
                {"error": "PM not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        elif len(pm) > 1:
            return Response(
                {"error": "Duplicated PM found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            pm = pm[0]

        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=project_id, message_id=message_id, is_thread=False
        )
        all_reactions = []
        for raw_reaction in raw_reactions:
            reaction = {
                "id": int(raw_reaction.reaction_id),
                "emoji": raw_reaction.reaction_emoji,
                "sender": {
                    "userName": raw_reaction.sender.username,
                    "userId": raw_reaction.sender.id,
                    "avatarImgPath": raw_reaction.sender.profile_image_file_name,
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "customStatus": "",
                },
                "tsSent": raw_reaction.ts_created_at,
            }
            all_reactions.append(reaction)

        thread_reply_counts = (
            PMThreadMessages.objects.filter(project=project_id, thread_id=message_id)
            .values("parent_message_uid__project__project_id", "parent_message_uid__message_id")
            .annotate(num_of_replies=Count("thread_message_id"))
        )
        reply_count = 0
        if len(thread_reply_counts) == 1:
            reply_count = int(thread_reply_counts[0]["num_of_replies"])
        elif len(thread_reply_counts) > 1:
            print("Error!!!! thread_reply_counts has multiple thread found")

        raw_last_read_message_id = ReadStatus.objects.filter(
            user=user_id, chat_type=CHAT_TYPE, chat_id=project_id, is_thread=False
        ).values_list("last_read_message_id")
        if len(raw_last_read_message_id) == 1:
            last_read_message_id = raw_last_read_message_id[0][0]
        else:
            last_read_message_id = -1

        message = {
            "messageIdWithChatId": f"{project_id}-{message_id}",
            "chatId": int(project_id),
            "messageId": int(message_id),
            "content": pm.message_body,
            "sender": {
                "userId": pm.sender.id,
                "userName": pm.sender.username,
                "userEmail": pm.sender.email,
                "avatarImgPath": pm.sender.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": pm.sender.is_system_user,
            },
            "receiver": {
                "userId": "",
                "userName": "",
                "userEmail": "",
                "avatarImgPath": "",
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": "",
            },
            "numReplies": reply_count,
            "reactions": all_reactions,
            "taskId": pm.task.task_id if pm.task else None,
            "taskExist": True if pm.task else False,
            "taskStatus": pm.task.status if pm.task else None,
            "project": {
                "projectId": (pm.task.project.project_id if pm.task else None),
                "projectName": (pm.task.project.project_name if pm.task else None),
                "isJoined": True,
                "systemUserId": (pm.task.project.project_system_user.id if pm.task else None),
            },
            "tsSent": pm.ts_sent_at,
            "tsUpdated": pm.ts_updated_at,
            "lastReadMessageId": last_read_message_id,
        }

        return Response(message, status=status.HTTP_200_OK)

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

            raw_last_read_message_id = ReadStatus.objects.filter(
                user=request.user.id,
                chat_type=CHAT_TYPE,
                chat_id=request.data["project_id"],
                is_thread=False,
            ).values_list("last_read_message_id")
            if len(raw_last_read_message_id) == 1:
                last_read_message_id = raw_last_read_message_id[0][0]
            else:
                last_read_message_id = -1

            serializer = PMMessagesSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                res = {**serializer.data, "last_read_message_id": last_read_message_id}
                return Response(res, status=status.HTTP_201_CREATED)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response(
                {"message": "Nothing to do cause it's already initialized"},
                status=status.HTTP_201_CREATED,
            )

    def put(self, request):
        try:
            if request.data.get("message_id") is None:
                message = PMMessages.objects.get(
                    project=request.data["project_id"], task=request.data["task_id"]
                )
            elif request.data.get("task_id") is None:
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
            "message_body": request.data.get("message_body", message.message_body),
        }

        raw_last_read_message_id = ReadStatus.objects.filter(
            user=request.user.id,
            chat_type=CHAT_TYPE,
            chat_id=request.data["project_id"],
            is_thread=False,
        ).values_list("last_read_message_id")
        if len(raw_last_read_message_id) == 1:
            last_read_message_id = raw_last_read_message_id[0][0]
        else:
            last_read_message_id = -1

        serializer = PMMessagesSerializer(message, data=data, partial=True)
        if serializer.is_valid():
            serializer.save()
            res = {**serializer.data, "last_read_message_id": last_read_message_id}
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


#############################
# PM Thread Messages views
#############################
class CheckPMThreadExistsView(AuthenticatedAPIView):
    def get(self, request):
        project_id = int(request.GET.get("project_id"))
        thread_id = int(request.GET.get("thread_id"))

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
    def get(self, request):
        user_id = request.GET.get("user_id")
        project_id = int(request.GET.get("project_id"))
        thread_id = int(request.GET.get("thread_id"))
        message_id = int(request.GET.get("message_id"))

        if not user_id or not project_id or not thread_id or not message_id:
            return Response(
                {"error": "user_id, project_id, thread_id and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        pm = PMThreadMessages.objects.filter(
            project=project_id, thread_id=thread_id, thread_message_id=message_id
        )
        if len(pm) == 0:
            return Response(
                {"error": "PM not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        elif len(pm) > 1:
            return Response(
                {"error": "Duplicated PM found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            pm = pm[0]

        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=project_id, message_id=message_id, is_thread=True
        )
        all_reactions = []
        for raw_reaction in raw_reactions:
            reaction = {
                "id": int(raw_reaction.reaction_id),
                "emoji": raw_reaction.reaction_emoji,
                "sender": {
                    "userName": raw_reaction.sender.username,
                    "userId": raw_reaction.sender.id,
                    "avatarImgPath": raw_reaction.sender.profile_image_file_name,
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "customStatus": "",
                },
                "tsSent": raw_reaction.ts_created_at,
            }
            all_reactions.append(reaction)

        contentText = generate_first_line.get(pm.thread_message_body[0])
        task_id = pm.parent_message_uid.task.task_id if pm.parent_message_uid.task else -1
        messageIdWithChatIdAndThreadId = f"{project_id}-{task_id}-{message_id}"
        message = {
            "messageIdWithChatIdAndThreadId": messageIdWithChatIdAndThreadId,
            "chatId": project_id,
            "threadId": pm.thread_id,
            "messageId": pm.thread_message_id,
            "content": pm.thread_message_body,
            "contentText": contentText,
            "sender": {
                "userId": pm.sender.id,
                "userName": pm.sender.username,
                "userEmail": pm.sender.email,
                "avatarImgPath": pm.sender.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": pm.sender.is_system_user,
            },
            "receiver": {
                "userId": "",
                "userName": "",
                "userEmail": "",
                "avatarImgPath": "",
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": "",
            },
            "reactions": all_reactions,
            "taskId": task_id,
            "taskExist": True if pm.parent_message_uid.task else False,
            "project": {
                "projectId": (
                    pm.parent_message_uid.task.project.project_id
                    if pm.parent_message_uid.task
                    else None
                ),
                "projectName": (
                    pm.parent_message_uid.task.project.project_name
                    if pm.parent_message_uid.task
                    else None
                ),
                "isJoined": True,
                "systemUserId": (
                    pm.parent_message_uid.task.project.project_system_user.id
                    if pm.parent_message_uid.task
                    else None
                ),
            },
            "tsSent": pm.ts_sent_at,
            "tsUpdated": pm.ts_updated_at,
        }

        return Response(message, status=status.HTTP_200_OK)

    def post(self, request):
        try:
            if request.data["thread_id"]:
                thread_id = int(request.data["thread_id"])
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

    def put(self, request):
        project_id = request.data.get("project_id")
        thread_id = request.data.get("thread_id")
        message_id = request.data.get("message_id")

        if project_id is None or message_id is None or thread_id is None:
            return Response(
                {"error": "project_id , thread_id, and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = get_object_or_404(
            PMThreadMessages, project=project_id, thread_id=thread_id, thread_message_id=message_id
        )

        update_data = request.data.copy()
        # Remove None values from the updated_data if it's None
        if "message_body" in update_data and update_data["message_body"] is None:
            update_data.pop("message_body")

        # Change the field name
        if "message_body" in update_data:
            update_data["thread_message_body"] = update_data.pop("message_body")

        serializer = PMThreadMessagesSerializer(message, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class PMThreadMessagesByIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")
        project_id = int(request.GET.get("pm_id"))
        thread_id = int(request.GET.get("thread_id"))

        if not team_id or not team_name or not project_id or not thread_id:
            return Response(
                "project_id and/or thread_id is not found", status=status.HTTP_400_BAD_REQUEST
            )

        # Fetch all messages where the project_id matches and the user is involved
        raw_messages = PMThreadMessages.objects.filter(
            project=project_id, thread_id=thread_id
        ).order_by("ts_sent_at")

        # Fetch reactions
        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=project_id, is_thread=True
        )

        thread_messages = []
        for raw_message in raw_messages:
            chat_id = int(raw_message.project.project_id)
            message_id = int(raw_message.thread_message_id)
            content = raw_message.thread_message_body
            sender_id = str(raw_message.sender.id)
            sender_name = str(raw_message.sender.username)
            sender_email = str(raw_message.sender.email)
            sender_avatar_img_path = raw_message.sender.profile_image_file_name
            is_system_user = raw_message.sender.is_system_user
            ts_sent = str(raw_message.ts_sent_at)
            ts_updated_at = str(raw_message.ts_updated_at)

            reactions = raw_reactions.filter(
                message_id=int(raw_message.thread_message_id)
            ).values_list(
                "reaction_id",
                "reaction_emoji",
                "sender__username",
                "sender__id",
                "sender__profile_image_file_name",
                "ts_created_at",
            )
            all_reactions = []
            for reaction in reactions:
                _reaction = {
                    "id": int(reaction[0]),
                    "emoji": reaction[1],
                    "sender": {
                        "userName": reaction[2],
                        "userId": reaction[3],
                        "avatarImgPath": reaction[4],
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "tsSent": reaction[5],
                }
                all_reactions.append(_reaction)

            # Get the parent ts_sent/ts_updated_at for the first thread message.
            if raw_message.thread_message_id == 1:
                parent_message = PMMessages.objects.filter(
                    project=project_id, message_id=thread_id
                )[0]
                ts_sent = parent_message.ts_sent_at
                ts_updated_at = parent_message.ts_updated_at

            contentText = generate_first_line.get(content[0])
            task_id = (
                raw_message.parent_message_uid.task.task_id
                if raw_message.parent_message_uid.task
                else -1
            )
            messageIdWithChatIdAndThreadId = f"{chat_id}-{task_id}-{message_id}"
            new_message = {
                "chatType": 3,
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
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "customStatus": "",
                    "isSystemUser": is_system_user,
                },
                "receiver": {
                    "userId": "",
                    "userName": "",
                    "userEmail": "",
                    "avatarImgPath": "",
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "customStatus": "",
                    "isSystemUser": "",
                },
                "reactions": all_reactions,
                "taskId": task_id,
                "taskExist": True if raw_message.parent_message_uid.task else False,
                "project": {
                    "projectId": (
                        raw_message.parent_message_uid.task.project.project_id
                        if raw_message.parent_message_uid.task
                        else None
                    ),
                    "projectName": (
                        raw_message.parent_message_uid.task.project.project_name
                        if raw_message.parent_message_uid.task
                        else None
                    ),
                    "isJoined": True if raw_message.parent_message_uid.task else False,
                    "systemUserId": (
                        raw_message.parent_message_uid.task.project.project_system_user.id
                        if raw_message.parent_message_uid.task
                        else None
                    ),
                },
                "tsSent": ts_sent,
                "tsUpdated": ts_updated_at,
            }
            thread_messages.append(new_message)

        return Response(thread_messages, status=status.HTTP_200_OK)
