from django.db.models import Q
from datetime import datetime

from origin.models.task.task_models import *


def get(user_id: str, team_id: str, my_all_project_ids, n_days_ago: datetime):
    task_comment_raw_reactions = TaskCommentReactionFact.objects.filter(
        Q(team=team_id, task__project__in=my_all_project_ids),
        ts_created_at__gte=n_days_ago,
    ).values(
        "task__project",
        "comment_id",
        "reaction_id",
        "reaction_emoji",
        "sender__username",
        "sender__id",
        "sender__profile_image_url",
        "ts_created_at",
    )

    _reacted_task_comment = TaskComments.objects.filter(
        task__team=team_id,
        ts_sent_at__gte=n_days_ago,
    ).filter(
        Q(
            task__project__in=list(
                set([row["task__project"] for row in task_comment_raw_reactions])
            )
        )
        & Q(comment_id__in=list(set([row["comment_id"] for row in task_comment_raw_reactions])))
    )

    reacted_task_comment = []
    for comment in _reacted_task_comment:
        try:
            content = " ".join([c["text"] for c in comment.comment_body[0]["content"]])
        except:
            print("[ERROR] reacted_task_comment", comment.comment_body)
            content = "Failed to get text..."

        reactions = task_comment_raw_reactions.filter(
            comment_id=int(comment.comment_id)
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

        reacted_task_comment.append(
            {
                "activityId": "{activity_type}-{chat_type}-{chat_id}-{is_thread}-{comment_id}".format(
                    activity_type=2,
                    chat_type=4,
                    chat_id=comment.task.project.project_id,
                    is_thread=0,
                    comment_id=comment.comment_id,
                ),
                "activityType": 2,  # reaction activity
                "chatType": 4,  # task comment
                "chatId": int(comment.task.project.project_id),
                "chatName": comment.task.project.project_name,
                "dmPartnerUser": {"userName": "", "userId": "", "avatarImgPath": ""},
                "isThread": False,
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
                "latestReaction": latest_reaction,
                "sender": {
                    "userName": comment.sender.username,
                    "userId": comment.sender.id,
                    "avatarImgPath": comment.sender.profile_image_url,
                },
                "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
                "tsSent": comment.ts_sent_at,
            }
        )

    return reacted_task_comment
