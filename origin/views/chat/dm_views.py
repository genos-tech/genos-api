from collections import defaultdict

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.reaction_models import *
from origin.models.chat.dm_models import *
from origin.models.chat.read_status_models import *
from origin.serializers.chat.dm_serializers import *
from origin.views.chat.modules.common import generate_first_line
from origin.models.chat.chat_master_models import UserChatMaster
from origin.views.utils.request_validators import validate_request_data, validate_request_user

CHAT_TYPE = 1


#############################
# DM Master views
#############################
class DMMasterView(AuthenticatedAPIView):
    def post(self, request):
        team_id = request.data.get("team", None)
        user_1_id = request.data.get("user_1_id", None)
        user_2_id = request.data.get("user_2_id", None)

        if not team_id or not user_1_id or not user_2_id:
            return Response(
                {"error": "team_id, user_1_id, and user_2_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a DM exists in any order
        exists = DMMaster.objects.filter(
            Q(team=team_id, user_1_id=user_1_id, user_2_id=user_2_id)
            | Q(team=team_id, user_1_id=user_2_id, user_2_id=user_1_id)
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
        team_id = request.GET.get("team_id", None)
        user_1_id = request.GET.get("user_1_id", None)
        user_2_id = request.GET.get("user_2_id", None)

        if not team_id and not user_1_id or not user_2_id:
            return Response(
                {"error": "team_id, user_1_id and user_2_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a DM exists in any order
        exists = DMMaster.objects.filter(
            Q(team=team_id, user_1_id=user_1_id, user_2_id=user_2_id)
            | Q(team=team_id, user_1_id=user_2_id, user_2_id=user_1_id)
        ).values_list("dm_id", flat=True)

        if len(exists) == 0:
            return Response({"dm_exists": False, "dm_id": None}, status=status.HTTP_200_OK)
        elif len(exists) == 1:
            return Response({"dm_exists": True, "dm_id": exists[0]}, status=status.HTTP_200_OK)
        else:
            return Response(
                {
                    "dm_exists": False,
                    "dm_id": None,
                    "exists": exists,
                    "error": "Duplicated DMs found",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )


class DMIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id", None)
        user_1_id = request.GET.get("user_1_id", None)
        user_2_id = request.GET.get("user_2_id", None)

        if not team_id and not user_1_id or not user_2_id:
            return Response(
                {"error": "team_id, user_1_id and user_2_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a DM exists in any order
        dm = DMMaster.objects.filter(
            Q(team=team_id, user_1_id=user_1_id, user_2_id=user_2_id)
            | Q(team=team_id, user_1_id=user_2_id, user_2_id=user_1_id)
        ).values_list("dm_id", flat=True)

        if len(dm) == 1:
            return Response({"dm_id": dm[0]}, status=status.HTTP_200_OK)
        else:
            return Response({"dm_id": None}, status=status.HTTP_200_OK)


class AllDMIdsView(AuthenticatedAPIView):
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
class DMHistoryView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")

        data = {
            "team_id": team_id,
            "team_name": team_name,
            "user_id": user_id,
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        # Get chat master for this user
        chat_master = UserChatMaster.objects.filter(user=user_id, team=team_id).values_list(
            "pinned_chats", "flagged_messages"
        )
        pinned_dm_ids = (
            set((c["chat_type"], c["chat_id"]) for c in chat_master[0][0])
            if len(chat_master) > 0 and chat_master[0] and chat_master[0][0]
            else set()
        )
        flagged_message_ids = (
            set(
                (c["chat_type"], c["chat_id"], c["thread_id"], c["message_id"])
                for c in chat_master[0][1]
            )
            if len(chat_master) > 0 and chat_master[0] and chat_master[0][1]
            else set()
        )

        # All DMs this user is part of
        dm_ids = list(
            UserDMMapping.objects.filter(user_id=user_id).values_list("dm_id", flat=True)
        )
        if not dm_ids:
            return Response(
                {
                    "chat_history": [],
                    "flagged_messages": [],
                },
                status=status.HTTP_200_OK,
            )

        # Fetch all messages (prefetch sender, receiver, task, project for efficiency)
        raw_messages = (
            DMMessages.objects.filter(dm__team=team_id, dm_id__in=dm_ids, is_deleted=False)
            .select_related("dm", "sender", "receiver", "task", "task__project")
            .order_by("ts_sent_at")
        )

        # Thread reply counts
        thread_reply_counts = {
            f"{row['parent_message_uid__dm__dm_id']}-{row['parent_message_uid__message_id']}": row[
                "num_of_replies"
            ]
            for row in DMThreadMessages.objects.filter(is_deleted=False)
            .values("parent_message_uid__dm__dm_id", "parent_message_uid__message_id")
            .annotate(num_of_replies=Count("thread_message_id"))
        }

        # Reactions (grouped by message_id)
        raw_reactions = (
            ReactionFact.objects.filter(chat_type=CHAT_TYPE, chat_id__in=dm_ids, is_thread=False)
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
                user=user_id, chat_type=CHAT_TYPE, chat_id__in=dm_ids, is_thread=False
            )
        }

        # Build DM histories
        message_history_dict = {}
        flagged_messages = []
        for msg in raw_messages:
            chat_id = msg.dm.dm_id
            msg_dict = self.serialize_message(
                msg,
                team_id,
                team_name,
                user_id,
                thread_reply_counts,
                reactions_by_message,
                flagged_message_ids,
            )

            # Init or append messages
            if chat_id not in message_history_dict:
                message_history_dict[chat_id] = self.init_chat_dict(
                    msg, msg_dict, team_id, team_name, user_id
                )
            else:
                message_history_dict[chat_id]["messages"].append(msg_dict)
                if msg.ts_sent_at > message_history_dict[chat_id]["TSLastMessage"]:
                    message_history_dict[chat_id]["latestMessage"] = msg_dict
                    message_history_dict[chat_id]["latestMessageText"] = generate_first_line.get(
                        msg_dict["content"][0]
                    )
                    message_history_dict[chat_id]["TSLastMessage"] = msg.ts_sent_at

            if msg_dict["isFlagged"]:
                flagged_messages.append(
                    {
                        "flaggedMessageId": "{chat_type}-{chat_id}-{thread_id}-{message_id}".format(
                            chat_type=CHAT_TYPE,
                            chat_id=chat_id,
                            thread_id=0,
                            message_id=msg_dict["messageId"],
                        ),
                        "chatType": CHAT_TYPE,
                        "chatName": message_history_dict[chat_id]["chatName"],
                        "chatId": chat_id,
                        "threadId": 0,
                        "messageId": msg_dict["messageId"],
                        "contentText": generate_first_line.get(msg_dict["content"][0]),
                        "sender": msg_dict["sender"],
                        "dmPartnerUser": message_history_dict[chat_id]["dmPartnerUser"],
                        "project": msg_dict["project"],
                        "taskId": msg_dict["taskId"],
                        "tsSent": msg_dict["tsSent"],
                    }
                )

        # Add last read info
        for chat_id, chat in message_history_dict.items():
            chat["lastReadMessageId"] = last_read_map.get(chat_id, -1)
            if (CHAT_TYPE, chat_id) in pinned_dm_ids:
                chat["isPinned"] = True
            else:
                chat["isPinned"] = False

        return Response(
            {
                "chat_history": (
                    list(message_history_dict.values()) if message_history_dict.values() else []
                ),
                "flagged_messages": flagged_messages,
            },
            status=status.HTTP_200_OK,
        )

    def serialize_message(
        self,
        msg,
        team_id,
        team_name,
        current_user_id,
        reply_counts,
        reactions_by_message,
        flagged_message_ids,
    ):
        dm_id = msg.dm.dm_id
        message_id = msg.message_id

        return {
            "messageIdWithChatId": f"{dm_id}-{message_id}",
            "chatType": CHAT_TYPE,
            "chatId": dm_id,
            "messageId": message_id,
            "content": msg.message_body,
            "sender": {
                "userName": msg.sender.username,
                "userId": msg.sender.id,
                "avatarImgPath": msg.sender.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
            },
            "receiver": {
                "userName": msg.receiver.username,
                "userId": msg.receiver.id,
                "avatarImgPath": msg.receiver.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
            },
            "numReplies": reply_counts.get(f"{dm_id}-{message_id}", 0),
            "reactions": reactions_by_message.get(message_id, []),
            "taskId": msg.task.task_id if msg.task else None,
            "taskExist": True if msg.task else False,
            "taskStatus": msg.task.status if msg.task else None,
            "project": (
                {
                    "projectId": msg.task.project.project_id,
                    "projectName": msg.task.project.project_name,
                    "isJoined": True,
                    "systemUserId": msg.task.project.project_system_user.id,
                }
                if msg.task
                else {
                    "projectId": None,
                    "projectName": None,
                    "isJoined": False,
                    "systemUserId": None,
                }
            ),
            "isFlagged": (
                True if (CHAT_TYPE, dm_id, 0, message_id) in flagged_message_ids else False
            ),
            "tsSent": msg.ts_sent_at,
            "tsUpdated": msg.ts_updated_at,
        }

    def init_chat_dict(self, msg, first_message, team_id, team_name, current_user_id):
        chat_id = msg.dm.dm_id
        sender = msg.sender
        receiver = msg.receiver

        # Pick the "partner" (the other user in the DM)
        if str(sender.id) == str(current_user_id):
            partner = receiver
        else:
            partner = sender

        partner_dict = {
            "teamId": team_id,
            "teamName": team_name,
            "userName": partner.username,
            "userId": partner.id,
            "userEmail": partner.email,
            "avatarImgPath": partner.profile_image_file_name,
            "tsLastSeen": "",
            "tsJoined": "",
            "customStatus": "",
        }

        return {
            "chatId": chat_id,
            "chatName": partner.username,
            "chatType": 1,
            "dmPartnerUser": partner_dict,
            "messages": [first_message],
            "latestMessage": first_message,
            "latestMessageText": generate_first_line.get(first_message["content"][0]),
            "TSLastMessage": msg.ts_sent_at,
        }


class DMSingleMessageView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")
        dm_id = int(request.GET.get("dm_id"))
        message_id = int(request.GET.get("message_id"))

        data = {
            "team_id": team_id,
            "user_id": user_id,
            "dm_id": dm_id,
            "message_id": message_id,
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        dm = DMMessages.objects.filter(dm=dm_id, message_id=message_id, is_deleted=False)
        if len(dm) == 0:
            return Response(
                {"error": "DM not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        elif len(dm) > 1:
            return Response(
                {"error": "Duplicated DM found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            dm = dm[0]

        chat_master = UserChatMaster.objects.filter(user=user_id, team=team_id).values_list(
            "flagged_messages", flat=True
        )
        flagged_message_ids = (
            set(
                (c["chat_type"], c["chat_id"], c["thread_id"], c["message_id"])
                for c in chat_master[0]
            )
            if len(chat_master) > 0 and chat_master[0]
            else set()
        )

        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=dm_id, message_id=message_id, is_thread=False
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
            DMThreadMessages.objects.filter(dm=dm_id, thread_id=message_id, is_deleted=False)
            .values("parent_message_uid__dm__dm_id", "parent_message_uid__message_id")
            .annotate(num_of_replies=Count("thread_message_id"))
        )
        reply_count = 0
        if len(thread_reply_counts) == 1:
            reply_count = int(thread_reply_counts[0]["num_of_replies"])
        elif len(thread_reply_counts) > 1:
            print("Error!!!! thread_reply_counts has multiple thread found")

        raw_last_read_message_id = ReadStatus.objects.filter(
            user=user_id, chat_type=CHAT_TYPE, chat_id=dm_id, is_thread=False
        ).values_list("last_read_message_id")
        if len(raw_last_read_message_id) == 1:
            last_read_message_id = raw_last_read_message_id[0][0]
        else:
            last_read_message_id = -1

        message = {
            "messageIdWithChatId": f"{dm_id}-{message_id}",
            "chatType": CHAT_TYPE,
            "chatId": int(dm_id),
            "messageId": int(message_id),
            "content": dm.message_body,
            "sender": {
                "userId": dm.sender.id,
                "userName": dm.sender.username,
                "userEmail": dm.sender.email,
                "avatarImgPath": dm.sender.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": dm.sender.is_system_user,
            },
            "receiver": {
                "userId": dm.receiver.id,
                "userName": dm.receiver.username,
                "userEmail": dm.receiver.email,
                "avatarImgPath": dm.receiver.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": dm.receiver.is_system_user,
            },
            "numReplies": reply_count,
            "reactions": all_reactions,
            "taskId": dm.task.task_id if dm.task else None,
            "taskExist": True if dm.task else False,
            "taskStatus": dm.task.status if dm.task else None,
            "project": {
                "projectId": (dm.task.project.project_id if dm.task else None),
                "projectName": (dm.task.project.project_name if dm.task else None),
                "isJoined": True,
                "systemUserId": (dm.task.project.project_system_user.id if dm.task else None),
            },
            "tsSent": dm.ts_sent_at,
            "isFlagged": (
                True if (CHAT_TYPE, dm_id, 0, message_id) in flagged_message_ids else False
            ),
            "tsUpdated": dm.ts_updated_at,
            "lastReadMessageId": last_read_message_id,
        }

        return Response(message, status=status.HTTP_200_OK)

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

            serializer = DMMessagesSerializer(data=data)
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
        dm_id = request.data.get("dm_id")
        message_id = request.data.get("message_id")

        data = {
            "dm_id": dm_id,
            "message_id": message_id,
        }

        if res := validate_request_data(data):
            return res

        message = get_object_or_404(DMMessages, dm=dm_id, message_id=message_id)

        update_data = request.data.copy()
        # Remove None values from the updated_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

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

        serializer = DMMessagesSerializer(message, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            res = {**serializer.data, "last_read_message_id": last_read_message_id}
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


#############################
# DM Thread Messages views
#############################
class CheckDMThreadExistsView(AuthenticatedAPIView):
    def get(self, request):
        dm_id = int(request.GET.get("dm_id"))
        thread_id = int(request.GET.get("thread_id"))

        if not dm_id or not thread_id:
            return Response(
                {"error": "Both dm_id and thread_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a DM exists in any order
        exists = DMThreadMessages.objects.filter(Q(dm=dm_id, thread_id=thread_id)).exists()

        return Response({"dm_thread_exists": exists}, status=status.HTTP_200_OK)


class DMSingleThreadMessageView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")
        dm_id = int(request.GET.get("dm_id"))
        thread_id = int(request.GET.get("thread_id"))
        message_id = int(request.GET.get("message_id"))

        if not user_id or not dm_id or not thread_id or not message_id:
            return Response(
                {"error": "user_id, dm_id, thread_id and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        dm = DMThreadMessages.objects.filter(
            dm=dm_id, thread_id=thread_id, thread_message_id=message_id, is_deleted=False
        )
        if len(dm) == 0:
            return Response(
                {"error": "DM not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        elif len(dm) > 1:
            return Response(
                {"error": "Duplicated DM found"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        else:
            dm = dm[0]

        # Get chat master for this user
        chat_master = UserChatMaster.objects.filter(user=user_id, team=team_id).values_list(
            "flagged_messages", flat=True
        )
        flagged_message_ids = (
            set(
                (c["chat_type"], c["chat_id"], c["thread_id"], c["message_id"])
                for c in chat_master[0]
            )
            if len(chat_master) > 0 and chat_master[0]
            else set()
        )

        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=dm_id, message_id=message_id, is_thread=True
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

        contentText = generate_first_line.get(dm.thread_message_body[0])
        messageIdWithChatIdAndThreadId = f"{dm_id}-{thread_id}-{message_id}"
        message = {
            "messageIdWithChatIdAndThreadId": messageIdWithChatIdAndThreadId,
            "chatType": CHAT_TYPE,
            "chatId": int(dm_id),
            "threadId": dm.thread_id,
            "messageId": dm.thread_message_id,
            "content": dm.thread_message_body,
            "contentText": contentText,
            "sender": {
                "userId": dm.sender.id,
                "userName": dm.sender.username,
                "userEmail": dm.sender.email,
                "avatarImgPath": dm.sender.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": dm.sender.is_system_user,
            },
            "receiver": {
                "userId": dm.receiver.id,
                "userName": dm.receiver.username,
                "userEmail": dm.receiver.email,
                "avatarImgPath": dm.receiver.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": dm.receiver.is_system_user,
            },
            "reactions": all_reactions,
            "taskId": dm.parent_message_uid.task.task_id if dm.parent_message_uid.task else None,
            "taskExist": True if dm.parent_message_uid.task else False,
            "project": {
                "projectId": (
                    dm.parent_message_uid.task.project.project_id
                    if dm.parent_message_uid.task
                    else None
                ),
                "projectName": (
                    dm.parent_message_uid.task.project.project_name
                    if dm.parent_message_uid.task
                    else None
                ),
                "isJoined": True,
                "systemUserId": (
                    dm.parent_message_uid.task.project.project_system_user.id
                    if dm.parent_message_uid.task
                    else None
                ),
            },
            "isFlagged": (
                True if (CHAT_TYPE, dm_id, thread_id, message_id) in flagged_message_ids else False
            ),
            "tsSent": dm.ts_sent_at,
            "tsUpdated": dm.ts_updated_at,
        }

        return Response(message, status=status.HTTP_200_OK)

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

        if "ts_sent" in request.data:
            data["ts_sent_at"] = request.data["ts_sent"]

        serializer = DMThreadMessagesSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        dm_id = request.data.get("dm_id")
        thread_id = request.data.get("thread_id")
        message_id = request.data.get("message_id")

        if dm_id is None or message_id is None or thread_id is None:
            return Response(
                {"error": "dm_id , thread_id, and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = get_object_or_404(
            DMThreadMessages, dm=dm_id, thread_id=thread_id, thread_message_id=message_id
        )

        update_data = request.data.copy()
        # Remove None values from the updated_data if it's None
        if "message_body" in update_data and update_data["message_body"] is None:
            update_data.pop("message_body")

        # Change the field name
        if "message_body" in update_data:
            update_data["thread_message_body"] = update_data.pop("message_body")

        serializer = DMThreadMessagesSerializer(message, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class DMThreadMessagesByIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")
        dm_id = int(request.GET.get("dm_id"))
        thread_id = int(request.GET.get("thread_id"))

        if not team_id or not team_name or not dm_id or not thread_id:
            return Response(
                "dm_id and/or thread_id is not found", status=status.HTTP_400_BAD_REQUEST
            )

        # Fetch all messages where the dm_id matches and the user is involved
        raw_messages = DMThreadMessages.objects.filter(
            dm=dm_id, thread_id=thread_id, is_deleted=False
        ).order_by("ts_sent_at")

        # Get chat master for this user
        chat_master = UserChatMaster.objects.filter(user=user_id, team=team_id).values_list(
            "flagged_messages", flat=True
        )
        flagged_message_ids = (
            set(
                (c["chat_type"], c["chat_id"], c["thread_id"], c["message_id"])
                for c in chat_master[0]
            )
            if len(chat_master) > 0 and chat_master[0]
            else set()
        )

        # Fetch reactions
        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=dm_id, is_thread=True
        )

        task_exist = False
        thread_messages = []
        for raw_message in raw_messages:
            chat_id = int(raw_message.dm.dm_id)
            message_id = int(raw_message.thread_message_id)
            content = raw_message.thread_message_body
            sender_id = str(raw_message.sender.id)
            sender_name = str(raw_message.sender.username)
            sender_email = str(raw_message.sender.email)
            sender_avatar_img_path = raw_message.sender.profile_image_file_name
            is_system_user = raw_message.sender.is_system_user
            ts_sent = str(raw_message.ts_sent_at)
            ts_updated_at = str(raw_message.ts_updated_at)

            if message_id == 1:
                # fetch the first thread message reactions -> the parent message reaction.
                reactions = ReactionFact.objects.filter(
                    chat_type=CHAT_TYPE, chat_id=dm_id, is_thread=False, message_id=thread_id
                ).values_list(
                    "reaction_id",
                    "reaction_emoji",
                    "sender__username",
                    "sender__id",
                    "sender__profile_image_file_name",
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
                parent_message = DMMessages.objects.filter(dm=dm_id, message_id=thread_id)[0]
                ts_sent = parent_message.ts_sent_at
                ts_updated_at = parent_message.ts_updated_at

            contentText = generate_first_line.get(content[0])

            _task_id = (
                raw_message.parent_message_uid.task.task_id
                if raw_message.parent_message_uid.task
                else None
            )
            if _task_id:
                task_id = _task_id
                task_exist = True

            messageIdWithChatIdAndThreadId = f"{chat_id}-{thread_id}-{message_id}"
            new_message = {
                "messageIdWithChatIdAndThreadId": messageIdWithChatIdAndThreadId,
                "chatType": CHAT_TYPE,
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
                "reactions": all_reactions,
                "taskId": _task_id,
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
                "isFlagged": (
                    True
                    if (CHAT_TYPE, chat_id, thread_id, message_id) in flagged_message_ids
                    else False
                ),
                "tsSent": ts_sent,
                "tsUpdated": ts_updated_at,
            }
            thread_messages.append(new_message)

        _thread_messages = []
        if task_exist:
            for m in thread_messages:
                m["taskExist"] = True
                m["taskId"] = task_id
                _thread_messages.append(m)
        else:
            _thread_messages = thread_messages

        return Response(_thread_messages, status=status.HTTP_200_OK)
