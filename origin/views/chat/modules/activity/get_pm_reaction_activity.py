from django.db.models import Q
from datetime import datetime

from origin.models.chat.reaction_models import *
from origin.models.chat.pm_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 3
ACTIVITY_TYPE = 2
IS_THREAD = 0


def get(
    all_activities: dict, user_id: str, team_id: str, my_all_project_ids, n_days_ago: datetime
):
    pm_raw_reactions = ReactionFact.objects.filter(
        Q(team=team_id, chat_type=3, chat_id__in=my_all_project_ids, is_thread=IS_THREAD == 1),
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

    pm_reacted_messages = PMMessages.objects.filter(
        project__team=team_id,
        ts_sent_at__gte=n_days_ago,
    ).filter(
        Q(sender=user_id)
        & Q(project__in=list(set([row["chat_id"] for row in pm_raw_reactions])))
        & Q(message_id__in=list(set([row["message_id"] for row in pm_raw_reactions])))
    )

    for message in pm_reacted_messages:
        content = generate_first_line.get(message.message_body[0])
        reactions = pm_raw_reactions.filter(message_id=int(message.message_id)).values_list(
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

        task_id = int(message.task.task_id) if message.task else -1

        activity_id = "{activity_type}-{chat_type}-{chat_id}-{message_id}".format(
            activity_type=ACTIVITY_TYPE,
            chat_type=CHAT_TYPE,
            chat_id=message.project.project_id,
            message_id=message.message_id,
        )
        all_activities[activity_id] = {
            "activityId": activity_id,
            "activityType": ACTIVITY_TYPE,  # reaction activity
            "chatType": CHAT_TYPE,  # pm
            "chatId": int(message.project.project_id),
            "chatName": message.project.project_name,
            "dmPartnerUser": {
                "userName": "",
                "userId": "",
                "avatarImgPath": "",
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
            },
            "isThread": IS_THREAD == 1,
            "threadId": -1,
            "messageId": int(message.message_id),
            "messageUniqueKey": f"{message.project.project_id}-{task_id}",
            "threadMessageUniqueKey": "",
            "taskId": task_id,
            "project": {
                "projectId": (message.task.project.project_id if message.task else None),
                "projectName": (message.task.project.project_name if message.task else None),
                "isJoined": True if message.task else False,
                "systemUserId": (
                    message.task.project.project_system_user.id if message.task else None
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
            "tsSent": (
                latest_reaction["tsSent"] if "tsSent" in latest_reaction else message.ts_sent_at
            ),
        }

    return all_activities
