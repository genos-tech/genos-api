from django.db.models import Q
from datetime import datetime

from origin.models.chat.dm_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 1
ACTIVITY_TYPE = 1
IS_THREAD = 1


def get(user_id: str, team_id: str, n_days_ago: datetime):
    my_all_dm_ids = UserDMMapping.objects.filter(user_id=user_id).values_list("dm_id", flat=True)
    _dm_thread_messages = DMThreadMessages.objects.filter(
        Q(dm__team=team_id, dm__in=my_all_dm_ids), ts_sent_at__gte=n_days_ago
    )
    dm_thread_messages = []
    for message in _dm_thread_messages:
        if message.sender.is_system_user == False:
            content = generate_first_line.get(message.thread_message_body[0])

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

            dm_thread_messages.append(
                {
                    "activityId": "{activity_type}-{chat_type}-{chat_id}-{is_thread}-{thread_id}-{message_id}".format(
                        activity_type=ACTIVITY_TYPE,
                        chat_type=CHAT_TYPE,
                        chat_id=message.dm.dm_id,
                        is_thread=IS_THREAD,
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
                    "latestReaction": {
                        "emoji": "",
                        "senderName": "",
                        "tsSent": "",
                    },
                    "sender": {
                        "userName": message.sender.username,
                        "userId": message.sender.id,
                        "avatarImgPath": message.sender.profile_image_url,
                    },
                    "reactions": {"myReactions": [], "allReactions": []},
                    "tsSent": message.ts_sent_at,
                }
            )
    return dm_thread_messages, my_all_dm_ids
