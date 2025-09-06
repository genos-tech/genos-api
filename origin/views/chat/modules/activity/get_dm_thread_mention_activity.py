from django.db.models import Q, Value, CharField
from django.db.models.functions import Concat
from datetime import datetime

from origin.models.chat.mention_models import *
from origin.models.chat.dm_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 1
ACTIVITY_TYPE = 3
IS_THREAD = 1


def get(all_activities: dict, user_id: str, team_id: str, my_all_dm_ids, n_days_ago: datetime):
    dm_raw_me_mentions = MentionFact.objects.filter(
        Q(
            team=team_id,
            chat_type=CHAT_TYPE,
            chat_id__in=my_all_dm_ids,
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

    dm_me_mentioned_thread_messages = (
        DMThreadMessages.objects.filter(Q(dm__team=team_id) & Q(ts_sent_at__gte=n_days_ago))
        .annotate(
            uid=Concat(
                "dm",
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
                    for row in dm_raw_me_mentions
                }
            )
        )
    )

    for message in dm_me_mentioned_thread_messages:
        content = generate_first_line.get(message.thread_message_body[0])

        if str(user_id) == str(message.sender.id):
            chat_name = message.receiver.username
            dm_partner_user = {
                "userName": message.receiver.username,
                "userId": message.receiver.id,
                "avatarImgPath": message.receiver.profile_image_url,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
            }
        else:
            chat_name = message.sender.username
            dm_partner_user = {
                "userName": message.sender.username,
                "userId": message.sender.id,
                "avatarImgPath": message.sender.profile_image_url,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
            }

        activity_id = "{activity_type}-{chat_type}-{chat_id}-{thread_id}-{message_id}".format(
            activity_type=ACTIVITY_TYPE,
            chat_type=CHAT_TYPE,
            chat_id=message.dm.dm_id,
            thread_id=message.thread_id,
            message_id=message.thread_message_id,
        )
        all_activities["-".join(activity_id.split("-")[1:])] = {
            "activityId": activity_id,
            "activityType": ACTIVITY_TYPE,
            "chatType": CHAT_TYPE,
            "chatId": int(message.dm.dm_id),
            "chatName": chat_name,
            "dmPartnerUser": dm_partner_user,
            "isThread": IS_THREAD == 1,
            "threadId": int(message.thread_id),
            "messageId": int(message.thread_message_id),
            "messageUniqueKey": f"{message.dm.dm_id}-{message.thread_id}",
            "threadMessageUniqueKey": f"{message.dm.dm_id}-{message.thread_id}-{message.thread_message_id}",
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
            "latestReaction": {"emoji": "", "senderName": "", "tsSent": ""},
            "sender": {
                "userName": "",
                "userId": "",
                "avatarImgPath": "",
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
            },
            "reactions": {"myReactions": [], "allReactions": []},
            "tsSent": message.ts_sent_at,
        }

    return all_activities
