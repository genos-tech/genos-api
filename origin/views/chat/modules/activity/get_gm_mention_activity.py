from django.db.models import Q
from datetime import datetime

from origin.models.chat.mention_models import *
from origin.models.chat.gm_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 2
ACTIVITY_TYPE = 3
IS_THREAD = 0


def get(all_activities: dict, user_id: str, team_id: str, my_all_gm_ids, n_days_ago: datetime):
    gm_raw_me_mentioned = MentionFact.objects.filter(
        Q(
            team=team_id,
            chat_type=CHAT_TYPE,
            chat_id__in=my_all_gm_ids,
            mentioned_user=user_id,
            is_thread=IS_THREAD == 1,
        ),
        ts_created_at__gte=n_days_ago,
    ).values(
        "chat_id",
        "message_id",
        "ts_created_at",
    )

    gm_me_mentioned_messages = GMMessages.objects.filter(
        gm__owner_team=team_id,
        ts_sent_at__gte=n_days_ago,
    ).filter(
        Q(gm__in=list(set([row["chat_id"] for row in gm_raw_me_mentioned])))
        & Q(message_id__in=list(set([row["message_id"] for row in gm_raw_me_mentioned])))
    )

    for message in gm_me_mentioned_messages:
        content = generate_first_line.get(message.message_body[0])

        activity_id = "{activity_type}-{chat_type}-{chat_id}-{message_id}".format(
            activity_type=ACTIVITY_TYPE,
            chat_type=CHAT_TYPE,
            chat_id=message.gm.gm_id,
            message_id=message.message_id,
        )
        all_activities["-".join(activity_id.split("-")[1:])] = {
            "activityId": activity_id,
            "activityType": ACTIVITY_TYPE,
            "chatType": CHAT_TYPE,
            "chatId": int(message.gm.gm_id),
            "chatName": message.gm.group_name,
            "dmPartnerUser": {
                "userName": "",
                "userId": "",
                "avatarImgPath": "",
                "tsLastSeen": "",
            },
            "isThread": IS_THREAD == 1,
            "threadId": -1,
            "messageId": int(message.message_id),
            "messageUniqueKey": f"{message.gm.gm_id}-{message.message_id}",
            "threadMessageUniqueKey": "",
            "taskId": int(message.task.task_id) if message.task else -1,
            "firstLineContent": content,
            "latestReaction": {"emoji": "", "senderName": "", "tsSent": ""},
            "sender": {
                "userName": "",
                "userId": "",
                "avatarImgPath": "",
                "tsJoined": "",
                "customStatus": "",
            },
            "reactions": {"myReactions": [], "allReactions": []},
            "tsSent": message.ts_sent_at,
        }

    return all_activities
