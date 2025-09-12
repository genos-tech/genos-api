from django.db.models import Q
from datetime import datetime

from origin.models.chat.mention_models import *
from origin.models.chat.pm_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 3
ACTIVITY_TYPE = 3
IS_THREAD = 0


def get(
    all_activities: dict, user_id: str, team_id: str, my_all_project_ids, n_days_ago: datetime
):
    pm_raw_me_mentioned = MentionFact.objects.filter(
        Q(
            team=team_id,
            chat_type=CHAT_TYPE,
            chat_id__in=my_all_project_ids,
            mentioned_user=user_id,
            is_thread=IS_THREAD == 1,
        ),
        ts_created_at__gte=n_days_ago,
    ).values(
        "chat_id",
        "message_id",
        "ts_created_at",
    )

    pm_me_mentioned_messages = (
        PMMessages.objects.filter(
            project__team=team_id,
            ts_sent_at__gte=n_days_ago,
        )
        .filter(~Q(sender=user_id))
        .filter(
            Q(project__in=list(set([row["chat_id"] for row in pm_raw_me_mentioned])))
            & Q(message_id__in=list(set([row["message_id"] for row in pm_raw_me_mentioned])))
        )
    )

    for message in pm_me_mentioned_messages:
        content = generate_first_line.get(message.message_body[0])

        task_id = int(message.task.task_id) if message.task else -1

        activity_id = "{activity_type}-{chat_type}-{chat_id}-{message_id}".format(
            activity_type=ACTIVITY_TYPE,
            chat_type=CHAT_TYPE,
            chat_id=message.project.project_id,
            message_id=task_id,
        )
        all_activities["-".join(activity_id.split("-")[1:])] = {
            "activityId": activity_id,
            "activityType": ACTIVITY_TYPE,
            "chatType": CHAT_TYPE,
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
            "latestReaction": {"emoji": "", "senderName": "", "tsSent": ""},
            "sender": {
                "userName": message.sender.username,
                "userId": message.sender.id,
                "avatarImgPath": message.sender.profile_image_url,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
            },
            "reactions": {"myReactions": [], "allReactions": []},
            "tsSent": message.ts_sent_at,
        }

    return all_activities
