from datetime import datetime

from django.db.models import Q, F, OuterRef, Exists, Case, When, Value, CharField
from django.db.models.functions import Concat

from origin.models.chat.activity_models import *
from origin.models.chat.read_status_models import *

ACTIVITY_TYPE = 1


def get(
    payload: dict,
    chat_type: int,
    chat_ids: list,
    n_days_ago: datetime,
    limit: int = 100,
    offset: int = 0,
):
    """
    For messages (DM, GM, PM, Task) except mention messages;
    activity_type: 1
    sender: not <payload["user_id"]>
    mentioned_user_ids: not <payload["user_id"]>
    """

    # Use database-level processing for better performance with large datasets
    return list(
        ActivityFact.objects.filter(
            team=payload["team_id"],
            activity_type=ACTIVITY_TYPE,
            chat_type=chat_type,
            chat_id__in=chat_ids,
            ts_created_at__gte=n_days_ago,
        )
        .filter(~Q(sender=payload["user_id"]))  # Exclude messages that the request user sent
        .filter(~Q(mentioned_user_ids__contains=[payload["user_id"]]))  # Exclude mentions
        .annotate(
            activityId=F("activity_id"),
            activityType=F("activity_type"),
            chatType=F("chat_type"),
            chatId=F("chat_id"),
            chatName=F("chat_name"),
            dmPartnerUserId=F("dm_partner_user"),
            dmPartnerUserName=F("dm_partner_user__username"),
            dmPartnerUserEmail=F("dm_partner_user__email"),
            isThread=F("is_thread"),
            threadId=F("thread_id"),
            messageId=F("message_id"),
            messageUniqueKey=F("message_unique_key"),
            threadMessageUniqueKey=F("thread_message_unique_key"),
            taskId=F("task"),
            # Mirror TaskMaster.display_id at the DB level so the
            # activity feed can render "<code>-<n>" without an extra
            # lookup. NULL when either side is missing — the frontend
            # `formatTaskDisplayId` util falls back to "#<taskId>".
            displayId=Case(
                When(
                    Q(task__project__code__isnull=False)
                    & Q(task__project_task_number__isnull=False),
                    then=Concat(
                        F("task__project__code"),
                        Value("-"),
                        F("task__project_task_number"),
                        output_field=CharField(),
                    ),
                ),
                default=Value(None),
                output_field=CharField(),
            ),
            firstLineContent=F("first_line_content"),
            senderId=F("sender"),
            projectId=F("project"),
            projectName=F("project__project_name"),
            latestReaction=F("latest_reaction"),
            latestReactionUser=F("latest_reaction_user"),
            mentionedUserIds=F("mentioned_user_ids"),
            isRead=Exists(
                ActivityReadStatus.objects.filter(
                    activity=OuterRef("activity_id"), user=payload["user_id"], is_read=True
                )
            ),
            tsSent=F("ts_created_at"),
        )
        .values(
            "team",
            "activityId",
            "activityType",
            "chatType",
            "chatId",
            "chatName",
            "dmPartnerUserId",
            "dmPartnerUserName",
            "dmPartnerUserEmail",
            "isThread",
            "threadId",
            "messageId",
            "messageUniqueKey",
            "threadMessageUniqueKey",
            "taskId",
            "displayId",
            "projectId",
            "projectName",
            "firstLineContent",
            "senderId",
            "latestReaction",
            "latestReactionUser",
            "reactions",
            "mentionedUserIds",
            "isRead",
            "tsSent",
        )
        .order_by("-tsSent")[offset : offset + limit]  # Most recent first  # Pagination slice
    )
