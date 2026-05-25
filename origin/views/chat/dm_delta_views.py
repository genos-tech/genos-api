"""Phase 2 incremental-sync endpoints for DM (chat_type=1).

Three views, one per "shape" of data:

    GET /api/v2/dm/chats/                 — full-fetch chat list with derived fields
    GET /api/v2/dm/messagesDelta/         — incremental DM messages (?since=)
    GET /api/v2/dm/threadMessagesDelta/   — incremental DM thread messages (?since=)

The split exists because chat metadata (chatName, dmPartnerUser, latestMessage,
TSLastMessage, lastReadMessageId, isPinned) is derived by joining
DMMessages / UserChatMaster / ReadStatus / CustomUser — no single source
can answer "which chats changed since X" without losing one of those
signals. So chats stay full-fetched (cheap — ~tens of rows per user) and
the bandwidth-heavy data (messages, thread messages) is what becomes
incremental.
"""

from collections import defaultdict

from django.db.models import Count, OuterRef, Q, Subquery
from rest_framework.response import Response
from rest_framework import status

from origin.models.chat.chat_master_models import UserChatMaster
from origin.models.chat.dm_models import (
    DMMaster,
    DMMessages,
    DMThreadMessages,
    UserDMMapping,
)
from origin.models.chat.reaction_models import ReactionFact
from origin.models.chat.read_status_models import ReadStatus
from origin.models.common.user_models import CustomUser
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

CHAT_TYPE = 1


# ---------------------------------------------------------------------------
# Shared serializers for the three DM delta endpoints below.
# ---------------------------------------------------------------------------


