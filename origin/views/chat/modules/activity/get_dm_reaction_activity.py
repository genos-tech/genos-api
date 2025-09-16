from django.db.models import Q
from datetime import datetime

from origin.models.chat.reaction_models import *
from origin.models.chat.dm_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 1
ACTIVITY_TYPE = 2
IS_THREAD = 0


def get(all_activities: dict, user_id: str, team_id: str, my_all_dm_ids, n_days_ago: datetime):
    dm_raw_reactions = ReactionFact.objects.filter(
        Q(team=team_id, chat_type=1, chat_id__in=my_all_dm_ids, is_thread=IS_THREAD == 1),
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

    dm_reacted_messages = DMMessages.objects.filter(
        dm__team=team_id,
        ts_sent_at__gte=n_days_ago,
    ).filter(
        Q(sender=user_id)
        & Q(dm__in=list(set([row["chat_id"] for row in dm_raw_reactions])))
        & Q(message_id__in=list(set([row["message_id"] for row in dm_raw_reactions])))
    )

    for message in dm_reacted_messages:
        if message.sender.is_system_user == False:
            content = generate_first_line.get(message.message_body[0])
            reactions = dm_raw_reactions.filter(message_id=int(message.message_id)).values_list(
                "reaction_id",
                "reaction_emoji",
                "sender__username",
                "sender__id",
                "sender__profile_image_url",
                "ts_created_at",
            )
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
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "tsSent": reaction[5],
                }
                all_reactions.append(_reaction)

                if latest_reaction == {} or latest_reaction["tsSent"] < reaction[5]:
                    latest_reaction = {
                        "emoji": reaction[1],
                        "sender": {
                            "userName": reaction[2],
                            "userId": reaction[3],
                            "avatarImgPath": reaction[4],
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        },
                        "tsSent": reaction[5],
                    }

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

            activity_id = "{activity_type}-{chat_type}-{chat_id}-{message_id}".format(
                activity_type=ACTIVITY_TYPE,
                chat_type=CHAT_TYPE,
                chat_id=message.dm.dm_id,
                message_id=message.message_id,
            )
            all_activities[activity_id] = {
                "activityId": activity_id,
                "activityType": ACTIVITY_TYPE,
                "chatType": CHAT_TYPE,  # dm
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
                "latestReaction": latest_reaction,
                "sender": {
                    "userName": message.sender.username,
                    "userId": message.sender.id,
                    "avatarImgPath": message.sender.profile_image_url,
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "customStatus": "",
                },
                "reactions": all_reactions,
                "tsSent": (
                    latest_reaction["tsSent"]
                    if "tsSent" in latest_reaction
                    else message.ts_sent_at
                ),
            }

    return all_activities
