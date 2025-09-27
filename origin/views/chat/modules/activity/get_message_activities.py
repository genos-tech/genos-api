from datetime import datetime

from django.db.models import F, Q

from origin.models.chat.activity_models import *

ACTIVITY_TYPE = 1


def get(payload: dict, chat_type: int, chat_ids: list, n_days_ago: datetime):
    """
    For messages (DM, GM, PM, Task Comment) except mention messages;
    activity_type: 1
    sender: not <payload["user_id"]>
    mentioned_user_ids: not <payload["user_id"]>
    """

    return list(
        ActivityFact.objects.filter(
            team=payload["team_id"],
            activity_type=ACTIVITY_TYPE,
            chat_type=chat_type,
            chat_id__in=chat_ids,
            ts_created_at__gte=n_days_ago,
        )
        .filter(~Q(sender=payload["user_id"]))  # Exclude messages that the request user sent
        .filter(
            ~Q(mentioned_user_ids__contains=[payload["user_id"]])
        )  # Exclude messages that the request user is mentioned in
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
            firstLineContent=F("first_line_content"),
            senderId=F("sender"),
            projectId=F("project"),
            projectName=F("project__project_name"),
            latestReaction=F("latest_reaction"),
            latestReactionUser=F("latest_reaction_user"),
            mentionedUserIds=F("mentioned_user_ids"),
            isRead=F("is_read"),
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
    )
