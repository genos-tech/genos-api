from django.db.models import Q
from datetime import datetime

from origin.models.task.task_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 4
ACTIVITY_TYPE = 3
IS_THREAD = 0


def get(
    all_activities: dict, user_id: str, team_id: str, my_all_project_ids, n_days_ago: datetime
):
    task_comment_raw_me_mentioned = TaskCommentMentionFact.objects.filter(
        Q(team=team_id, task__project__in=my_all_project_ids, mentioned_user=user_id),
        ts_created_at__gte=n_days_ago,
    ).values(
        "task__project",
        "comment_id",
        "ts_created_at",
    )

    me_mentioned_task_comment = TaskComments.objects.filter(
        task__team=team_id,
        ts_sent_at__gte=n_days_ago,
    ).filter(
        Q(
            task__project__in=list(
                set([row["task__project"] for row in task_comment_raw_me_mentioned])
            )
        )
        & Q(comment_id__in=list(set([row["comment_id"] for row in task_comment_raw_me_mentioned])))
    )

    for comment in me_mentioned_task_comment:
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
            "activityType": ACTIVITY_TYPE,  # reaction activity
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
            "taskId": int(comment.task.task_id) if comment.task else -1,
            "project": {
                "projectId": comment.task.project.project_id,
                "projectName": comment.task.project.project_name,
                "isJoined": True,
                "systemUserId": None,
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
            "tsSent": comment.ts_sent_at,
        }

    return all_activities
