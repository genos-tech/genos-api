from django.db.models import Q, Value, CharField
from django.db.models.functions import Concat
from datetime import datetime

from origin.models.chat.mention_models import *
from origin.models.chat.pm_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 3
ACTIVITY_TYPE = 3
IS_THREAD = 1


def get(all_activities: dict, user_id: str, team_id: str, my_all_pm_ids, n_days_ago: datetime):
    pm_raw_me_mentions = MentionFact.objects.filter(
        Q(
            team=team_id,
            chat_type=CHAT_TYPE,
            chat_id__in=my_all_pm_ids,
            mentioned_user=user_id,
            is_thread=IS_THREAD == 1,
        ),
        ts_created_at__gte=n_days_ago,
    ).values(
        "chat_id",
        "thread_id",
        "message_id",
        "ts_created_at",
    )

    pm_me_mentioned_thread_messages = (
        PMThreadMessages.objects.filter(~Q(sender=user_id))
        .filter(Q(project__team=team_id) & Q(ts_sent_at__gte=n_days_ago))
        .annotate(
            uid=Concat(
                "project",
                Value("/"),
                "thread_id",
                Value("/"),
                "thread_message_id",
                output_field=CharField(),
            )
        )
        .filter(
            uid__in=list(
                {
                    f"{row['chat_id']}/{row['thread_id']}/{row['message_id']}"
                    for row in pm_raw_me_mentions
                }
            )
        )
    )

    for message in pm_me_mentioned_thread_messages:
        content = generate_first_line.get(message.thread_message_body[0])

        task_id = (
            int(message.parent_message_uid.task.task_id) if message.parent_message_uid.task else -1
        )

        activity_id = "{activity_type}-{chat_type}-{chat_id}-{thread_id}-{message_id}".format(
            activity_type=ACTIVITY_TYPE,
            chat_type=CHAT_TYPE,
            chat_id=message.project.project_id,
            thread_id=message.thread_id,
            message_id=message.thread_message_id,
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
            "threadId": int(message.thread_id),
            "messageId": int(message.thread_message_id),
            "messageUniqueKey": f"{message.project.project_id}-{task_id}",
            "threadMessageUniqueKey": f"{message.project.project_id}-{task_id}-{message.thread_message_id}",
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
            "latestReaction": {
                "emoji": "",
                "sender": {
                    "userName": "",
                    "userId": "",
                    "avatarImgPath": "",
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "customStatus": "",
                },
                "tsSent": "",
            },
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
