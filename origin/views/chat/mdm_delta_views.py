"""Phase 2 incremental-sync endpoints for MDM (chat_type=4).

Sister file to dm_delta_views.py / gm_delta_views.py — three-endpoint
split adapted to MDMMaster / MDMMessages / MDMThreadMessages and the
MDM-specific shape (display_name as chatName, mdmMembers list on each
chat row).
"""

from collections import defaultdict

from django.db.models import Count, OuterRef, Q, Subquery
from rest_framework.response import Response
from rest_framework import status

from origin.models.chat.chat_master_models import UserChatMaster
from origin.models.chat.mdm_models import (
    MDMMaster,
    MDMMembers,
    MDMMessages,
    MDMThreadMessages,
)
from origin.models.chat.reaction_models import ReactionFact
from origin.models.chat.read_status_models import ReadStatus
from origin.views.chat.modules.common import generate_first_line
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.incremental import (
    build_delta_response,
    capture_server_time,
    parse_since,
)
from origin.views.utils.request_validators import (
    validate_request_data,
    validate_request_user,
)

CHAT_TYPE = 4


def _build_reactions_by_key(reaction_rows, *, is_thread):
    result = defaultdict(list)
    for r in reaction_rows:
        key = (
            (r["chat_id"], r["thread_id"], r["message_id"])
            if is_thread
            else (r["chat_id"], r["message_id"])
        )
        result[key].append(
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
    return result


def _serialize_message(msg, reactions, num_replies, flagged_set):
    mdm_id = msg.mdm_id
    return {
        "messageIdWithChatId": f"{mdm_id}-{msg.message_id}",
        "chatType": CHAT_TYPE,
        "chatId": mdm_id,
        "messageId": msg.message_id,
        "content": msg.message_body,
        "sender": {
            "userId": msg.sender.id,
            "userName": msg.sender.username,
            "userEmail": msg.sender.email,
            "avatarImgPath": msg.sender.profile_image_file_name,
            "tsLastSeen": "",
            "tsJoined": "",
            "customStatus": "",
            "isSystemUser": msg.sender.is_system_user,
        },
        "numReplies": num_replies,
        "reactions": reactions,
        "taskId": msg.task.task_id if msg.task else None,
        "taskExist": bool(msg.task),
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
        "isFlagged": (CHAT_TYPE, mdm_id, 0, msg.message_id) in flagged_set,
        "tsSent": str(msg.ts_sent_at),
        "tsUpdated": str(msg.ts_updated_at),
        "isDeleted": msg.is_deleted,
    }


def _serialize_thread_message(tm, reactions, flagged_set):
    mdm_id = tm.mdm_id
    parent_task = tm.parent_message_uid.task if tm.parent_message_uid_id else None
    return {
        "messageIdWithChatIdAndThreadId": f"{mdm_id}-{tm.thread_id}-{tm.thread_message_id}",
        "chatType": CHAT_TYPE,
        "chatId": mdm_id,
        "threadId": tm.thread_id,
        "messageId": tm.thread_message_id,
        "content": tm.thread_message_body,
        "contentText": (
            generate_first_line.get(tm.thread_message_body[0]) if tm.thread_message_body else ""
        ),
        "sender": {
            "userId": tm.sender.id,
            "userName": tm.sender.username,
            "userEmail": tm.sender.email,
            "avatarImgPath": tm.sender.profile_image_file_name,
            "tsLastSeen": "",
            "tsJoined": "",
            "customStatus": "",
            "isSystemUser": tm.sender.is_system_user,
        },
        "reactions": reactions,
        "taskId": parent_task.task_id if parent_task else None,
        "taskExist": bool(parent_task),
        "project": {
            "projectId": parent_task.project.project_id if parent_task else None,
            "projectName": parent_task.project.project_name if parent_task else None,
            "isJoined": bool(parent_task),
            "systemUserId": parent_task.project.project_system_user.id if parent_task else None,
        },
        "isFlagged": (CHAT_TYPE, mdm_id, tm.thread_id, tm.thread_message_id) in flagged_set,
        "tsSent": str(tm.ts_sent_at),
        "tsUpdated": str(tm.ts_updated_at),
        "isDeleted": tm.is_deleted,
    }


class MDMChatsListView(AuthenticatedAPIView):
    """GET /api/v2/mdm/chats/ — full-fetch MDM chat list with derived fields."""

    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")

        data = {"team_id": team_id, "team_name": team_name, "user_id": user_id}
        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        mdm_ids = list(
            MDMMembers.objects.filter(Q(mdm__owner_team=team_id, attendee=user_id)).values_list(
                "mdm_id", flat=True
            )
        )
        if not mdm_ids:
            return Response({"chats": [], "flagged_messages": []})

        chat_master_row = (
            UserChatMaster.objects.filter(user=user_id, team=team_id)
            .values_list("pinned_chats", "flagged_messages")
            .first()
        )
        pinned_set = (
            {(c["chat_type"], c["chat_id"]) for c in (chat_master_row[0] or [])}
            if chat_master_row
            else set()
        )
        raw_flagged = chat_master_row[1] if chat_master_row else []

        last_read_map = {
            rs.chat_id: rs.last_read_message_id
            for rs in ReadStatus.objects.filter(
                user=user_id, chat_type=CHAT_TYPE, chat_id__in=mdm_ids, is_thread=False
            )
        }

        latest_subq = MDMMessages.objects.filter(mdm=OuterRef("pk"), is_deleted=False).order_by(
            "-ts_sent_at"
        )
        mdms = list(
            MDMMaster.objects.filter(mdm_id__in=mdm_ids).annotate(
                latest_msg_id=Subquery(latest_subq.values("message_id")[:1])
            )
        )

        latest_keys = [
            f"{m.mdm_id}-{m.latest_msg_id}" for m in mdms if m.latest_msg_id is not None
        ]
        latest_msgs = MDMMessages.objects.filter(uid__in=latest_keys).select_related(
            "mdm", "sender", "task", "task__project"
        )
        latest_msg_by_chat_id = {x.mdm_id: x for x in latest_msgs}

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
        members_by_chat = defaultdict(list)
        for m in members_qs:
            members_by_chat[m["mdm_id"]].append(
                {
                    "userId": str(m["attendee__id"]),
                    "userName": m["attendee__username"],
                    "userEmail": m["attendee__email"] or "",
                    "avatarImgPath": m["attendee__profile_image_file_name"] or "",
                    "teamId": team_id,
                    "teamName": team_name,
                }
            )

        chats = []
        for m in mdms:
            latest_msg = latest_msg_by_chat_id.get(m.mdm_id)
            latest_dict = None
            latest_text = ""
            ts_last = None
            if latest_msg is not None:
                latest_dict = _serialize_message(latest_msg, [], 0, set())
                latest_text = (
                    generate_first_line.get(latest_msg.message_body[0])
                    if latest_msg.message_body
                    else ""
                )
                ts_last = str(latest_msg.ts_sent_at)

            chats.append(
                {
                    "chatId": m.mdm_id,
                    "chatName": m.display_name or f"MDM-{m.mdm_id}",
                    "chatType": CHAT_TYPE,
                    "isPinned": (CHAT_TYPE, m.mdm_id) in pinned_set,
                    "mdmMembers": members_by_chat.get(m.mdm_id, []),
                    "lastReadMessageId": last_read_map.get(m.mdm_id, -1),
                    "latestMessage": latest_dict,
                    "latestMessageText": latest_text,
                    "TSLastMessage": ts_last,
                }
            )

        dm_flagged_keys = [
            (f["chat_type"], f["chat_id"], f.get("thread_id", 0), f["message_id"])
            for f in raw_flagged
            if f["chat_type"] == CHAT_TYPE and f.get("thread_id", 0) == 0
        ]
        flagged_messages = []
        if dm_flagged_keys:
            flagged_uids = [f"{k[1]}-{k[3]}" for k in dm_flagged_keys]
            flagged_msgs = list(
                MDMMessages.objects.filter(uid__in=flagged_uids).select_related(
                    "mdm", "sender", "task", "task__project"
                )
            )
            chat_by_id = {c["chatId"]: c for c in chats}
            for fm in flagged_msgs:
                chat = chat_by_id.get(fm.mdm_id)
                if chat is None:
                    continue
                flagged_messages.append(
                    {
                        "flaggedMessageId": f"{CHAT_TYPE}-{fm.mdm_id}-0-{fm.message_id}",
                        "chatType": CHAT_TYPE,
                        "chatName": chat["chatName"],
                        "chatId": fm.mdm_id,
                        "threadId": 0,
                        "messageId": fm.message_id,
                        "contentText": (
                            generate_first_line.get(fm.message_body[0]) if fm.message_body else ""
                        ),
                        "sender": {
                            "userName": fm.sender.username,
                            "userId": fm.sender.id,
                            "avatarImgPath": fm.sender.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        },
                        "project": (
                            {
                                "projectId": fm.task.project.project_id,
                                "projectName": fm.task.project.project_name,
                                "isJoined": True,
                                "systemUserId": fm.task.project.project_system_user.id,
                            }
                            if fm.task
                            else {
                                "projectId": None,
                                "projectName": None,
                                "isJoined": False,
                                "systemUserId": None,
                            }
                        ),
                        "taskId": fm.task.task_id if fm.task else None,
                        "tsSent": str(fm.ts_sent_at),
                    }
                )

        return Response({"chats": chats, "flagged_messages": flagged_messages})


class MDMMessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v2/mdm/messagesDelta/?since="""

    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")
        data = {"team_id": team_id, "user_id": user_id}
        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        server_time = capture_server_time()
        since = parse_since(request)

        mdm_ids = list(
            MDMMembers.objects.filter(Q(mdm__owner_team=team_id, attendee=user_id)).values_list(
                "mdm_id", flat=True
            )
        )
        if not mdm_ids:
            return Response(
                build_delta_response({"messages": []}, server_time),
                status=status.HTTP_200_OK,
            )

        if since is None:
            qs = MDMMessages.objects.filter(mdm_id__in=mdm_ids, is_deleted=False)
        else:
            recent_reaction_uids = set(
                f"{r['chat_id']}-{r['message_id']}"
                for r in ReactionFact.objects.filter(
                    chat_type=CHAT_TYPE,
                    chat_id__in=mdm_ids,
                    is_thread=False,
                    ts_updated_at__gt=since,
                ).values("chat_id", "message_id")
            )
            recent_thread_parent_uids = set(
                MDMThreadMessages.objects.filter(
                    mdm_id__in=mdm_ids, ts_updated_at__gt=since
                ).values_list("parent_message_uid", flat=True)
            )
            indirect_uids = list(recent_reaction_uids | recent_thread_parent_uids)
            qs = MDMMessages.objects.filter(mdm_id__in=mdm_ids).filter(
                Q(ts_updated_at__gt=since) | Q(uid__in=indirect_uids)
            )

        msgs = list(
            qs.select_related("mdm", "sender", "task", "task__project").order_by("ts_sent_at")
        )
        if not msgs:
            return Response(
                build_delta_response({"messages": []}, server_time),
                status=status.HTTP_200_OK,
            )

        chat_ids = list({m.mdm_id for m in msgs})

        reaction_rows = (
            ReactionFact.objects.filter(chat_type=CHAT_TYPE, chat_id__in=chat_ids, is_thread=False)
            .select_related("sender")
            .values(
                "chat_id",
                "message_id",
                "reaction_id",
                "reaction_emoji",
                "sender__username",
                "sender__id",
                "sender__profile_image_file_name",
                "ts_created_at",
            )
        )
        reactions_by_key = _build_reactions_by_key(reaction_rows, is_thread=False)

        reply_counts = {
            f"{row['parent_message_uid__mdm__mdm_id']}-{row['parent_message_uid__message_id']}": row[
                "num_of_replies"
            ]
            for row in MDMThreadMessages.objects.filter(
                is_deleted=False, parent_message_uid__mdm_id__in=chat_ids
            )
            .values("parent_message_uid__mdm__mdm_id", "parent_message_uid__message_id")
            .annotate(num_of_replies=Count("thread_message_id"))
        }

        chat_master_row = (
            UserChatMaster.objects.filter(user=user_id, team=team_id)
            .values_list("flagged_messages", flat=True)
            .first()
        )
        flagged_set = {
            (f["chat_type"], f["chat_id"], f.get("thread_id", 0), f["message_id"])
            for f in (chat_master_row or [])
        }

        messages = [
            _serialize_message(
                m,
                reactions=reactions_by_key.get((m.mdm_id, m.message_id), []),
                num_replies=reply_counts.get(f"{m.mdm_id}-{m.message_id}", 0),
                flagged_set=flagged_set,
            )
            for m in msgs
        ]
        return Response(
            build_delta_response({"messages": messages}, server_time),
            status=status.HTTP_200_OK,
        )


class MDMThreadMessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v2/mdm/threadMessagesDelta/?since="""

    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")
        data = {"team_id": team_id, "user_id": user_id}
        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        server_time = capture_server_time()
        since = parse_since(request)

        mdm_ids = list(
            MDMMembers.objects.filter(Q(mdm__owner_team=team_id, attendee=user_id)).values_list(
                "mdm_id", flat=True
            )
        )
        if not mdm_ids:
            return Response(
                build_delta_response({"thread_messages": []}, server_time),
                status=status.HTTP_200_OK,
            )

        if since is None:
            qs = MDMThreadMessages.objects.filter(mdm_id__in=mdm_ids, is_deleted=False)
        else:
            reaction_triples = list(
                ReactionFact.objects.filter(
                    chat_type=CHAT_TYPE,
                    chat_id__in=mdm_ids,
                    is_thread=True,
                    ts_updated_at__gt=since,
                ).values_list("chat_id", "thread_id", "message_id")
            )
            reaction_q = Q()
            for cid, tid, mid in reaction_triples:
                reaction_q |= Q(mdm_id=cid, thread_id=tid, thread_message_id=mid)
            qs = MDMThreadMessages.objects.filter(mdm_id__in=mdm_ids).filter(
                Q(ts_updated_at__gt=since) | reaction_q
            )

        tms = list(
            qs.select_related(
                "mdm", "sender", "parent_message_uid", "parent_message_uid__task"
            ).order_by("ts_sent_at")
        )
        if not tms:
            return Response(
                build_delta_response({"thread_messages": []}, server_time),
                status=status.HTTP_200_OK,
            )

        chat_ids = list({tm.mdm_id for tm in tms})

        reaction_rows = (
            ReactionFact.objects.filter(chat_type=CHAT_TYPE, chat_id__in=chat_ids, is_thread=True)
            .select_related("sender")
            .values(
                "chat_id",
                "thread_id",
                "message_id",
                "reaction_id",
                "reaction_emoji",
                "sender__username",
                "sender__id",
                "sender__profile_image_file_name",
                "ts_created_at",
            )
        )
        reactions_by_key = _build_reactions_by_key(reaction_rows, is_thread=True)

        chat_master_row = (
            UserChatMaster.objects.filter(user=user_id, team=team_id)
            .values_list("flagged_messages", flat=True)
            .first()
        )
        flagged_set = {
            (f["chat_type"], f["chat_id"], f.get("thread_id", 0), f["message_id"])
            for f in (chat_master_row or [])
        }

        thread_messages = [
            _serialize_thread_message(
                tm,
                reactions=reactions_by_key.get(
                    (tm.mdm_id, tm.thread_id, tm.thread_message_id), []
                ),
                flagged_set=flagged_set,
            )
            for tm in tms
        ]
        return Response(
            build_delta_response({"thread_messages": thread_messages}, server_time),
            status=status.HTTP_200_OK,
        )
