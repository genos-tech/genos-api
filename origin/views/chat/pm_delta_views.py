"""Phase 2 incremental-sync endpoints for PM (chat_type=3).

PM is unusual among the chat types: there's no PMMaster table — a
ProjectMaster IS the chat. The chat list is "all projects this user is
a member of", and PMMessages.project_id is the chat key.
"""

from collections import defaultdict

from django.db.models import Count, OuterRef, Q, Subquery
from rest_framework.response import Response
from rest_framework import status

from origin.models.chat.chat_master_models import UserChatMaster
from origin.models.chat.pm_models import PMMessages, PMThreadMessages
from origin.models.chat.reaction_models import ReactionFact
from origin.models.chat.read_status_models import ReadStatus
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.views.chat.modules.common import generate_first_line
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.incremental import (
    build_delta_response,
    capture_server_time,
    check_since,
)
from origin.views.utils.request_validators import (
    validate_request_data,
    validate_request_user,
)

CHAT_TYPE = 3


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


def _serialize_message(msg, reactions, num_replies, task_comment_counts, flagged_set):
    project_id = msg.project_id
    return {
        # NB: PMSingleMessageView returns "{project_id}-{task_id_or_0}" but
        # the bulk history endpoint never used that key — it relied on
        # message_id. Match the existing chat.handlers.ts shape: the IDB
        # key for PM_MESSAGES is messageIdWithChatId = "{chat_id}-{msg_id}".
        "messageIdWithChatId": f"{project_id}-{msg.message_id}",
        "chatType": CHAT_TYPE,
        "chatId": project_id,
        "systemUserId": msg.project.project_system_user.id if msg.project else None,
        "messageId": msg.message_id,
        "content": msg.message_body,
        "sender": {
            "userName": msg.sender.username,
            "userId": msg.sender.id,
            "userEmail": msg.sender.email,
            "avatarImgPath": msg.sender.profile_image_file_name,
            "tsLastSeen": "",
            "tsJoined": "",
            "customStatus": "",
            "isSystemUser": msg.sender.is_system_user,
        },
        "numReplies": num_replies,
        "taskCommentCount": (task_comment_counts.get(msg.task.task_id, 0) if msg.task else 0),
        "reactions": reactions,
        "project": {
            "projectId": msg.project.project_id if msg.project else None,
            "projectName": msg.project.project_name if msg.project else None,
            "isJoined": True,
            "systemUserId": msg.project.project_system_user.id if msg.project else None,
        },
        "taskId": msg.task.task_id if msg.task else None,
        "displayId": msg.task.display_id if msg.task else None,
        "taskExist": bool(msg.task),
        "taskStatus": msg.task.status if msg.task else None,
        "isFlagged": (CHAT_TYPE, project_id, 0, msg.message_id) in flagged_set,
        "tsSent": str(msg.ts_sent_at),
        "tsUpdated": str(msg.ts_updated_at),
        "isDeleted": msg.is_deleted,
    }


def _serialize_thread_message(tm, reactions, flagged_set):
    project_id = tm.project_id
    parent_task = tm.parent_message_uid.task if tm.parent_message_uid_id else None
    return {
        "messageIdWithChatIdAndThreadId": f"{project_id}-{tm.thread_id}-{tm.thread_message_id}",
        "chatType": CHAT_TYPE,
        "chatId": project_id,
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
        "isFlagged": (CHAT_TYPE, project_id, tm.thread_id, tm.thread_message_id) in flagged_set,
        "tsSent": str(tm.ts_sent_at),
        "tsUpdated": str(tm.ts_updated_at),
        "isDeleted": tm.is_deleted,
    }


