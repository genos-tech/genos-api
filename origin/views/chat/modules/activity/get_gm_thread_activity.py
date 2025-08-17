from django.db.models import Q
from datetime import datetime

from origin.models.chat.gm_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 2
ACTIVITY_TYPE = 1
IS_THREAD = 1


def get(user_id: str, team_id: str, n_days_ago: datetime):
    my_all_gm_ids = GMMembers.objects.filter(gm__owner_team=team_id, attendee=user_id).values_list(
        "gm_id", flat=True
    )
    _gm_thread_messages = GMThreadMessages.objects.filter(
        Q(gm__owner_team=team_id, gm__in=my_all_gm_ids), ts_sent_at__gte=n_days_ago
    )
    gm_thread_messages = []
    for message in _gm_thread_messages:
        if message.sender.is_system_user == False:
            content = generate_first_line.get(message.thread_message_body[0])
            gm_thread_messages.append(
                {
                    "activityId": "{activity_type}-{chat_type}-{chat_id}-{is_thread}-{thread_id}-{message_id}".format(
                        activity_type=ACTIVITY_TYPE,
                        chat_type=CHAT_TYPE,
                        chat_id=message.gm.gm_id,
                        is_thread=IS_THREAD,
                        thread_id=message.thread_id,
                        message_id=message.thread_message_id,
                    ),
                    "activityType": ACTIVITY_TYPE,
                    "chatType": CHAT_TYPE,  # gm
                    "chatId": int(message.gm.gm_id),
                    "chatName": message.gm.group_name,
                    "dmPartnerUser": {"userName": "", "userId": "", "avatarImgPath": ""},
                    "isThread": IS_THREAD == 1,
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
    return gm_thread_messages, my_all_gm_ids
