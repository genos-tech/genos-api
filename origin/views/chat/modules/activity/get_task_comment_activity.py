from django.db.models import Q
from datetime import datetime

from origin.models.task.task_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 4
ACTIVITY_TYPE = 1
IS_THREAD = 0


def get(
    all_activities: dict, user_id: str, team_id: str, my_all_project_ids, n_days_ago: datetime
):
    task_comments = TaskComments.objects.filter(~Q(sender=user_id)).filter(
        task__team=team_id,
        task__project__in=my_all_project_ids,
        ts_sent_at__gte=n_days_ago,
    )
    for comment in task_comments:
        content = generate_first_line.get(comment.comment_body[0])
        activity_id = "{activity_type}-{chat_type}-{chat_id}-{task_id}-{message_id}".format(
            activity_type=ACTIVITY_TYPE,
            chat_type=CHAT_TYPE,
            chat_id=comment.task.project.project_id,
            task_id=comment.task.task_id,
            message_id=comment.comment_id,
        )
        all_activities[activity_id] = {
            "activityId": activity_id,
            "activityType": ACTIVITY_TYPE,
            "chatType": CHAT_TYPE,  # task comment
            "chatId": int(comment.task.project.project_id),
            "chatName": comment.task.project.project_name,
            "dmPartnerUser": {
                "userName": "",
                "userId": "",
                "avatarImgPath": "",
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
            },
            "isThread": IS_THREAD == 1,
            "threadId": -1,
            "messageId": int(comment.comment_id),
            "messageUniqueKey": f"{comment.task.project.project_id}-{comment.task.task_id}",
            "threadMessageUniqueKey": "",
            "taskId": int(comment.task.task_id),
            "project": {
                "projectId": comment.task.project.project_id,
                "projectName": comment.task.project.project_name,
                "isJoined": True,
                "systemUserId": None,
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
                "userName": comment.sender.username,
                "userId": comment.sender.id,
                "avatarImgPath": comment.sender.profile_image_file_name,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
            },
            "reactions": [],
            "tsSent": comment.ts_sent_at,
        }
    return all_activities
