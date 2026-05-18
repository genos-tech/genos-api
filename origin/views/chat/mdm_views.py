from django.core.cache import cache
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.reaction_models import *
from origin.models.chat.mdm_models import MDMMaster, MDMMembers, MDMMessages, MDMThreadMessages
from origin.models.chat.read_status_models import *
from origin.serializers.chat.mdm_serializers import *
from origin.views.chat.modules.common import generate_first_line
from origin.views.utils.request_validators import validate_request_data, validate_request_user
from origin.models.chat.chat_master_models import UserChatMaster

CHAT_TYPE = 4  # MDM chat type


#############################
# MDM Master views
#############################
class MDMMasterView(AuthenticatedAPIView):
    def post(self, request):
        owner_team = request.data.get("owner_team", None)
        owner_user = request.data.get("owner_user", None)
        display_name = request.data.get("display_name", None)
        member_ids = request.data.get("member_ids", [])

        if not owner_team or not owner_user:
            return Response(
                {"error": "owner_team and owner_user are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(member_ids) < 2:
            return Response(
                {"error": "At least 2 other members are required for a multi-user DM."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if an MDM with exact same members already exists
        # This is a simplified check - for exact matching, we'd need more complex logic
        existing_mdms = (
            MDMMembers.objects.filter(
                attendee_id__in=member_ids + [owner_user],
                mdm__owner_team=owner_team,
                mdm__is_deleted=False,
            )
            .values("mdm_id")
            .annotate(member_count=Count("attendee_id"))
            .filter(member_count=len(member_ids) + 1)
        )

        # Check if any existing MDM has exactly the same members
        for existing in existing_mdms:
            mdm_members = set(
                str(uid)
                for uid in MDMMembers.objects.filter(mdm_id=existing["mdm_id"]).values_list(
                    "attendee_id", flat=True
                )
            )
            expected_members = set(str(u) for u in [owner_user] + member_ids)
            if mdm_members == expected_members:
                return Response(
                    {"mdm_exists": True, "mdm_id": existing["mdm_id"]},
                    status=status.HTTP_200_OK,
                )

        # Create new MDM
        serializer = MDMMasterSerializer(
            data={
                "owner_user": owner_user,
                "owner_team": owner_team,
                "display_name": display_name,
            }
        )

        if serializer.is_valid():
            mdm = serializer.save()

            # Add owner as a member
            MDMMembers.objects.create(mdm=mdm, attendee_id=owner_user)

            # Add other members
            for member_id in member_ids:
                MDMMembers.objects.create(mdm=mdm, attendee_id=member_id)

            # Fetch member names to generate display name if not provided
            if not display_name:
                members = (
                    MDMMembers.objects.filter(mdm=mdm)
                    .select_related("attendee")
                    .values_list("attendee__username", flat=True)
                )
                generated_name = ", ".join(list(members)[:3])
                if len(members) > 3:
                    generated_name += f" +{len(members) - 3}"
                mdm.display_name = generated_name
                mdm.save(update_fields=["display_name"])

            return Response(
                {
                    "chatName": mdm.display_name,
                    "chatId": mdm.mdm_id,
                    "message": "Multi-user DM created successfully",
                },
                status=status.HTTP_201_CREATED,
            )

        error_messages = " ".join(
            [f"{field}: {' '.join(errors)}" for field, errors in serializer.errors.items()]
        )
        return Response({"message": error_messages}, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        data = {
            "team_id": request.GET.get("team_id"),
            "mdm_id": request.GET.get("mdm_id"),
        }

        if res := validate_request_data(data):
            return res

        mdm_data = MDMMaster.objects.filter(Q(mdm_id=data["mdm_id"])).values()

        if len(mdm_data) == 1:
            mdm_data = mdm_data[0]

            raw_mdm_members = (
                MDMMembers.objects.filter(Q(mdm_id=data["mdm_id"]))
                .order_by("attendee__email")
                .values(
                    "mdm__owner_team__team_id",
                    "mdm__owner_team__team_name",
                    "attendee__id",
                    "attendee__username",
                    "attendee__email",
                    "attendee__profile_image_file_name",
                    "attendee__is_offline_forced",
                    "attendee__role",
                    "attendee__base_country",
                    "attendee__custom_status",
                    "attendee__ts_created_at",
                    "attendee__is_system_user",
                )
            )
            mdm_members = []
            for attendee in raw_mdm_members:
                mdm_members.append(
                    {
                        "teamId": attendee["mdm__owner_team__team_id"],
                        "teamName": attendee["mdm__owner_team__team_name"],
                        "userId": attendee["attendee__id"],
                        "userName": attendee["attendee__username"],
                        "userEmail": attendee["attendee__email"],
                        "avatarImgPath": attendee["attendee__profile_image_file_name"],
                        "isOfflineForced": attendee["attendee__is_offline_forced"] or "",
                        "role": attendee["attendee__role"] or "",
                        "baseCountry": attendee["attendee__base_country"] or "",
                        "customStatus": attendee["attendee__custom_status"] or "",
                        "tsLastSeen": "",
                        "tsJoined": attendee["attendee__ts_created_at"],
                    }
                )

            res = {
                "mdmId": mdm_data["mdm_id"],
                "displayName": mdm_data["display_name"],
                "ownerUserId": mdm_data["owner_user_id"],
                "tsCreatedAt": mdm_data["ts_created_at"],
                "mdmMembers": mdm_members,
            }
            return Response(res, status=status.HTTP_200_OK)
        else:
            return Response(
                {"error": "MDM not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )


class CheckMDMExistsView(AuthenticatedAPIView):
    def get(self, request):
        mdm_id = int(request.GET.get("mdm_id"))

        if not mdm_id:
            return Response(
                {"error": "mdm_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        exists = MDMMaster.objects.filter(Q(mdm_id=mdm_id, is_deleted=False)).exists()

        return Response({"mdm_exists": exists}, status=status.HTTP_200_OK)


class AllMDMIdsView(AuthenticatedAPIView):
    def get(self, request):
        attendee_id = request.GET.get("attendee_id")

        if not attendee_id:
            return Response(
                {"error": "attendee_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = f"mdm:ids:{attendee_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached, status=status.HTTP_200_OK)

        mdm_ids = MDMMembers.objects.filter(
            Q(attendee=attendee_id, mdm__is_deleted=False)
        ).values_list("mdm", flat=True)
        payload = {"mdm_ids": list(set(mdm_ids))}

        cache.set(cache_key, payload, timeout=60)
        return Response(payload, status=status.HTTP_200_OK)


class MDMMembersView(AuthenticatedAPIView):
    def get(self, request):
        mdm_id = request.GET.get("mdm_id")
        if not mdm_id:
            return Response(
                {"error": "mdm_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = f"mdm:members:{mdm_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached, status=status.HTTP_200_OK)

        members = MDMMembers.objects.filter(mdm_id=mdm_id).values("attendee_id")
        payload = {"members": list(members)}

        cache.set(cache_key, payload, timeout=60)
        return Response(payload, status=status.HTTP_200_OK)

    def post(self, request):
        from origin.models.common.user_models import CustomUser

        mdm_id = request.data["mdm_id"]
        attendee_id = request.data["attendee_id"]
        data = {"mdm": mdm_id, "attendee": attendee_id}

        already_joined = MDMMembers.objects.filter(
            Q(mdm_id=data["mdm"], attendee_id=data["attendee"])
        ).exists()

        if already_joined:
            return Response(data, status=status.HTTP_201_CREATED)

        serializer = MDMMembersSerializer(data=data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        serializer.save()

        join_message_data = None
        try:
            mdm_obj = MDMMaster.objects.get(mdm_id=mdm_id)
            current_count = MDMMessages.objects.filter(mdm=mdm_obj).count()
            joined_body = [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Has joined", "styles": {}}],
                },
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "", "styles": {}}],
                },
            ]
            msg = MDMMessages.objects.create(
                mdm=mdm_obj,
                sender_id=attendee_id,
                message_id=current_count + 1,
                message_body=joined_body,
            )
            user = CustomUser.objects.get(id=attendee_id)
            team_id = request.data.get("team_id", "")
            team_name = request.data.get("team_name", "")
            join_message_data = {
                "chatId": int(mdm_id),
                "chatName": mdm_obj.display_name or f"MDM-{mdm_id}",
                "chatType": CHAT_TYPE,
                "messageId": msg.message_id,
                "content": joined_body,
                "contentText": "Has joined",
                "sender": {
                    "userId": str(user.id),
                    "userName": user.username,
                    "userEmail": user.email or "",
                    "avatarImgPath": user.profile_image_file_name or "",
                    "teamId": team_id,
                    "teamName": team_name,
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "customStatus": user.custom_status or "",
                },
                "tsSent": str(msg.ts_sent_at),
                "tsUpdated": str(msg.ts_updated_at),
            }
        except Exception as e:
            import logging

            logging.getLogger(__name__).error(f"Failed to create join message: {e}")

        response_data = {**serializer.data}
        if join_message_data:
            response_data["join_message"] = join_message_data
        return Response(response_data, status=status.HTTP_201_CREATED)


#############################
# MDM Messages views
#############################
class MDMHistoryView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        attendee_id = request.GET.get("user_id")

        data = {
            "team_id": team_id,
            "team_name": team_name,
            "attendee_id": attendee_id,
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["attendee_id"])):
            return res

        # Get chat master for this user
        pinned_chats = UserChatMaster.objects.filter(user=attendee_id, team=team_id).values_list(
            "pinned_chats", "flagged_messages"
        )
        pinned_mdm_ids = (
            set((c["chat_type"], c["chat_id"]) for c in pinned_chats[0][0])
            if len(pinned_chats) > 0 and pinned_chats[0] and pinned_chats[0][0]
            else set()
        )
        flagged_message_ids = (
            set(
                (c["chat_type"], c["chat_id"], c["thread_id"], c["message_id"])
                for c in pinned_chats[0][1]
            )
            if len(pinned_chats) > 0 and pinned_chats[0] and pinned_chats[0][1]
            else set()
        )

        # Get MDMs for this user
        if request.GET.get("mdm_id"):
            mdm_ids = [request.GET.get("mdm_id")]
        else:
            mdm_ids = list(
                MDMMembers.objects.filter(
                    Q(mdm__owner_team=team_id, attendee=attendee_id, mdm__is_deleted=False)
                ).values_list("mdm_id", flat=True)
            )

        if not mdm_ids:
            return Response(
                {
                    "chat_history": [],
                    "flagged_messages": [],
                },
                status=status.HTTP_200_OK,
            )

        # Get thread reply counts
        thread_reply_count_map = self._get_thread_reply_count_map(mdm_ids)

        # Get reactions
        reaction_map = self._get_reaction_map(mdm_ids)

        # Get messages
        raw_messages = MDMMessages.objects.filter(
            mdm_id__in=mdm_ids, is_deleted=False
        ).select_related("sender", "task__project", "mdm")

        # Build message history
        message_history_dict, flagged_messages = self._build_message_history(
            raw_messages,
            team_id,
            team_name,
            reaction_map,
            thread_reply_count_map,
            pinned_mdm_ids,
            flagged_message_ids,
        )

        # Ensure all MDMs appear even if they have no messages yet
        mdm_masters = MDMMaster.objects.filter(mdm_id__in=mdm_ids, is_deleted=False).values(
            "mdm_id", "display_name", "ts_created_at"
        )
        for mdm_info in mdm_masters:
            mid = mdm_info["mdm_id"]
            if mid not in message_history_dict:
                ts_created = str(mdm_info.get("ts_created_at") or timezone.now())
                message_history_dict[mid] = {
                    "chatId": mid,
                    "chatName": mdm_info["display_name"] or f"MDM-{mid}",
                    "chatType": CHAT_TYPE,
                    "dmPartnerUser": {
                        "userName": "",
                        "userId": "",
                        "avatarImgPath": "",
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "messages": [],
                    "latestMessage": None,
                    "latestMessageText": "",
                    "TSLastMessage": ts_created,
                    "isPinned": (CHAT_TYPE, mid) in pinned_mdm_ids,
                    "isFlagged": False,
                }

        # Add last read message IDs
        self._attach_last_read_ids(message_history_dict, attendee_id, mdm_ids)

        # Attach member info for each MDM
        self._attach_members(message_history_dict, mdm_ids, team_id, team_name)

        return Response(
            {
                "chat_history": (
                    list(message_history_dict.values()) if message_history_dict else []
                ),
                "flagged_messages": flagged_messages,
            },
            status=status.HTTP_200_OK,
        )

    def _get_thread_reply_count_map(self, mdm_ids):
        # Scope to the caller's mdm_ids so we don't scan the entire thread
        # table for chats the user isn't even in.
        counts = (
            MDMThreadMessages.objects.filter(
                is_deleted=False,
                parent_message_uid__mdm__mdm_id__in=mdm_ids,
            )
            .values("parent_message_uid__mdm__mdm_id", "parent_message_uid__message_id")
            .annotate(num_of_replies=Count("thread_message_id"))
        )
        return {
            f"{c['parent_message_uid__mdm__mdm_id']}-{c['parent_message_uid__message_id']}": c[
                "num_of_replies"
            ]
            for c in counts
        }

    def _get_reaction_map(self, mdm_ids):
        reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id__in=mdm_ids, is_thread=False
        ).values(
            "chat_id",
            "message_id",
            "reaction_id",
            "reaction_emoji",
            "sender__username",
            "sender__id",
            "sender__profile_image_file_name",
            "ts_created_at",
        )
        reaction_map = {}
        for r in reactions:
            reaction_map.setdefault((r["chat_id"], r["message_id"]), []).append(
                {
                    "id": int(r["reaction_id"]),
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
        return reaction_map

    def _build_message_history(
        self,
        raw_messages,
        team_id,
        team_name,
        reaction_map,
        thread_reply_count_map,
        pinned_mdm_ids,
        flagged_message_ids,
    ):
        message_history_dict = {}
        flagged_messages = []
        last_message_dict = {}
        ts_last_message_dict = {}

        for raw in raw_messages:
            chat_id = raw.mdm.mdm_id
            chat_name = raw.mdm.display_name or f"MDM-{chat_id}"
            message_id = raw.message_id

            new_message = {
                "messageIdWithChatId": f"{chat_id}-{message_id}",
                "chatType": CHAT_TYPE,
                "chatId": chat_id,
                "messageId": message_id,
                "content": raw.message_body,
                "sender": {
                    "teamId": team_id,
                    "teamName": team_name,
                    "userName": raw.sender.username,
                    "userEmail": raw.sender.email,
                    "userId": raw.sender.id,
                    "avatarImgPath": raw.sender.profile_image_file_name,
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "customStatus": "",
                },
                "numReplies": thread_reply_count_map.get(f"{chat_id}-{message_id}", 0),
                "reactions": reaction_map.get((chat_id, message_id), []),
                "taskId": raw.task.task_id if raw.task else None,
                "taskExist": True if raw.task else False,
                "taskStatus": raw.task.status if raw.task else None,
                "project": {
                    "projectId": raw.task.project.project_id if raw.task else None,
                    "projectName": raw.task.project.project_name if raw.task else None,
                    "isJoined": bool(raw.task),
                    "systemUserId": raw.task.project.project_system_user.id if raw.task else None,
                },
                "isFlagged": (CHAT_TYPE, chat_id, 0, message_id) in flagged_message_ids,
                "tsSent": str(raw.ts_sent_at),
                "tsUpdated": str(raw.ts_updated_at),
            }

            if new_message["isFlagged"]:
                flagged_messages.append(
                    {
                        "flaggedMessageId": f"{CHAT_TYPE}-{chat_id}-0-{message_id}",
                        "chatName": chat_name,
                        "chatType": CHAT_TYPE,
                        "chatId": chat_id,
                        "threadId": 0,
                        "messageId": new_message["messageId"],
                        "contentText": generate_first_line.get(new_message["content"][0]),
                        "sender": new_message["sender"],
                        "dmPartnerUser": {
                            "userName": "",
                            "userId": "",
                            "avatarImgPath": "",
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        },
                        "project": new_message["project"],
                        "taskId": new_message["taskId"],
                        "tsSent": new_message["tsSent"],
                    }
                )

            # Track last message per chat
            if chat_id in ts_last_message_dict:
                if str(raw.ts_sent_at) > ts_last_message_dict[chat_id]:
                    last_message_dict[chat_id] = new_message
                    ts_last_message_dict[chat_id] = str(raw.ts_sent_at)
            else:
                last_message_dict[chat_id] = new_message
                ts_last_message_dict[chat_id] = str(raw.ts_sent_at)

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
                    "chatType": CHAT_TYPE,
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
                    "isPinned": (CHAT_TYPE, chat_id) in pinned_mdm_ids,
                    "isFlagged": (CHAT_TYPE, chat_id, 0, message_id) in flagged_message_ids,
                }

        return message_history_dict, flagged_messages

    def _attach_last_read_ids(self, message_history_dict, attendee_id, mdm_ids):
        last_reads = ReadStatus.objects.filter(
            user=attendee_id, chat_type=CHAT_TYPE, chat_id__in=mdm_ids, is_thread=False
        ).values("chat_id", "last_read_message_id")

        last_read_map = {r["chat_id"]: r["last_read_message_id"] for r in last_reads}

        for chat_id, chat_data in message_history_dict.items():
            chat_data["lastReadMessageId"] = last_read_map.get(chat_id, -1)

    def _attach_members(self, message_history_dict, mdm_ids, team_id, team_name):
        members_qs = (
            MDMMembers.objects.filter(mdm_id__in=mdm_ids)
            .select_related("attendee")
            .values(
                "mdm_id",
                "attendee__id",
                "attendee__username",
                "attendee__email",
                "attendee__profile_image_file_name",
            )
        )
        members_map = {}
        for m in members_qs:
            members_map.setdefault(m["mdm_id"], []).append(
                {
                    "userId": str(m["attendee__id"]),
                    "userName": m["attendee__username"],
                    "userEmail": m["attendee__email"] or "",
                    "avatarImgPath": m["attendee__profile_image_file_name"] or "",
                    "teamId": team_id,
                    "teamName": team_name,
                }
            )

        for chat_id, chat_data in message_history_dict.items():
            chat_data["mdmMembers"] = members_map.get(chat_id, [])


class MDMSingleMessageView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")
        mdm_id = int(request.GET.get("mdm_id"))
        message_id = int(request.GET.get("message_id"))

        data = {
            "team_id": team_id,
            "user_id": user_id,
            "mdm_id": mdm_id,
            "message_id": message_id,
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        mdm = MDMMessages.objects.filter(mdm=mdm_id, message_id=message_id, is_deleted=False)
        if len(mdm) == 0:
            return Response({"error": "MDM message not found"}, status=status.HTTP_400_BAD_REQUEST)
        elif len(mdm) > 1:
            return Response(
                {"error": "Duplicate MDM message found"}, status=status.HTTP_400_BAD_REQUEST
            )
        else:
            mdm = mdm[0]

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

        # select_related("sender") collapses the per-row sender lookup into
        # the same SQL — without it the loop below would issue one query per
        # reaction (N+1).
        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=mdm_id, message_id=message_id, is_thread=False
        ).select_related("sender")
        all_reactions = []
        for raw_reaction in raw_reactions:
            all_reactions.append(
                {
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
            )

        thread_reply_counts = (
            MDMThreadMessages.objects.filter(mdm=mdm_id, thread_id=message_id, is_deleted=False)
            .values("parent_message_uid__mdm__mdm_id", "parent_message_uid__message_id")
            .annotate(num_of_replies=Count("thread_message_id"))
        )
        reply_count = (
            int(thread_reply_counts[0]["num_of_replies"]) if len(thread_reply_counts) == 1 else 0
        )

        raw_last_read_message_id = ReadStatus.objects.filter(
            user=user_id, chat_type=CHAT_TYPE, chat_id=mdm_id, is_thread=False
        ).values_list("last_read_message_id")
        last_read_message_id = (
            raw_last_read_message_id[0][0] if len(raw_last_read_message_id) == 1 else -1
        )

        message = {
            "messageIdWithChatId": f"{mdm_id}-{message_id}",
            "chatType": CHAT_TYPE,
            "chatId": int(mdm_id),
            "messageId": int(message_id),
            "content": mdm.message_body,
            "sender": {
                "userId": mdm.sender.id,
                "userName": mdm.sender.username,
                "userEmail": mdm.sender.email,
                "avatarImgPath": mdm.sender.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": mdm.sender.is_system_user,
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
            "taskId": mdm.task.task_id if mdm.task else None,
            "taskExist": True if mdm.task else False,
            "taskStatus": mdm.task.status if mdm.task else None,
            "project": {
                "projectId": mdm.task.project.project_id if mdm.task else None,
                "projectName": mdm.task.project.project_name if mdm.task else None,
                "isJoined": True,
                "systemUserId": mdm.task.project.project_system_user.id if mdm.task else None,
            },
            "isFlagged": (CHAT_TYPE, mdm_id, 0, message_id) in flagged_message_ids,
            "tsSent": mdm.ts_sent_at,
            "tsUpdated": mdm.ts_updated_at,
            "lastReadMessageId": last_read_message_id,
        }

        return Response(message, status=status.HTTP_200_OK)

    def post(self, request):
        mdm = MDMMaster.objects.filter(mdm_id=request.data["mdm_id"])
        current_message_count = (
            MDMMessages.objects.filter(mdm=mdm[0]).count() if len(mdm) > 0 else 0
        )

        is_init = request.data.get("is_init")
        if (is_init in [None, False]) or (is_init == True and current_message_count == 0):
            data = {
                "mdm": request.data["mdm_id"],
                "sender": request.data["sender_id"],
                "message_id": current_message_count + 1,
                "message_body": request.data["message_body"],
            }

            raw_last_read_message_id = ReadStatus.objects.filter(
                user=request.user.id,
                chat_type=CHAT_TYPE,
                chat_id=request.data["mdm_id"],
                is_thread=False,
            ).values_list("last_read_message_id")
            last_read_message_id = (
                raw_last_read_message_id[0][0] if len(raw_last_read_message_id) == 1 else -1
            )

            serializer = MDMMessagesSerializer(data=data)
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
        mdm_id = request.data.get("mdm_id")
        message_id = request.data.get("message_id")

        if mdm_id is None or message_id is None:
            return Response(
                {"error": "mdm_id and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = get_object_or_404(MDMMessages, mdm=mdm_id, message_id=message_id)

        update_data = request.data.copy()
        if "message_body" in update_data and update_data["message_body"] is None:
            update_data.pop("message_body")
        if "task_id" in update_data and update_data["task_id"] is None:
            update_data.pop("task_id")
        if "task_id" in update_data:
            update_data["task"] = update_data.pop("task_id")

        raw_last_read_message_id = ReadStatus.objects.filter(
            user=request.user.id,
            chat_type=CHAT_TYPE,
            chat_id=request.data["mdm_id"],
            is_thread=False,
        ).values_list("last_read_message_id")
        last_read_message_id = (
            raw_last_read_message_id[0][0] if len(raw_last_read_message_id) == 1 else -1
        )

        serializer = MDMMessagesSerializer(message, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            res = {**serializer.data, "last_read_message_id": last_read_message_id}
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


#############################
# MDM Thread Messages views
#############################
class CheckMDMThreadExistsView(AuthenticatedAPIView):
    def get(self, request):
        mdm_id = int(request.GET.get("mdm_id"))
        thread_id = int(request.GET.get("thread_id"))

        if not mdm_id or not thread_id:
            return Response(
                {"error": "Both mdm_id and thread_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        exists = MDMThreadMessages.objects.filter(Q(mdm=mdm_id, thread_id=thread_id)).exists()

        return Response({"mdm_thread_exists": exists}, status=status.HTTP_200_OK)


class MDMSingleThreadMessageView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")
        mdm_id = int(request.GET.get("mdm_id"))
        thread_id = int(request.GET.get("thread_id"))
        message_id = int(request.GET.get("message_id"))

        data = {
            "team_id": team_id,
            "user_id": user_id,
            "mdm_id": mdm_id,
            "thread_id": thread_id,
            "message_id": message_id,
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        mdm = MDMThreadMessages.objects.filter(
            mdm=mdm_id, thread_id=thread_id, thread_message_id=message_id, is_deleted=False
        )
        if len(mdm) == 0:
            return Response(
                {"error": "MDM thread message not found"}, status=status.HTTP_400_BAD_REQUEST
            )
        elif len(mdm) > 1:
            return Response(
                {"error": "Duplicate MDM thread message found"}, status=status.HTTP_400_BAD_REQUEST
            )
        else:
            mdm = mdm[0]

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
            chat_type=CHAT_TYPE,
            chat_id=mdm_id,
            message_id=message_id,
            is_thread=True,
            thread_id=thread_id,
        ).select_related("sender")
        all_reactions = []
        for raw_reaction in raw_reactions:
            all_reactions.append(
                {
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
            )

        contentText = generate_first_line.get(mdm.thread_message_body[0])
        messageIdWithChatIdAndThreadId = f"{mdm_id}-{thread_id}-{message_id}"

        message = {
            "messageIdWithChatIdAndThreadId": messageIdWithChatIdAndThreadId,
            "chatType": CHAT_TYPE,
            "chatId": int(mdm_id),
            "threadId": mdm.thread_id,
            "messageId": mdm.thread_message_id,
            "content": mdm.thread_message_body,
            "contentText": contentText,
            "sender": {
                "userId": mdm.sender.id,
                "userName": mdm.sender.username,
                "userEmail": mdm.sender.email,
                "avatarImgPath": mdm.sender.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
                "isSystemUser": mdm.sender.is_system_user,
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
            "taskId": mdm.parent_message_uid.task.task_id if mdm.parent_message_uid.task else None,
            "taskExist": True if mdm.parent_message_uid.task else False,
            "project": {
                "projectId": (
                    mdm.parent_message_uid.task.project.project_id
                    if mdm.parent_message_uid.task
                    else None
                ),
                "projectName": (
                    mdm.parent_message_uid.task.project.project_name
                    if mdm.parent_message_uid.task
                    else None
                ),
                "isJoined": True,
                "systemUserId": (
                    mdm.parent_message_uid.task.project.project_system_user.id
                    if mdm.parent_message_uid.task
                    else None
                ),
            },
            "isFlagged": (CHAT_TYPE, mdm_id, thread_id, message_id) in flagged_message_ids,
            "tsSent": mdm.ts_sent_at,
            "tsUpdated": mdm.ts_updated_at,
        }

        return Response(message, status=status.HTTP_200_OK)

    def post(self, request):
        mdm = MDMMaster.objects.filter(mdm_id=request.data["mdm_id"])
        if len(mdm) > 0:
            current_thread_message_count = MDMThreadMessages.objects.filter(
                mdm=mdm[0], thread_id=request.data["thread_id"]
            ).count()
        else:
            return Response("MDM is not found", status=status.HTTP_400_BAD_REQUEST)

        data = {
            "mdm": request.data["mdm_id"],
            "thread_id": request.data["thread_id"],
            "sender": request.data["sender_id"],
            "thread_message_id": current_thread_message_count + 1,
            "thread_message_body": request.data["message_body"],
            "parent_message_uid": f"{request.data['mdm_id']}-{request.data['parent_message_id']}",
            "task": request.data.get("task"),
        }

        serializer = MDMThreadMessagesSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        mdm_id = request.data.get("mdm_id")
        thread_id = request.data.get("thread_id")
        message_id = request.data.get("message_id")

        if mdm_id is None or message_id is None or thread_id is None:
            return Response(
                {"error": "mdm_id, thread_id, and message_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = get_object_or_404(
            MDMThreadMessages, mdm=mdm_id, thread_id=thread_id, thread_message_id=message_id
        )

        update_data = request.data.copy()
        if "message_body" in update_data and update_data["message_body"] is None:
            update_data.pop("message_body")
        if "message_body" in update_data:
            update_data["thread_message_body"] = update_data.pop("message_body")

        serializer = MDMThreadMessagesSerializer(message, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class MDMThreadMessagesByIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")
        mdm_id = int(request.GET.get("mdm_id"))
        thread_id = int(request.GET.get("thread_id"))

        data = {
            "team_id": team_id,
            "team_name": team_name,
            "user_id": user_id,
            "mdm_id": mdm_id,
            "thread_id": thread_id,
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        # select_related expands the FK chain accessed in the loop below
        # (raw_message.mdm, raw_message.sender, raw_message.parent_message_uid.task)
        # so the whole thread loads in a single SQL with joins.
        raw_messages = (
            MDMThreadMessages.objects.filter(mdm=mdm_id, thread_id=thread_id, is_deleted=False)
            .select_related("mdm", "sender", "parent_message_uid__task")
            .order_by("ts_sent_at")
        )

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
            chat_type=CHAT_TYPE, chat_id=mdm_id, is_thread=True, thread_id=thread_id
        )

        thread_messages = []
        for raw_message in raw_messages:
            chat_id = int(raw_message.mdm.mdm_id)
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
                reactions = ReactionFact.objects.filter(
                    chat_type=CHAT_TYPE, chat_id=mdm_id, is_thread=False, message_id=thread_id
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
                all_reactions.append(
                    {
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
                )

            if raw_message.thread_message_id == 1:
                parent_message = MDMMessages.objects.filter(mdm=mdm_id, message_id=thread_id)[0]
                ts_sent = parent_message.ts_sent_at
                ts_updated_at = parent_message.ts_updated_at

            contentText = generate_first_line.get(content[0])
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
                "taskId": (
                    raw_message.parent_message_uid.task.task_id
                    if raw_message.parent_message_uid.task
                    else None
                ),
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
                "isFlagged": (CHAT_TYPE, chat_id, thread_id, message_id) in flagged_message_ids,
                "tsSent": ts_sent,
                "tsUpdated": ts_updated_at,
            }
            thread_messages.append(new_message)

        return Response(thread_messages, status=status.HTTP_200_OK)
