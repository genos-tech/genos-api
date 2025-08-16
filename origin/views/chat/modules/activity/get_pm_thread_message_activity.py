from django.db.models import Q
from datetime import datetime

from origin.models.chat.pm_models import *
from origin.models.project.prj_models import *


def get(user_id: str, team_id: str, n_days_ago: datetime):
    my_all_project_ids = ProjectMembers.objects.filter(team=team_id, attendee=user_id).values_list(
        "project_id", flat=True
    )
    _pm_thread_messages = PMThreadMessages.objects.filter(
        Q(project__team=team_id, project__in=my_all_project_ids),
        ts_sent_at__gte=n_days_ago,
    )
    pm_thread_messages = []
    for message in _pm_thread_messages:
        if message.sender.is_system_user == False:
            try:
                content = " ".join([c["text"] for c in message.thread_message_body[0]["content"]])
            except:
                print("[ERROR] pm_thread_message", message.thread_message_body)
                content = "Failed to get text..."

            pm_thread_messages.append(
                {
                    "activityId": "{activity_type}-{chat_type}-{chat_id}-{is_thread}-{message_id}".format(
                        activity_type=1,
                        chat_type=1,
                        chat_id=message.project.project_id,
                        is_thread=1,
                        message_id=message.thread_message_id,
                    ),
                    "activityType": 1,
                    "chatType": 3,  # pm
                    "chatId": int(message.project.project_id),
                    "chatName": message.project.project_name,
                    "dmPartnerUser": {"userName": "", "userId": "", "avatarImgPath": ""},
                    "isThread": True,
                    "threadId": int(message.thread_id),
                    "messageId": int(message.thread_message_id),
                    "messageUniqueKey": f"{message.project.project_id}-{message.thread_id}",
                    "threadMessageUniqueKey": f"{message.project.project_id}-{message.thread_id}-{message.thread_message_id}",
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
    return pm_thread_messages, my_all_project_ids