class PMChatsListView(AuthenticatedAPIView):
    """GET /api/v2/pm/chats/ — full-fetch PM chat list (one row per project the user joined)."""

    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")

        data = {"team_id": team_id, "team_name": team_name, "user_id": user_id}
        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        project_ids = list(
            ProjectMembers.objects.filter(team=team_id, attendee=user_id).values_list(
                "project_id", flat=True
            )
        )
        if not project_ids:
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
                user=user_id, chat_type=CHAT_TYPE, chat_id__in=project_ids, is_thread=False
            )
        }

        latest_subq = PMMessages.objects.filter(project=OuterRef("pk"), is_deleted=False).order_by(
            "-ts_sent_at"
        )
        projects = list(
            ProjectMaster.objects.filter(project_id__in=project_ids)
            .select_related("project_system_user")
            .annotate(latest_msg_id=Subquery(latest_subq.values("message_id")[:1]))
        )

        latest_keys = [
            f"{p.project_id}-{p.latest_msg_id}" for p in projects if p.latest_msg_id is not None
        ]
        latest_msgs = PMMessages.objects.filter(uid__in=latest_keys).select_related(
            "project", "project__project_system_user", "sender", "task", "task__project"
        )
        latest_msg_by_chat_id = {m.project_id: m for m in latest_msgs}

        chats = []
        for p in projects:
            latest_msg = latest_msg_by_chat_id.get(p.project_id)
            latest_dict = None
            latest_text = ""
            ts_last = None
            if latest_msg is not None:
                latest_dict = _serialize_message(latest_msg, [], 0, {}, set())
                latest_text = (
                    generate_first_line.get(latest_msg.message_body[0])
                    if latest_msg.message_body
                    else ""
                )
                ts_last = str(latest_msg.ts_sent_at)

            chats.append(
                {
                    "chatId": p.project_id,
                    "chatName": p.project_name,
                    "chatType": CHAT_TYPE,
                    "systemUserId": p.project_system_user.id if p.project_system_user else None,
                    "isPinned": (CHAT_TYPE, p.project_id) in pinned_set,
                    "profileImagePath": getattr(p, "profile_image_file_name", None),
                    "project": {
                        "projectId": p.project_id,
                        "projectName": p.project_name,
                        "isJoined": True,
                        "systemUserId": (
                            p.project_system_user.id if p.project_system_user else None
                        ),
                    },
                    "lastReadMessageId": last_read_map.get(p.project_id, -1),
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
                PMMessages.objects.filter(uid__in=flagged_uids).select_related(
                    "project", "project__project_system_user", "sender", "task", "task__project"
                )
            )
            chat_by_id = {c["chatId"]: c for c in chats}
            for fm in flagged_msgs:
                chat = chat_by_id.get(fm.project_id)
                if chat is None:
                    continue
                flagged_messages.append(
                    {
                        "flaggedMessageId": f"{CHAT_TYPE}-{fm.project_id}-0-{fm.message_id}",
                        "chatType": CHAT_TYPE,
                        "chatName": chat["chatName"],
                        "chatId": fm.project_id,
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
                        "project": chat["project"],
                        "taskId": fm.task.task_id if fm.task else None,
                        "displayId": fm.task.display_id if fm.task else None,
                        "tsSent": str(fm.ts_sent_at),
                    }
                )

        return Response({"chats": chats, "flagged_messages": flagged_messages})


class PMMessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v2/pm/messagesDelta/?since="""

    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")
        data = {"team_id": team_id, "user_id": user_id}
        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        server_time = capture_server_time()
        since, force_full = check_since(request)

        project_ids = list(
            ProjectMembers.objects.filter(team=team_id, attendee=user_id).values_list(
                "project_id", flat=True
            )
        )
        if not project_ids:
            return Response(
                build_delta_response({"messages": []}, server_time, force_full_reload=force_full),
                status=status.HTTP_200_OK,
            )

        if since is None:
            qs = PMMessages.objects.filter(project__in=project_ids, is_deleted=False)
        else:
            recent_reaction_uids = set(
                f"{r['chat_id']}-{r['message_id']}"
                for r in ReactionFact.objects.filter(
                    chat_type=CHAT_TYPE,
                    chat_id__in=project_ids,
                    is_thread=False,
                    ts_updated_at__gt=since,
                ).values("chat_id", "message_id")
            )
            recent_thread_parent_uids = set(
                PMThreadMessages.objects.filter(
                    project__in=project_ids, ts_updated_at__gt=since
                ).values_list("parent_message_uid", flat=True)
            )
            indirect_uids = list(recent_reaction_uids | recent_thread_parent_uids)
            qs = PMMessages.objects.filter(project__in=project_ids).filter(
                Q(ts_updated_at__gt=since) | Q(uid__in=indirect_uids)
            )

        msgs = list(
            qs.select_related(
                "project", "project__project_system_user", "sender", "task", "task__project"
            ).order_by("ts_sent_at")
        )
        if not msgs:
            return Response(
                build_delta_response({"messages": []}, server_time, force_full_reload=force_full),
                status=status.HTTP_200_OK,
            )

        chat_ids = list({m.project_id for m in msgs})

        # Reactions
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

        # Reply counts
        reply_counts = {
            f"{row['parent_message_uid__project__project_id']}-{row['parent_message_uid__message_id']}": row[
                "num_of_replies"
            ]
            for row in PMThreadMessages.objects.filter(
                is_deleted=False, parent_message_uid__project__in=chat_ids
            )
            .values(
                "parent_message_uid__project__project_id",
                "parent_message_uid__message_id",
            )
            .annotate(num_of_replies=Count("thread_message_id"))
        }

        # Task comment counts (PM-specific — see PMHistoryView for rationale).
        from origin.models.task.task_models import TaskComments

        pm_task_ids = [m.task.task_id for m in msgs if m.task]
        task_comment_counts = (
            {
                row["task"]: row["num_of_comments"]
                for row in TaskComments.objects.filter(task_id__in=pm_task_ids, is_deleted=False)
                .values("task")
                .annotate(num_of_comments=Count("comment_id"))
            }
            if pm_task_ids
            else {}
        )

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
                reactions=reactions_by_key.get((m.project_id, m.message_id), []),
                num_replies=reply_counts.get(f"{m.project_id}-{m.message_id}", 0),
                task_comment_counts=task_comment_counts,
                flagged_set=flagged_set,
            )
            for m in msgs
        ]
        return Response(
            build_delta_response(
                {"messages": messages}, server_time, force_full_reload=force_full
            ),
            status=status.HTTP_200_OK,
        )


class PMThreadMessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v2/pm/threadMessagesDelta/?since="""

    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")
        data = {"team_id": team_id, "user_id": user_id}
        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        server_time = capture_server_time()
        since, force_full = check_since(request)

        project_ids = list(
            ProjectMembers.objects.filter(team=team_id, attendee=user_id).values_list(
                "project_id", flat=True
            )
        )
        if not project_ids:
            return Response(
                build_delta_response(
                    {"thread_messages": []}, server_time, force_full_reload=force_full
                ),
                status=status.HTTP_200_OK,
            )

        if since is None:
            qs = PMThreadMessages.objects.filter(project__in=project_ids, is_deleted=False)
        else:
            reaction_triples = list(
                ReactionFact.objects.filter(
                    chat_type=CHAT_TYPE,
                    chat_id__in=project_ids,
                    is_thread=True,
                    ts_updated_at__gt=since,
                ).values_list("chat_id", "thread_id", "message_id")
            )
            reaction_q = Q()
            for cid, tid, mid in reaction_triples:
                reaction_q |= Q(project_id=cid, thread_id=tid, thread_message_id=mid)
            qs = PMThreadMessages.objects.filter(project__in=project_ids).filter(
                Q(ts_updated_at__gt=since) | reaction_q
            )

        tms = list(
            qs.select_related(
                "project", "sender", "parent_message_uid", "parent_message_uid__task"
            ).order_by("ts_sent_at")
        )
        if not tms:
            return Response(
                build_delta_response(
                    {"thread_messages": []}, server_time, force_full_reload=force_full
                ),
                status=status.HTTP_200_OK,
            )

        chat_ids = list({tm.project_id for tm in tms})

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
                    (tm.project_id, tm.thread_id, tm.thread_message_id), []
                ),
                flagged_set=flagged_set,
            )
            for tm in tms
        ]
        return Response(
            build_delta_response(
                {"thread_messages": thread_messages},
                server_time,
                force_full_reload=force_full,
            ),
            status=status.HTTP_200_OK,
        )
