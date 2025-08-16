from django.db.models import Q
from datetime import datetime

from origin.models.chat.reaction_models import *
from origin.models.chat.gm_models import *


def get(user_id: str, team_id: str, my_all_gm_ids, n_days_ago: datetime):
    gm_raw_reactions = ReactionFact.objects.filter(
        Q(team=team_id, chat_type=2, chat_id__in=my_all_gm_ids, is_thread=True),
        ts_created_at__gte=n_days_ago,
    ).values(
        "chat_id",
        "message_id",
        "reaction_id",
        "reaction_emoji",
        "sender__username",
        "sender__id",
        "sender__profile_image_url",
        "ts_created_at",
    )

    _gm_reacted_thread_messages = GMThreadMessages.objects.filter(
        gm__owner_team=team_id,
        ts_sent_at__gte=n_days_ago,
    ).filter(
        Q(gm__in=list(set([row["chat_id"] for row in gm_raw_reactions])))
        & Q(thread_message_id__in=list(set([row["message_id"] for row in gm_raw_reactions])))
    )

    gm_reacted_thread_messages = []
    for message in _gm_reacted_thread_messages:
        if message.sender.is_system_user == False:
            try:
                content = " ".join([c["text"] for c in message.thread_message_body[0]["content"]])
            except:
                print("[ERROR] gm_reacted_thread_message", message.thread_message_body)
                content = "Failed to get text..."

            reactions = gm_raw_reactions.filter(
                message_id=int(message.thread_message_id)
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
            latest_reaction = {}
            for reaction in reactions:
                _reaction = {
                    "id": int(reaction[0]),
                    "emoji": reaction[1],
                    "sender": {
                        "userName": reaction[2],
                        "userId": reaction[3],
                        "avatarImgPath": reaction[4],
                    },
                    "tsSent": reaction[5],
                }
                if str(reaction[3]) == user_id:
                    my_reactions.append(_reaction)
                all_reactions.append(_reaction)

                if latest_reaction == {} or latest_reaction["tsSent"] < reaction[5]:
                    latest_reaction = {
                        "emoji": reaction[1],
                        "senderName": reaction[2],
                        "tsSent": reaction[5],
                    }

            gm_reacted_thread_messages.append(
                {
                    "activityId": "{activity_type}-{chat_type}-{chat_id}-{is_thread}-{message_id}".format(
                        activity_type=2,
                        chat_type=2,
                        chat_id=message.gm.gm_id,
                        is_thread=1,
                        message_id=message.thread_message_id,
                    ),
                    "activityType": 2,  # reaction activity
                    "chatType": 2,  # gm
                    "chatId": int(message.gm.gm_id),
                    "chatName": message.gm.group_name,
                    "dmPartnerUser": {"userName": "", "userId": "", "avatarImgPath": ""},
                    "isThread": True,
                    "threadId": int(message.thread_id),
                    "messageId": int(message.thread_message_id),
                    "messageUniqueKey": f"{message.gm.gm_id}-{message.thread_id}",
                    "threadMessageUniqueKey": f"{message.gm.gm_id}-{message.thread_id}-{message.thread_message_id}",
                    "taskId": (
                        int(message.parent_message_uid.task.task_id)
                        if message.parent_message_uid.task
                        else -1
                    ),
                    "project": {
                        "projectId": (
                            message.parent_message_uid.task.project.project_id
                            if message.parent_message_uid.task
                            else None
                        ),
                        "projectName": (
                            message.parent_message_uid.task.project.project_name
                            if message.parent_message_uid.task
                            else None
                        ),
                        "isJoined": True if message.parent_message_uid.task else False,
                        "systemUserId": (
                            message.parent_message_uid.task.project.project_system_user.id
                            if message.parent_message_uid.task
                            else None
                        ),
                    },
                    "firstLineContent": content,
                    "latestReaction": latest_reaction,
                    "sender": {
                        "userName": message.sender.username,
                        "userId": message.sender.id,
                        "avatarImgPath": message.sender.profile_image_url,
                    },
                    "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
                    "tsSent": message.ts_sent_at,
                }
            )

    return gm_reacted_thread_messages
