from datetime import datetime

from origin.models.task.task_models import *


def get(team_id: str, my_all_project_ids, n_days_ago: datetime):
    _task_comments = TaskComments.objects.filter(
        task__team=team_id,
        task__project__in=my_all_project_ids,
        ts_sent_at__gte=n_days_ago,
    )
    task_comments = []
    for comment in _task_comments:
        try:
            content = " ".join([c["text"] for c in comment.comment_body[0]["content"]])
        except:
            print("[ERROR] task_comment", comment.comment_body[0])
            content = "Failed to get text..."

        task_comments.append(
            {
                "activityId": "{activity_type}-{chat_type}-{chat_id}-{is_thread}-{message_id}".format(
                    activity_type=1,
                    chat_type=4,
                    chat_id=comment.task.project.project_id,
                    is_thread=0,
                    message_id=comment.comment_id,
                ),
                "activityType": 1,
                "chatType": 4,  # task comment
                "chatId": int(comment.task.project.project_id),
                "chatName": comment.task.project.project_name,
                "dmPartnerUser": {"userName": "", "userId": "", "avatarImgPath": ""},
                "isThread": False,
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
                    "senderName": "",
                    "tsSent": "",
                },
                "sender": {
                    "userName": comment.sender.username,
                    "userId": comment.sender.id,
                    "avatarImgPath": comment.sender.profile_image_url,
                },
                "reactions": {"myReactions": [], "allReactions": []},
                "tsSent": comment.ts_sent_at,
            }
        )
    return task_comments
