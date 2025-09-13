from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.reaction_models import *
from origin.models.chat.gm_models import GMMaster, GMMembers, GMMessages, GMThreadMessages
from origin.models.chat.read_status_models import *
from origin.serializers.chat.gm_serializers import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 2


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
        gm_id = int(request.GET.get("gm_id"))

        if not gm_id:
            return Response(
                {"error": "Both gm_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a GM exists in any order
        exists = GMMaster.objects.filter(Q(gm_id=gm_id)).exists()

        return Response({"gm_exists": exists}, status=status.HTTP_200_OK)


class GMIdView(AuthenticatedAPIView):
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


class AllGMIdsView(AuthenticatedAPIView):
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
class GMHistoryView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        attendee_id = request.GET.get("user_id")

        if not team_id or not team_name or not attendee_id:
            return Response(
                {"error": "team_id, team_name and attendee_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch all gm_ids linked to the user
        gm_ids = list(
            GMMembers.objects.filter(Q(gm__owner_team=team_id, attendee=attendee_id)).values_list(
                "gm_id", flat=True
            )
        )

        if not gm_ids:
            return Response({"messages": []}, status=status.HTTP_200_OK)

        # Fetch all messages where the gm_id matches and the user is involved
        raw_messages = GMMessages.objects.filter(gm_id__in=gm_ids)

        # Group by gm_id and parent_message_id, then count the replies in each group
        thread_reply_counts = GMThreadMessages.objects.values(
            "parent_message_uid__gm__gm_id", "parent_message_uid__message_id"
        ).annotate(num_of_replies=Count("thread_message_id"))

        thread_reply_count_map = {}
        for reply_count_info in thread_reply_counts:
            gm_id = reply_count_info["parent_message_uid__gm__gm_id"]
            message_id = reply_count_info["parent_message_uid__message_id"]
            reply_count = reply_count_info["num_of_replies"]
            thread_reply_count_map[f"{gm_id}-{message_id}"] = reply_count

        # Fetch reactions
        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id__in=gm_ids, is_thread=False
        )

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
            ts_updated_at = str(raw_message.ts_updated_at)
            ts_sent = str(raw_message.ts_sent_at)

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
                        "customStatus": "",
                    },
                    "tsSent": reaction[5],
                }
                if str(reaction[3]) == attendee_id:
                    my_reactions.append(_reaction)
                all_reactions.append(_reaction)

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
                    "avatarImgPath": sender_avatar_img_path,
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "customStatus": "",
                },
                "numReplies": thread_reply_count_map.get(
                    f"{raw_message.gm.gm_id}-{message_id}", None
                ),
                "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
                "taskId": raw_message.task.task_id if raw_message.task else None,
                "taskStatus": raw_message.task.status if raw_message.task else None,
                "project": {
                    "projectId": (
                        raw_message.task.project.project_id if raw_message.task else None
                    ),
                    "projectName": (
                        raw_message.task.project.project_name if raw_message.task else None
                    ),
                    "isJoined": True if raw_message.task else False,
                    "systemUserId": (
                        raw_message.task.project.project_system_user.id
                        if raw_message.task
                        else None
                    ),
                },
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
                    "chatType": 2,
                    "dmPartnerUser": {
                        "userName": "",
                        "userId": "",
                        "avatarImgPath": "",
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "messages": [new_message],
                    "latestMessage": last_message_dict[chat_id],
                    "latestMessageText": latest_message_text,
                    "TSLastMessage": ts_last_message_dict[chat_id],
                }

        # Add last_read_message_id for each chat.
        last_read_message_id_for_chats = ReadStatus.objects.filter(
            user=attendee_id, chat_type=CHAT_TYPE, chat_id__in=gm_ids, is_thread=False
        )
        for chat_id in message_history_dict.keys():
            raw_last_read_message_id = last_read_message_id_for_chats.filter(
                chat_id=chat_id
            ).values_list("last_read_message_id")
            if len(raw_last_read_message_id) == 1:
                last_read_message_id = raw_last_read_message_id[0][0]
            else:
                last_read_message_id = -1

            message_history_dict[chat_id]["lastReadMessageId"] = last_read_message_id

        message_history = list(message_history_dict.values())

        return Response(message_history, status=status.HTTP_200_OK)


