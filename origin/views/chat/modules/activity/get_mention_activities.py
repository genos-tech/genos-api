from django.db.models import F, Q, Value, IntegerField, CharField
from django.db.models.functions import Concat

from datetime import datetime

from origin.models.chat.activity_models import *


# For mention messages, the activity_id has the following format:
# for non-thread messages: <activity_type>-<chat_type>-<chat_id>-<message_id>.
# for thread messages: <activity_type>-<chat_type>-<chat_id>-<thread_id>-<message_id>.
# But, the activity_type is always 1 in the database even if it's a mention message.
# This is because the activity can be a mention for users mentioned in the message,
# but it will be just a message activity if the user is not mentioned in the message.
# Therefore, when we response the activities, we'll change the activity_type to 3.
ACTIVITY_TYPE_IN_DB = 1
ACTIVITY_TYPE_IN_RESPONSE = 3


def get(payload: dict, chat_type: int, chat_ids: list, n_days_ago: datetime):
    """
    For mention messages (DM, GM, PM, Task Comment);
    activity_type: 1
    sender: not <payload["user_id"]>
    mentioned_user_ids: <payload["user_id"]>
    """

    return list(
        ActivityFact.objects.filter(
            team=payload["team_id"],
            activity_type=ACTIVITY_TYPE_IN_DB,
            chat_type=chat_type,
            chat_id__in=chat_ids,
            ts_created_at__gte=n_days_ago,
        )
        .filter(~Q(sender=payload["user_id"]))  # Exclude messages that the request user sent
        .filter(
            Q(mentioned_user_ids__contains=[payload["user_id"]])
        )  # Only include messages that the request user is mentioned in
        .annotate(
            activityId=Concat(
                Value(ACTIVITY_TYPE_IN_RESPONSE),
                Value("-"),
                "chat_type",
                Value("-"),
                "chat_id",
                Value("-"),
                "message_id",
                output_field=CharField(),
            ),
            activityType=Value(
                ACTIVITY_TYPE_IN_RESPONSE, output_field=IntegerField()
            ),  # activity_type: ACTIVITY_TYPE_IN_RESPONSE
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
