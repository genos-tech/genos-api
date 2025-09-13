from django.db.models import Q
from datetime import datetime

from origin.models.chat.pm_models import *
from origin.models.project.prj_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 3
ACTIVITY_TYPE = 1
IS_THREAD = 1


def get(all_activities: dict, user_id: str, team_id: str, n_days_ago: datetime):
    my_all_project_ids = ProjectMembers.objects.filter(team=team_id, attendee=user_id).values_list(
        "project_id", flat=True
    )
    _pm_thread_messages = PMThreadMessages.objects.filter(~Q(sender=user_id)).filter(
        Q(project__team=team_id, project__in=my_all_project_ids),
        ts_sent_at__gte=n_days_ago,
    )
    for message in _pm_thread_messages:
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
            "chatType": CHAT_TYPE,  # pm
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
            "taskId": task_id,
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
    return all_activities, my_all_project_ids
