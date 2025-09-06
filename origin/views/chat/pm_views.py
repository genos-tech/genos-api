from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.reaction_models import *
from origin.models.project.prj_models import ProjectMembers, ProjectMaster
from origin.models.chat.pm_models import PMMessages, PMThreadMessages
from origin.serializers.chat.pm_serializers import *
from origin.views.chat.modules.common import generate_first_line


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

        # Fetch reactions
        raw_reactions = ReactionFact.objects.filter(
            chat_type=3, chat_id__in=project_ids, is_thread=False
        )

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
            ts_updated_at = str(raw_message.ts_updated_at)

            reactions = raw_reactions.filter(message_id=int(raw_message.message_id)).values_list(
                "reaction_id",
                "reaction_emoji",
                "sender__username",
                "sender__id",
                "sender__profile_image_url",
                "ts_created_at",
            )
            my_reactions = []
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
                    },
                    "tsSent": reaction[5],
                }
                if str(reaction[3]) == attendee_id:
                    my_reactions.append(_reaction)
                all_reactions.append(_reaction)

            messageIdWithChatId = (
                f"{chat_id}-{raw_message.task.task_id if raw_message.task else 0}"
            )
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
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "isSystemUser": is_system_user,
                },
                "numReplies": thread_reply_count_map.get(
                    f"{raw_message.project.project_id}-{message_id}", None
                ),
                "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
                "taskId": raw_message.task.task_id if raw_message.task else None,
                "taskStatus": raw_message.task.status if raw_message.task else None,
                "tsSent": ts_sent,
                "tsUpdated": ts_updated_at,
            }

            if chat_id in ts_last_message_dict:
                prev_ts_last_message = ts_last_message_dict[chat_id]
                if ts_sent > prev_ts_last_message:
                    last_message_dict[chat_id] = new_message
                    ts_last_message_dict[chat_id] = ts_sent
            else:
                last_message_dict[chat_id] = new_message
                ts_last_message_dict[chat_id] = ts_sent

            latest_message_text = generate_first_line.get(last_message_dict[chat_id]["content"][0])

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
                    "chatType": 3,
                    "dmPartnerUser": {
                        "userName": "",
                        "userId": "",
                        "avatarImgPath": "",
                        "tsLastSeen": "",
                        "tsJoined": "",
                    },
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
                {"error": "GM not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        elif len(pm) > 1:
            return Response(
                {"error": "Duplicated GM found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            pm = pm[0]

        raw_reactions = ReactionFact.objects.filter(
            chat_type=3, chat_id=project_id, message_id=message_id, is_thread=False
        )
        all_reactions = []
        my_reactions = []
        for raw_reaction in raw_reactions:
            reaction = {
                "id": int(raw_reaction.reaction_id),
                "emoji": raw_reaction.reaction_emoji,
                "sender": {
                    "userName": raw_reaction.sender.username,
                    "userId": raw_reaction.sender.id,
                    "avatarImgPath": raw_reaction.sender.profile_image_url,
                    "tsLastSeen": "",
                    "tsJoined": "",
                },
                "tsSent": raw_reaction.ts_created_at,
            }
            all_reactions.append(reaction)
            if raw_reaction.sender.id == user_id:
                my_reactions.append(reaction)

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

        message = {
            "messageIdWithChatId": f"{project_id}-{message_id}",
            "chatId": int(project_id),
            "messageId": int(message_id),
            "content": pm.message_body,
            "sender": {
                "userId": pm.sender.id,
                "userName": pm.sender.username,
                "userEmail": pm.sender.email,
                "avatarImgPath": pm.sender.profile_image_url,
                "tsLastSeen": "",
                "tsJoined": "",
                "isSystemUser": pm.sender.is_system_user,
            },
            "numReplies": reply_count,
            "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
            "taskId": pm.task.task_id if pm.task else None,
            "taskStatus": pm.task.status if pm.task else None,
            "project": {
                "projectId": (pm.task.project.project_id if pm.task else None),
                "projectName": (pm.task.project.project_name if pm.task else None),
                "isJoined": True,
                "systemUserId": (pm.task.project.project_system_user.id if pm.task else None),
            },
            "tsSent": pm.ts_sent_at,
            "tsUpdated": pm.ts_updated_at,
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
                {"error": "GM not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        elif len(pm) > 1:
            return Response(
                {"error": "Duplicated GM found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            pm = pm[0]

        raw_reactions = ReactionFact.objects.filter(
            chat_type=2, chat_id=project_id, message_id=message_id, is_thread=True
        )
        all_reactions = []
        my_reactions = []
        for raw_reaction in raw_reactions:
            reaction = {
                "id": int(raw_reaction.reaction_id),
                "emoji": raw_reaction.reaction_emoji,
                "sender": {
                    "userName": raw_reaction.sender.username,
                    "userId": raw_reaction.sender.id,
                    "avatarImgPath": raw_reaction.sender.profile_image_url,
                    "tsLastSeen": "",
                    "tsJoined": "",
                },
                "tsSent": raw_reaction.ts_created_at,
            }
            all_reactions.append(reaction)
            if str(raw_reaction.sender.id) == user_id:
                my_reactions.append(reaction)

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
                "avatarImgPath": pm.sender.profile_image_url,
                "tsLastSeen": "",
                "tsJoined": "",
                "isSystemUser": pm.sender.is_system_user,
            },
            "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
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
        project_id = int(request.data["project_id"])
        thread_id = int(request.data["thread_id"])
        message_id = int(request.data["message_id"])

        if not project_id or not thread_id or not message_id:
            return Response(
                {"error": "project_id, thread_id, and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = PMThreadMessages.objects.get(
            project=project_id, thread_id=thread_id, thread_message_id=message_id
        )

        data = {
            "thread_message_body": (
                request.data["message_body"]
                if request.data["message_body"]
                else message.message_body
            )
        }

        serializer = PMThreadMessagesSerializer(message, data=data, partial=True)
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
            chat_type=3, chat_id=project_id, is_thread=True
        )

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
            ts_updated_at = str(raw_message.ts_updated_at)

            reactions = raw_reactions.filter(
                message_id=int(raw_message.thread_message_id)
            ).values_list(
                "reaction_id",
                "reaction_emoji",
                "sender__username",
                "sender__id",
                "sender__profile_image_url",
                "ts_created_at",
            )
            my_reactions = []
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
                    },
                    "tsSent": reaction[5],
                }
                if str(reaction[3]) == user_id:
                    my_reactions.append(_reaction)
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
                    "isSystemUser": is_system_user,
                },
                "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
                "taskId": task_id,
                "tsSent": ts_sent,
                "tsUpdated": ts_updated_at,
            }
            thread_messages.append(new_message)

        return Response(thread_messages, status=status.HTTP_200_OK)