def _serialize_partner(partner, team_id, team_name):
    return {
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


def _serialize_user_lite(user):
    return {
        "userName": user.username,
        "userId": user.id,
        "avatarImgPath": user.profile_image_file_name,
        "tsLastSeen": "",
        "tsJoined": "",
        "customStatus": "",
    }


def _serialize_reactions_for(reactions_by_key, key):
    """`reactions_by_key` is a defaultdict(list); `key` is (chat_id, message_id)
    for non-thread messages or (chat_id, thread_id, message_id) for thread
    messages."""
    return reactions_by_key.get(key, [])


def _serialize_message(msg, reactions, num_replies, flagged_set, team_id, team_name, user_id):
    dm_id = msg.dm_id
    return {
        "messageIdWithChatId": f"{dm_id}-{msg.message_id}",
        "chatType": CHAT_TYPE,
        "chatId": dm_id,
        "messageId": msg.message_id,
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
        "isFlagged": (CHAT_TYPE, dm_id, 0, msg.message_id) in flagged_set,
        "tsSent": msg.ts_sent_at,
        "tsUpdated": msg.ts_updated_at,
        "isDeleted": msg.is_deleted,
    }


def _serialize_thread_message(tm, reactions, flagged_set):
    dm_id = tm.dm_id
    parent_task = tm.parent_message_uid.task if tm.parent_message_uid_id else None
    return {
        "messageIdWithChatIdAndThreadId": f"{dm_id}-{tm.thread_id}-{tm.thread_message_id}",
        "chatType": CHAT_TYPE,
        "chatId": dm_id,
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
        "receiver": {
            "userId": tm.receiver.id,
            "userName": tm.receiver.username,
            "userEmail": tm.receiver.email,
            "avatarImgPath": tm.receiver.profile_image_file_name,
            "tsLastSeen": "",
            "tsJoined": "",
            "customStatus": "",
            "isSystemUser": tm.receiver.is_system_user,
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
        "isFlagged": (CHAT_TYPE, dm_id, tm.thread_id, tm.thread_message_id) in flagged_set,
        "tsSent": tm.ts_sent_at,
        "tsUpdated": tm.ts_updated_at,
        "isDeleted": tm.is_deleted,
    }


def _build_reactions_by_key(reaction_rows, *, is_thread):
    """`reaction_rows` are .values() dicts from a ReactionFact query."""
    result = defaultdict(list)
    for r in reaction_rows:
        if is_thread:
            key = (r["chat_id"], r["thread_id"], r["message_id"])
        else:
            key = (r["chat_id"], r["message_id"])
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


# ---------------------------------------------------------------------------
# DMChatsListView — always full-fetch
# ---------------------------------------------------------------------------


class DMChatsListView(AuthenticatedAPIView):
    """GET /api/v2/dm/chats/

    Returns the current set of DM chats for `user_id` in `team_id`, each
    with the derived fields needed to render the sidebar (chat name,
    partner avatar, latest message preview, pinned/read indicators).
    Also returns the user's DM-scoped `flagged_messages`.

    Always a full fetch. Backed by:
    - DMMaster + Subquery to find each DM's latest message_id.
    - One bulk fetch of those latest messages with select_related joins.
    - CustomUser lookup for partner names/avatars in a single query.
    - UserChatMaster for pinned/flagged + ReadStatus for unread indicators.
    """

    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")

        data = {"team_id": team_id, "team_name": team_name, "user_id": user_id}
        if res := validate_request_data(data):
            return res
        if res := validate_request_user(str(request.user.id), str(data["user_id"])):
            return res

        dm_ids = list(
            UserDMMapping.objects.filter(user_id=user_id).values_list("dm_id", flat=True)
        )
        if not dm_ids:
            return Response({"chats": [], "flagged_messages": []})

        # Pinned + flagged (per-user JSON on UserChatMaster)
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

        # Per-chat unread cursor
        last_read_map = {
            rs.chat_id: rs.last_read_message_id
            for rs in ReadStatus.objects.filter(
                user=user_id,
                chat_type=CHAT_TYPE,
                chat_id__in=dm_ids,
                is_thread=False,
            )
        }

        # Latest message id per DM (Subquery is O(1) per DM at SQL level)
        latest_subq = DMMessages.objects.filter(dm=OuterRef("pk"), is_deleted=False).order_by(
            "-ts_sent_at"
        )
        dms = list(
            DMMaster.objects.filter(dm_id__in=dm_ids, is_deleted=False).annotate(
                latest_msg_id=Subquery(latest_subq.values("message_id")[:1])
            )
        )

        # Bulk-fetch those latest messages with joins (sender/receiver/task)
        latest_keys = [f"{d.dm_id}-{d.latest_msg_id}" for d in dms if d.latest_msg_id is not None]
        latest_msgs = DMMessages.objects.filter(uid__in=latest_keys).select_related(
            "dm", "sender", "receiver", "task", "task__project"
        )
        latest_msg_by_dm_id = {m.dm_id: m for m in latest_msgs}

        # Partner CustomUser bulk fetch
        partner_ids = set()
        for d in dms:
            partner_ids.add(d.user_2_id if str(d.user_1_id) == str(user_id) else d.user_1_id)
        partner_by_id = {str(u.id): u for u in CustomUser.objects.filter(id__in=partner_ids)}

        chats = []
        for d in dms:
            partner_id_str = str(d.user_2_id if str(d.user_1_id) == str(user_id) else d.user_1_id)
            partner = partner_by_id.get(partner_id_str)
            if partner is None:
                # Defensive: skip DMs whose partner row vanished.
                continue
            latest_msg = latest_msg_by_dm_id.get(d.dm_id)
            latest_dict = None
            latest_text = ""
            ts_last = None
            if latest_msg is not None:
                latest_dict = _serialize_message(
                    latest_msg,
                    reactions=[],
                    num_replies=0,
                    flagged_set=set(),
                    team_id=team_id,
                    team_name=team_name,
                    user_id=user_id,
                )
                latest_text = (
                    generate_first_line.get(latest_msg.message_body[0])
                    if latest_msg.message_body
                    else ""
                )
                ts_last = latest_msg.ts_sent_at

            chats.append(
                {
                    "chatId": d.dm_id,
                    "chatName": partner.username,
                    "chatType": CHAT_TYPE,
                    "dmPartnerUser": _serialize_partner(partner, team_id, team_name),
                    "isPinned": (CHAT_TYPE, d.dm_id) in pinned_set,
                    "lastReadMessageId": last_read_map.get(d.dm_id, -1),
                    "latestMessage": latest_dict,
                    "latestMessageText": latest_text,
                    "TSLastMessage": ts_last,
                }
            )

        # Denormalize flagged_messages to the existing shape. raw_flagged is
        # a list of {chat_type, chat_id, thread_id, message_id} dicts; we
        # need to surface chatName / dmPartnerUser / sender / project for
        # each, scoped to DM only.
        dm_flagged_keys = [
            (f["chat_type"], f["chat_id"], f["thread_id"], f["message_id"])
            for f in raw_flagged
            if f["chat_type"] == CHAT_TYPE and f.get("thread_id", 0) == 0
        ]
        flagged_messages = []
        if dm_flagged_keys:
            flagged_uids = [f"{k[1]}-{k[3]}" for k in dm_flagged_keys]
            flagged_msgs = list(
                DMMessages.objects.filter(uid__in=flagged_uids).select_related(
                    "dm", "sender", "receiver", "task", "task__project"
                )
            )
            chat_by_id = {c["chatId"]: c for c in chats}
            for fm in flagged_msgs:
                chat = chat_by_id.get(fm.dm_id)
                if chat is None:
                    continue
                flagged_messages.append(
                    {
                        "flaggedMessageId": f"{CHAT_TYPE}-{fm.dm_id}-0-{fm.message_id}",
                        "chatType": CHAT_TYPE,
                        "chatName": chat["chatName"],
                        "chatId": fm.dm_id,
                        "threadId": 0,
                        "messageId": fm.message_id,
                        "contentText": (
                            generate_first_line.get(fm.message_body[0]) if fm.message_body else ""
                        ),
                        "sender": _serialize_user_lite(fm.sender),
                        "dmPartnerUser": chat["dmPartnerUser"],
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
                        "tsSent": fm.ts_sent_at,
                    }
                )

        return Response({"chats": chats, "flagged_messages": flagged_messages})


# ---------------------------------------------------------------------------
# DMMessagesDeltaView — incremental
# ---------------------------------------------------------------------------


class DMMessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v2/dm/messagesDelta/?since=ISO_TIMESTAMP

    Returns DM messages changed since `since`. Without `since`, returns
    the full message set for all of the user's DMs (full load).

    A message is "changed" if any of these is true:
      - its own ts_updated_at > since (body edited, soft-deleted)
      - it has a reaction whose ts_updated_at > since (added/removed)
      - it has a thread message whose ts_updated_at > since (numReplies changed)
    The OR keeps reactions/numReplies fresh on the existing checkpoint
    pattern without requiring a separate reactions endpoint.
    """

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

        dm_ids = list(
            UserDMMapping.objects.filter(user_id=user_id).values_list("dm_id", flat=True)
        )
        if not dm_ids:
            return Response(
                build_delta_response({"messages": []}, server_time, force_full_reload=force_full),
                status=status.HTTP_200_OK,
            )

        # Direct message changes
        if since is None:
            qs = DMMessages.objects.filter(dm_id__in=dm_ids, is_deleted=False)
        else:
            # Indirect changes: messages whose reactions or thread-message
            # children changed since. Compute their uids first so we can
            # union them with the direct-change query.
            recent_reaction_uids = set(
                f"{r['chat_id']}-{r['message_id']}"
                for r in ReactionFact.objects.filter(
                    chat_type=CHAT_TYPE,
                    chat_id__in=dm_ids,
                    is_thread=False,
                    ts_updated_at__gt=since,
                ).values("chat_id", "message_id")
            )
            recent_thread_parent_uids = set(
                DMThreadMessages.objects.filter(
                    dm_id__in=dm_ids, ts_updated_at__gt=since
                ).values_list("parent_message_uid", flat=True)
            )
            indirect_uids = list(recent_reaction_uids | recent_thread_parent_uids)
            qs = DMMessages.objects.filter(dm_id__in=dm_ids).filter(
                Q(ts_updated_at__gt=since) | Q(uid__in=indirect_uids)
            )

        msgs = list(
            qs.select_related("dm", "sender", "receiver", "task", "task__project").order_by(
                "ts_sent_at"
            )
        )
        if not msgs:
            return Response(
                build_delta_response({"messages": []}, server_time, force_full_reload=force_full),
                status=status.HTTP_200_OK,
            )

        msg_keys = [(m.dm_id, m.message_id) for m in msgs]
        chat_ids = list({m.dm_id for m in msgs})

        # Reactions for these messages
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

        # numReplies for these messages
        reply_counts = {
            f"{row['parent_message_uid__dm__dm_id']}-{row['parent_message_uid__message_id']}": row[
                "num_of_replies"
            ]
            for row in DMThreadMessages.objects.filter(
                is_deleted=False,
                parent_message_uid__dm_id__in=chat_ids,
            )
            .values("parent_message_uid__dm__dm_id", "parent_message_uid__message_id")
            .annotate(num_of_replies=Count("thread_message_id"))
        }

        # Flagged set for these messages (from UserChatMaster JSON)
        chat_master_row = (
            UserChatMaster.objects.filter(user=user_id, team=team_id)
            .values_list("flagged_messages", flat=True)
            .first()
        )
        flagged_set = {
            (f["chat_type"], f["chat_id"], f.get("thread_id", 0), f["message_id"])
            for f in (chat_master_row or [])
        }

        messages = []
        for m in msgs:
            messages.append(
                _serialize_message(
                    m,
                    reactions=reactions_by_key.get((m.dm_id, m.message_id), []),
                    num_replies=reply_counts.get(f"{m.dm_id}-{m.message_id}", 0),
                    flagged_set=flagged_set,
                    team_id=team_id,
                    team_name="",
                    user_id=user_id,
                )
            )

        return Response(
            build_delta_response(
                {"messages": messages}, server_time, force_full_reload=force_full
            ),
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# DMThreadMessagesDeltaView — incremental
# ---------------------------------------------------------------------------


class DMThreadMessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v2/dm/threadMessagesDelta/?since=ISO_TIMESTAMP

    Returns DM thread messages changed since `since`. Without `since`,
    returns ALL non-deleted thread messages across the user's DMs (full
    load — meant for first-ever hydration).
    """

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

        dm_ids = list(
            UserDMMapping.objects.filter(user_id=user_id).values_list("dm_id", flat=True)
        )
        if not dm_ids:
            return Response(
                build_delta_response(
                    {"thread_messages": []}, server_time, force_full_reload=force_full
                ),
                status=status.HTTP_200_OK,
            )

        if since is None:
            qs = DMThreadMessages.objects.filter(dm_id__in=dm_ids, is_deleted=False)
        else:
            recent_reaction_uids = set(
                f"{r['chat_id']}-{r['thread_id']}-{r['message_id']}"
                for r in ReactionFact.objects.filter(
                    chat_type=CHAT_TYPE,
                    chat_id__in=dm_ids,
                    is_thread=True,
                    ts_updated_at__gt=since,
                ).values("chat_id", "thread_id", "message_id")
            )
            # DMThreadMessages doesn't have a composite uid we can match on,
            # so apply the reaction filter by triple-equality via a Q OR.
            reaction_q = Q()
            for uid in recent_reaction_uids:
                cid, tid, mid = uid.split("-")
                reaction_q |= Q(dm_id=int(cid), thread_id=int(tid), thread_message_id=int(mid))
            qs = DMThreadMessages.objects.filter(dm_id__in=dm_ids).filter(
                Q(ts_updated_at__gt=since) | reaction_q
            )

        tms = list(
            qs.select_related(
                "dm", "sender", "receiver", "parent_message_uid", "parent_message_uid__task"
            ).order_by("ts_sent_at")
        )
        if not tms:
            return Response(
                build_delta_response(
                    {"thread_messages": []}, server_time, force_full_reload=force_full
                ),
                status=status.HTTP_200_OK,
            )

        chat_ids = list({tm.dm_id for tm in tms})

        # Reactions on thread messages
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

        thread_messages = []
        for tm in tms:
            thread_messages.append(
                _serialize_thread_message(
                    tm,
                    reactions=reactions_by_key.get(
                        (tm.dm_id, tm.thread_id, tm.thread_message_id), []
                    ),
                    flagged_set=flagged_set,
                )
            )

        return Response(
            build_delta_response(
                {"thread_messages": thread_messages},
                server_time,
                force_full_reload=force_full,
            ),
            status=status.HTTP_200_OK,
        )
