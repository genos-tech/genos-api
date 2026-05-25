from datetime import datetime

from django.db.models import F, Q, Exists, OuterRef, Case, When, Value, CharField
from django.db.models.functions import Concat

from origin.models.chat.activity_models import *
from origin.models.chat.read_status_models import *

ACTIVITY_TYPE = 2


def get(
    payload: dict,
    chat_type: int,
    chat_ids: list,
    n_days_ago: datetime,
    is_delta_load: bool = False,
    limit: int = 100,
    offset: int = 0,
):
    """
    For reactions in DM, GM, PM, Task;
    activity_type: 2
    sender: <payload["user_id"]>
    latest_reaction_user: not <payload["user_id"]>

    `is_delta_load`: when True, filter by `ts_updated_at` (catches edits +
    soft-deletes) and include `is_deleted` rows so the client can apply
    tombstones.
    """

    base = ActivityFact.objects.filter(
        team=payload["team_id"],
        activity_type=ACTIVITY_TYPE,
        chat_type=chat_type,
        chat_id__in=chat_ids,
        sender=payload["user_id"],
    )
    if is_delta_load:
        base = base.filter(ts_updated_at__gt=n_days_ago)
    else:
        base = base.filter(ts_created_at__gte=n_days_ago, is_deleted=False)

    return list(
        base.filter(
            ~Q(latest_reaction_user=payload["user_id"])
        )  # Exclude reactions that the request user did
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
            isDeleted=F("is_deleted"),
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
            "isDeleted",
        )
        .order_by("-tsSent")[offset : offset + limit]  # Most recent first  # Pagination slice
    )
