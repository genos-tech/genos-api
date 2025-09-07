from django.db.models import Q
from datetime import datetime

from origin.models.task.task_models import *
from origin.views.chat.modules.common import generate_first_line

CHAT_TYPE = 4
ACTIVITY_TYPE = 2
IS_THREAD = 0


def get(
    all_activities: dict, user_id: str, team_id: str, my_all_project_ids, n_days_ago: datetime
):
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

    reacted_task_comment = TaskComments.objects.filter(
        task__team=team_id,
        ts_sent_at__gte=n_days_ago,
    ).filter(
        Q(sender=user_id)
        & Q(
            task__project__in=list(
                set([row["task__project"] for row in task_comment_raw_reactions])
            )
        )
        & Q(comment_id__in=list(set([row["comment_id"] for row in task_comment_raw_reactions])))
    )

    for comment in reacted_task_comment:
        content = generate_first_line.get(comment.comment_body[0])
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
                    "tsLastSeen": "",
                    "tsJoined": "",
                    "customStatus": "",
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
            "latestReaction": latest_reaction,
            "sender": {
                "userName": comment.sender.username,
                "userId": comment.sender.id,
                "avatarImgPath": comment.sender.profile_image_url,
                "tsLastSeen": "",
                "tsJoined": "",
                "customStatus": "",
            },
            "reactions": {"myReactions": my_reactions, "allReactions": all_reactions},
            "tsSent": (
                latest_reaction["tsSent"] if "tsSent" in latest_reaction else comment.ts_sent_at
            ),
        }

    return all_activities