class GMSingleMessageView(AuthenticatedAPIView):
    def get(self, request):
        user_id = request.GET.get("user_id")
        gm_id = int(request.GET.get("gm_id"))
        message_id = int(request.GET.get("message_id"))

        if not user_id or not gm_id or not message_id:
            return Response(
                {"error": "user_id, gm_id and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gm = GMMessages.objects.filter(gm=gm_id, message_id=message_id)
        if len(gm) == 0:
            return Response(
                {"error": "GM not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        elif len(gm) > 1:
            return Response(
                {"error": "Duplicated GM found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            gm = gm[0]

        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=gm_id, message_id=message_id, is_thread=False
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
                    "customStatus": "",
                },
                "tsSent": raw_reaction.ts_created_at,
            }
            all_reactions.append(reaction)
            if raw_reaction.sender.id == user_id:
                my_reactions.append(reaction)

        thread_reply_counts = (
            GMThreadMessages.objects.filter(gm=gm_id, thread_id=message_id)
            .values("parent_message_uid__gm__gm_id", "parent_message_uid__message_id")
            .annotate(num_of_replies=Count("thread_message_id"))
        )
        reply_count = 0
        if len(thread_reply_counts) == 1:
            reply_count = int(thread_reply_counts[0]["num_of_replies"])
        elif len(thread_reply_counts) > 1:
            print("Error!!!! thread_reply_counts has multiple thread found")

        raw_last_read_message_id = ReadStatus.objects.filter(
            user=user_id, chat_type=CHAT_TYPE, chat_id=gm_id, is_thread=False
        ).values_list("last_read_message_id")
        if len(raw_last_read_message_id) == 1:
            last_read_message_id = raw_last_read_message_id[0][0]
        else:
            last_read_message_id = -1

        message = {
            "messageIdWithChatId": f"{gm_id}-{message_id}",
            "chatId": int(gm_id),
            "messageId": int(message_id),
            "content": gm.message_body,
            "sender": {
                "userId": gm.sender.id,
                "userName": gm.sender.username,
                "userEmail": gm.sender.email,
                "avatarImgPath": gm.sender.profile_image_url,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": gm.sender.is_system_user,
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
            "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
            "taskId": gm.task.task_id if gm.task else None,
            "taskStatus": gm.task.status if gm.task else None,
            "project": {
                "projectId": (gm.task.project.project_id if gm.task else None),
                "projectName": (gm.task.project.project_name if gm.task else None),
                "isJoined": True,
                "systemUserId": (gm.task.project.project_system_user.id if gm.task else None),
            },
            "tsSent": gm.ts_sent_at,
            "tsUpdated": gm.ts_updated_at,
            "lastReadMessageId": last_read_message_id,
        }

        return Response(message, status=status.HTTP_200_OK)

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

            raw_last_read_message_id = ReadStatus.objects.filter(
                user=request.user.id,
                chat_type=CHAT_TYPE,
                chat_id=request.data["gm_id"],
                is_thread=False,
            ).values_list("last_read_message_id")
            if len(raw_last_read_message_id) == 1:
                last_read_message_id = raw_last_read_message_id[0][0]
            else:
                last_read_message_id = -1

            serializer = GMMessagesSerializer(data=data)
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
        gm_id = request.data.get("gm_id")
        message_id = request.data.get("message_id")

        if gm_id is None or message_id is None:
            return Response(
                {"error": "gm_id and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = get_object_or_404(GMMessages, gm=gm_id, message_id=message_id)

        update_data = request.data.copy()
        # Remove None values from the updated_data
        if "message_body" in update_data and update_data["message_body"] is None:
            update_data.pop("message_body")
        if "task_id" in update_data and update_data["task_id"] is None:
            update_data.pop("task_id")

        # For the task_id, it needs to be changed to "task" if exists.
        if "task_id" in update_data:
            update_data["task"] = update_data.pop("task_id")

        raw_last_read_message_id = ReadStatus.objects.filter(
            user=request.user.id,
            chat_type=CHAT_TYPE,
            chat_id=request.data["dm_id"],
            is_thread=False,
        ).values_list("last_read_message_id")
        if len(raw_last_read_message_id) == 1:
            last_read_message_id = raw_last_read_message_id[0][0]
        else:
            last_read_message_id = -1

        serializer = GMMessagesSerializer(message, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            res = {**serializer.data, "last_read_message_id": last_read_message_id}
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


#############################
# GM Thread Messages views
#############################
class CheckGMThreadExistsView(AuthenticatedAPIView):
    def get(self, request):
        gm_id = int(request.GET.get("gm_id"))
        thread_id = int(request.GET.get("thread_id"))

        if not gm_id or not thread_id:
            return Response(
                {"error": "Both gm_id and thread_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a GM exists in any order
        exists = GMThreadMessages.objects.filter(Q(gm=gm_id, thread_id=thread_id)).exists()

        return Response({"gm_thread_exists": exists}, status=status.HTTP_200_OK)


class GMSingleThreadMessageView(AuthenticatedAPIView):
    def get(self, request):
        user_id = request.GET.get("user_id")
        gm_id = int(request.GET.get("gm_id"))
        thread_id = int(request.GET.get("thread_id"))
        message_id = int(request.GET.get("message_id"))

        if not user_id or not gm_id or not thread_id or not message_id:
            return Response(
                {"error": "user_id, gm_id, thread_id and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        gm = GMThreadMessages.objects.filter(
            gm=gm_id, thread_id=thread_id, thread_message_id=message_id
        )
        if len(gm) == 0:
            return Response(
                {"error": "GM not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        elif len(gm) > 1:
            return Response(
                {"error": "Duplicated GM found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            gm = gm[0]

        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=gm_id, message_id=message_id, is_thread=True
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
                    "customStatus": "",
                },
                "tsSent": raw_reaction.ts_created_at,
            }
            all_reactions.append(reaction)
            if str(raw_reaction.sender.id) == user_id:
                my_reactions.append(reaction)

        contentText = generate_first_line.get(gm.thread_message_body[0])
        messageIdWithChatIdAndThreadId = f"{gm_id}-{thread_id}-{message_id}"
        message = {
            "messageIdWithChatIdAndThreadId": messageIdWithChatIdAndThreadId,
            "chatId": int(gm_id),
            "threadId": gm.thread_id,
            "messageId": gm.thread_message_id,
            "content": gm.thread_message_body,
            "contentText": contentText,
            "sender": {
                "userId": gm.sender.id,
                "userName": gm.sender.username,
                "userEmail": gm.sender.email,
                "avatarImgPath": gm.sender.profile_image_url,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": gm.sender.is_system_user,
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
            "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
            "taskId": gm.parent_message_uid.task.task_id if gm.parent_message_uid.task else None,
            "taskExist": True if gm.parent_message_uid.task else False,
            "project": {
                "projectId": (
                    gm.parent_message_uid.task.project.project_id
                    if gm.parent_message_uid.task
                    else None
                ),
                "projectName": (
                    gm.parent_message_uid.task.project.project_name
                    if gm.parent_message_uid.task
                    else None
                ),
                "isJoined": True,
                "systemUserId": (
                    gm.parent_message_uid.task.project.project_system_user.id
                    if gm.parent_message_uid.task
                    else None
                ),
            },
            "tsSent": gm.ts_sent_at,
            "tsUpdated": gm.ts_updated_at,
        }

        return Response(message, status=status.HTTP_200_OK)

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

    def put(self, request):
        gm_id = request.data.get("gm_id")
        thread_id = request.data.get("thread_id")
        message_id = request.data.get("message_id")

        if gm_id is None or message_id is None or thread_id is None:
            return Response(
                {"error": "gm_id , thread_id, and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = get_object_or_404(
            GMThreadMessages, gm=gm_id, thread_id=thread_id, thread_message_id=message_id
        )

        update_data = request.data.copy()
        # Remove None values from the updated_data if it's None
        if "message_body" in update_data and update_data["message_body"] is None:
            update_data.pop("message_body")

        # Change the field name
        if "message_body" in update_data:
            update_data["thread_message_body"] = update_data.pop("message_body")

        serializer = GMThreadMessagesSerializer(message, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GMThreadMessagesByIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")
        gm_id = int(request.GET.get("gm_id"))
        thread_id = int(request.GET.get("thread_id"))

        if not team_id or not team_name or not user_id or not gm_id or not thread_id:
            return Response(
                "gm_id and/or thread_id is not found", status=status.HTTP_400_BAD_REQUEST
            )

        # Fetch all messages where the gm_id matches and the user is involved
        raw_messages = GMThreadMessages.objects.filter(gm=gm_id, thread_id=thread_id).order_by(
            "ts_sent_at"
        )

        # Fetch reactions
        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=gm_id, is_thread=True
        )

        thread_messages = []
        for raw_message in raw_messages:
            chat_id = int(raw_message.gm.gm_id)
            message_id = int(raw_message.thread_message_id)
            content = raw_message.thread_message_body
            sender_id = str(raw_message.sender.id)
            sender_name = str(raw_message.sender.username)
            sender_email = str(raw_message.sender.email)
            sender_avatar_img_path = raw_message.sender.profile_image_url
            is_system_user = raw_message.sender.is_system_user
            ts_sent = str(raw_message.ts_sent_at)
            ts_updated_at = str(raw_message.ts_updated_at)

            if message_id == 1:
                # fetch the first thread message reactions -> the parent message reaction.
                reactions = ReactionFact.objects.filter(
                    chat_type=CHAT_TYPE, chat_id=gm_id, is_thread=False, message_id=thread_id
                ).values_list(
                    "reaction_id",
                    "reaction_emoji",
                    "sender__username",
                    "sender__id",
                    "sender__profile_image_url",
                    "ts_created_at",
                )
            else:
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
                        "customStatus": "",
                    },
                    "tsSent": reaction[5],
                }
                if str(reaction[3]) == user_id:
                    my_reactions.append(_reaction)
                all_reactions.append(_reaction)

            # Get the parent ts_sent/ts_updated_at for the first thread message.
            if raw_message.thread_message_id == 1:
                parent_message = GMMessages.objects.filter(gm=gm_id, message_id=thread_id)[0]
                ts_sent = parent_message.ts_sent_at
                ts_updated_at = parent_message.ts_updated_at

            contentText = generate_first_line.get(content[0])
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
                "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
                "taskId": (
                    raw_message.parent_message_uid.task.task_id
                    if raw_message.parent_message_uid.task
                    else None
                ),
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
