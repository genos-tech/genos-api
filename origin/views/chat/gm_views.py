from django.core.cache import cache
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.reaction_models import *
from origin.models.chat.gm_models import GMMaster, GMMembers, GMMessages, GMThreadMessages
from origin.models.chat.read_status_models import *
from origin.serializers.chat.gm_serializers import *
from origin.models.common.inbox_models import InboxItems
from origin.views.chat.modules.common import generate_first_line
from origin.views.utils.request_validators import validate_request_data, validate_request_user
from origin.models.chat.chat_master_models import UserChatMaster

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
                    "isPrivate": serializer.data["is_private"],
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

    def get(self, request):

        data = {
            "team_id": request.GET.get("team_id"),
            "gm_id": request.GET.get("gm_id"),
        }

        if res := validate_request_data(data):
            return res

        gm_data = GMMaster.objects.filter(Q(gm_id=data["gm_id"])).values()

        if len(gm_data) == 1:
            gm_data = gm_data[0]

            raw_gm_members = (
                GMMembers.objects.filter(Q(gm_id=data["gm_id"]))
                .order_by("attendee__email")
                .values(
                    "gm__owner_team__team_id",
                    "gm__owner_team__team_name",
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
            gm_members = []
            for attendee in raw_gm_members:
                gm_members.append(
                    {
                        "teamId": attendee["gm__owner_team__team_id"],
                        "teamName": attendee["gm__owner_team__team_name"],
                        "userId": attendee["attendee__id"],
                        "userName": attendee["attendee__username"],
                        "userEmail": attendee["attendee__email"],
                        "avatarImgPath": attendee["attendee__profile_image_file_name"],
                        "isOfflineForced": (
                            attendee["attendee__is_offline_forced"]
                            if attendee["attendee__is_offline_forced"]
                            else ""
                        ),
                        "role": (attendee["attendee__role"] if attendee["attendee__role"] else ""),
                        "baseCountry": (
                            attendee["attendee__base_country"]
                            if attendee["attendee__base_country"]
                            else ""
                        ),
                        "customStatus": (
                            attendee["attendee__custom_status"]
                            if attendee["attendee__custom_status"]
                            else ""
                        ),
                        "tsLastSeen": "",
                        "tsJoined": attendee["attendee__ts_created_at"],
                    }
                )

            res = {
                "gmId": gm_data["gm_id"],
                "gmName": gm_data["group_name"],
                "ownerUserId": gm_data["owner_user_id"],
                "profileImagePath": gm_data["profile_image_file_name"],
                "isPrivate": gm_data["is_private"],
                "tsCreatedAt": gm_data["ts_created_at"],
                "gmMembers": gm_members,
            }
            return Response(res, status=status.HTTP_200_OK)
        else:
            return Response(
                {"error": "GM not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )


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


class LeaveGMView(AuthenticatedAPIView):
    """Hard-delete the requester's GM membership.

    Owners can't leave (would orphan the group). `GMMembers` has no
    soft-delete flag, so the row is removed outright; re-join uses the
    existing `GMMembersView.post` which idempotently re-inserts.
    """

    def post(self, request):
        gm_id = request.data.get("gm_id")
        attendee_id = request.data.get("attendee_id")
        if not gm_id or not attendee_id:
            return Response(
                {"error": "gm_id and attendee_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if str(request.user.id) != str(attendee_id):
            return Response(
                {"error": "You can only leave a group on your own behalf."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            gm = GMMaster.objects.get(gm_id=gm_id)
        except GMMaster.DoesNotExist:
            return Response(
                {"error": "Group not found."}, status=status.HTTP_404_NOT_FOUND
            )

        if gm.owner_user and str(gm.owner_user.id) == str(attendee_id):
            return Response(
                {"error": "The group owner cannot leave the group."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        deleted, _ = GMMembers.objects.filter(gm_id=gm_id, attendee_id=attendee_id).delete()
        if deleted == 0:
            return Response(
                {"error": "You are not a member of this group."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {"gm_id": gm_id, "attendee_id": attendee_id}, status=status.HTTP_200_OK
        )


class JoinGMFromInboxView(AuthenticatedAPIView):
    def post(self, request):
        inbox_item_id = int(request.data["item_id"])

        inbox_item = InboxItems.objects.filter(item_id=inbox_item_id).values_list(
            "sender", "item_optionals"
        )[0]

        attendee_id = inbox_item[0]
        gm_id = inbox_item[1]["gm_id"]
        gm_name = inbox_item[1]["gm_name"]

        # Check if the attendee is not joined yet.
        is_joined = GMMembers.objects.filter(Q(gm_id=gm_id, attendee_id=attendee_id)).exists()

        data = {"gm": gm_id, "attendee": attendee_id}
        if is_joined:
            data["gmId"] = gm_id
            data["gmName"] = gm_name
            return Response(data, status=status.HTTP_201_CREATED)
        else:
            serializer = GMMembersSerializer(data=data)
            if serializer.is_valid():
                serializer.save()
                res = serializer.data
                res["gmId"] = gm_id
                res["gmName"] = gm_name
                return Response(res, status=status.HTTP_201_CREATED)

        error = serializer.errors
        error["hint"] = f"Failed to join GM: {gm_id}"
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class AllGMIdsView(AuthenticatedAPIView):
    def get(self, request):
        attendee_id = request.GET.get("attendee_id")

        if not attendee_id:
            return Response(
                {"error": "attendee_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cache_key = f"gm:ids:{attendee_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached, status=status.HTTP_200_OK)

        gm_ids = GMMembers.objects.filter(Q(attendee=attendee_id)).values_list("gm")

        connected_set = set()
        for (group_id,) in gm_ids:
            connected_set.add(group_id)

        payload = {"gm_ids": list(connected_set)}
        cache.set(cache_key, payload, timeout=60)
        return Response(payload, status=status.HTTP_200_OK)


#############################
# GM Messages views
#############################
class GMSingleMessageView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")
        gm_id = int(request.GET.get("gm_id"))
        message_id = int(request.GET.get("message_id"))

        data = {
            "team_id": team_id,
            "user_id": user_id,
            "gm_id": gm_id,
            "message_id": message_id,
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        gm = GMMessages.objects.filter(gm=gm_id, message_id=message_id, is_deleted=False)
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
            chat_type=CHAT_TYPE, chat_id=gm_id, message_id=message_id, is_thread=False
        ).select_related("sender")
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
            GMThreadMessages.objects.filter(gm=gm_id, thread_id=message_id, is_deleted=False)
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
            "chatType": CHAT_TYPE,
            "chatId": int(gm_id),
            "messageId": int(message_id),
            "content": gm.message_body,
            "sender": {
                "userId": gm.sender.id,
                "userName": gm.sender.username,
                "userEmail": gm.sender.email,
                "avatarImgPath": gm.sender.profile_image_file_name,
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
            "reactions": all_reactions,
            "taskId": gm.task.task_id if gm.task else None,
            "taskExist": True if gm.task else False,
            "taskStatus": gm.task.status if gm.task else None,
            "project": {
                "projectId": (gm.task.project.project_id if gm.task else None),
                "projectName": (gm.task.project.project_name if gm.task else None),
                "isJoined": True,
                "systemUserId": (gm.task.project.project_system_user.id if gm.task else None),
            },
            "isFlagged": (
                True if (CHAT_TYPE, gm_id, 0, message_id) in flagged_message_ids else False
            ),
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
            chat_id=request.data["gm_id"],
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
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")
        gm_id = int(request.GET.get("gm_id"))
        thread_id = int(request.GET.get("thread_id"))
        message_id = int(request.GET.get("message_id"))

        data = {
            "team_id": team_id,
            "user_id": user_id,
            "gm_id": gm_id,
            "thread_id": thread_id,
            "message_id": message_id,
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        gm = GMThreadMessages.objects.filter(
            gm=gm_id, thread_id=thread_id, thread_message_id=message_id, is_deleted=False
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
            chat_id=gm_id,
            message_id=message_id,
            is_thread=True,
            thread_id=thread_id,
        ).select_related("sender")
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

        contentText = generate_first_line.get(gm.thread_message_body[0])
        messageIdWithChatIdAndThreadId = f"{gm_id}-{thread_id}-{message_id}"
        message = {
            "messageIdWithChatIdAndThreadId": messageIdWithChatIdAndThreadId,
            "chatType": CHAT_TYPE,
            "chatId": int(gm_id),
            "threadId": gm.thread_id,
            "messageId": gm.thread_message_id,
            "content": gm.thread_message_body,
            "contentText": contentText,
            "sender": {
                "userId": gm.sender.id,
                "userName": gm.sender.username,
                "userEmail": gm.sender.email,
                "avatarImgPath": gm.sender.profile_image_file_name,
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
            "reactions": all_reactions,
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
            "isFlagged": (
                True if (CHAT_TYPE, gm_id, thread_id, message_id) in flagged_message_ids else False
            ),
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
            return Response("gm is not found", status=status.HTTP_400_BAD_REQUEST)

        data = {
            "gm": request.data["gm_id"],
            "thread_id": request.data["thread_id"],
            "sender": request.data["sender_id"],
            "thread_message_id": current_thread_message_count + 1,
            "thread_message_body": request.data["message_body"],
            "parent_message_uid": "{gm_id}-{parent_message_id}".format(
                gm_id=request.data["gm_id"], parent_message_id=request.data["parent_message_id"]
            ),
            "task": request.data.get("task"),
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

        data = {
            "team_id": team_id,
            "team_name": team_name,
            "user_id": user_id,
            "gm_id": gm_id,
            "thread_id": thread_id,
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        # Fetch all messages where the gm_id matches and the user is involved.
        # select_related expands the FK chain accessed in the loop below so the
        # whole thread loads in a single SQL with joins.
        raw_messages = (
            GMThreadMessages.objects.filter(gm=gm_id, thread_id=thread_id, is_deleted=False)
            .select_related("gm", "sender", "parent_message_uid__task")
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

        # Fetch reactions
        raw_reactions = ReactionFact.objects.filter(
            chat_type=CHAT_TYPE, chat_id=gm_id, is_thread=True, thread_id=thread_id
        )

        thread_messages = []
        for raw_message in raw_messages:
            chat_id = int(raw_message.gm.gm_id)
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
                    chat_type=CHAT_TYPE, chat_id=gm_id, is_thread=False, message_id=thread_id
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
                parent_message = GMMessages.objects.filter(gm=gm_id, message_id=thread_id)[0]
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
                "isFlagged": (
                    True
                    if (CHAT_TYPE, chat_id, thread_id, message_id) in flagged_message_ids
                    else False
                ),
                "tsSent": ts_sent,
                "tsUpdated": ts_updated_at,
            }
            thread_messages.append(new_message)

        return Response(thread_messages, status=status.HTTP_200_OK)


class GMThreadMessagesByTaskIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")
        gm_id = int(request.GET.get("gm_id"))
        task_id = int(request.GET.get("task_id"))

        data = {
            "team_id": team_id,
            "team_name": team_name,
            "user_id": user_id,
            "gm_id": gm_id,
            "task_id": task_id,
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        # Fetch all messages where the gm_id matches and the task_id matches and the user is involved.
        raw_messages = (
            GMThreadMessages.objects.filter(
                gm=gm_id, parent_message_uid__task=task_id, is_deleted=False
            )
            .select_related("gm", "sender", "parent_message_uid__task")
            .order_by("ts_sent_at")
        )

        thread_id = raw_messages[0].thread_id
        if not thread_id:
            return Response("thread_id is not found", status=status.HTTP_400_BAD_REQUEST)

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
            chat_type=CHAT_TYPE, chat_id=gm_id, is_thread=True, thread_id=thread_id
        )

        thread_messages = []
        for raw_message in raw_messages:
            chat_id = int(raw_message.gm.gm_id)
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
                    chat_type=CHAT_TYPE, chat_id=gm_id, is_thread=False, message_id=thread_id
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
                parent_message = GMMessages.objects.filter(gm=gm_id, message_id=thread_id)[0]
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
                "isFlagged": (
                    True
                    if (CHAT_TYPE, chat_id, thread_id, message_id) in flagged_message_ids
                    else False
                ),
                "tsSent": ts_sent,
                "tsUpdated": ts_updated_at,
            }
            thread_messages.append(new_message)

        return Response(thread_messages, status=status.HTTP_200_OK)


class GMProfileImageView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser]

    def put(self, request):
        gm_id = request.POST.get("gm_id")
        profile_image = request.FILES.get("profile_image")

        data = {
            "gm_id": gm_id,
            "profile_image": profile_image,
        }

        if res := validate_request_data(data):
            return res

        gm_data = GMMaster.objects.get(gm_id=gm_id)

        # Only update the FileField
        new_profile_image_data = {
            "profile_image_url": profile_image,
        }

        serializer = GMMasterSerializer(gm_data, data=new_profile_image_data, partial=True)
        if serializer.is_valid():
            saved_user = serializer.save()

            # At this point, Django has stored the file, possibly renamed
            # Now get the actual stored filename
            stored_file_name = saved_user.profile_image_url.name.split("/")[-1]
            saved_user.profile_image_file_name = f"gm_profiles/{gm_id}/{stored_file_name}"
            saved_user.save(update_fields=["profile_image_file_name"])

            return Response(GMMasterSerializer(saved_user).data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
