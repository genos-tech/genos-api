from django.db.models import Q, Value, CharField
from django.db.models.functions import Concat
from datetime import datetime

from origin.models.chat.reaction_models import *
from origin.models.chat.dm_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 1
ACTIVITY_TYPE = 2
IS_THREAD = 1


def get(user_id: str, team_id: str, my_all_dm_ids, n_days_ago: datetime):
    dm_raw_reactions = ReactionFact.objects.filter(
        Q(team=team_id, chat_type=1, chat_id__in=my_all_dm_ids, is_thread=IS_THREAD == 1),
        ts_created_at__gte=n_days_ago,
    ).values(
        "chat_id",
        "thread_id",
        "message_id",
        "reaction_id",
        "reaction_emoji",
        "sender__username",
        "sender__id",
        "sender__profile_image_url",
        "ts_created_at",
    )

    _dm_reacted_thread_messages = DMThreadMessages.objects.filter(
        dm__team=team_id,
        ts_sent_at__gte=n_days_ago,
    ).filter(
        Q(dm__in=list(set([row["chat_id"] for row in dm_raw_reactions])))
        & Q(thread_message_id__in=list(set([row["message_id"] for row in dm_raw_reactions])))
    )

    _dm_reacted_thread_messages = (
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
                    for row in dm_raw_reactions
                }
            )
        )
    )

    dm_reacted_thread_messages = []
    for message in _dm_reacted_thread_messages:
        if message.sender.is_system_user == False:
            content = generate_first_line.get(message.thread_message_body[0])
            reactions = dm_raw_reactions.filter(
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

            if str(user_id) == str(message.sender.id):
                chat_name = message.receiver.username
                dm_partner_user = {
                    "userName": message.receiver.username,
                    "userId": message.receiver.id,
                    "avatarImgPath": message.receiver.profile_image_url,
                }
            else:
                chat_name = message.sender.username
                dm_partner_user = {
                    "userName": message.sender.username,
                    "userId": message.sender.id,
                    "avatarImgPath": message.sender.profile_image_url,
                }

            dm_reacted_thread_messages.append(
                {
                    "activityId": "{activity_type}-{chat_type}-{chat_id}-{thread_id}-{message_id}".format(
                        activity_type=ACTIVITY_TYPE,
                        chat_type=CHAT_TYPE,
                        chat_id=message.dm.dm_id,
                        thread_id=message.thread_id,
                        message_id=message.thread_message_id,
                    ),
                    "activityType": ACTIVITY_TYPE,
                    "chatType": CHAT_TYPE,  # dm
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

    return dm_reacted_thread_messages
