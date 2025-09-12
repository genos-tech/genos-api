from django.db.models import Q
from datetime import datetime

from origin.models.chat.mention_models import *
from origin.models.chat.dm_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 1
ACTIVITY_TYPE = 3
IS_THREAD = 0


def get(all_activities: dict, user_id: str, team_id: str, my_all_dm_ids, n_days_ago: datetime):
    dm_raw_me_mentioned = MentionFact.objects.filter(
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
        "message_id",
        "ts_created_at",
    )

    _dm_me_mentioned_messages = (
        DMMessages.objects.filter(
            dm__team=team_id,
            ts_sent_at__gte=n_days_ago,
        )
        .filter(~Q(sender=user_id))  # Exclude thread messages sent by myself
        .filter(
            Q(dm__in=list({row["chat_id"] for row in dm_raw_me_mentioned}))
            & Q(message_id__in=list({row["message_id"] for row in dm_raw_me_mentioned}))
        )
    )

    for message in _dm_me_mentioned_messages:
        content = generate_first_line.get(message.message_body[0])

        if str(user_id) == str(message.sender.id):
            chat_name = message.receiver.username
            dm_partner_user = {
                "userName": message.receiver.username,
                "userId": message.receiver.id,
                "avatarImgPath": message.receiver.profile_image_url,
                "tsLastSeen": "",
            }
        else:
            chat_name = message.sender.username
            dm_partner_user = {
                "userName": message.sender.username,
                "userId": message.sender.id,
                "avatarImgPath": message.sender.profile_image_url,
                "tsJoined": "",
                "customStatus": "",
            }

        activity_id = "{activity_type}-{chat_type}-{chat_id}-{message_id}".format(
            activity_type=ACTIVITY_TYPE,
            chat_type=CHAT_TYPE,
            chat_id=message.dm.dm_id,
            message_id=message.message_id,
        )
        all_activities["-".join(activity_id.split("-")[1:])] = {
            "activityId": activity_id,
            "activityType": ACTIVITY_TYPE,
            "chatType": CHAT_TYPE,
            "chatId": int(message.dm.dm_id),
            "chatName": chat_name,
            "dmPartnerUser": dm_partner_user,
            "isThread": IS_THREAD == 1,
            "threadId": -1,
            "messageId": int(message.message_id),
            "messageUniqueKey": f"{message.dm.dm_id}-{message.message_id}",
            "threadMessageUniqueKey": "",
            "taskId": int(message.task.task_id) if message.task else -1,
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
