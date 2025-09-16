from django.db.models import F, Q
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from datetime import timedelta

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.dm_models import *
from origin.models.chat.gm_models import *
from origin.models.chat.pm_models import *
from origin.models.project.prj_models import *
from origin.serializers.chat.activity_serializers import *

from .modules.activity import (
    get_dm_reaction_activity,
    get_dm_thread_activity,
    get_dm_thread_reaction_activity,
    get_gm_reaction_activity,
    get_gm_thread_activity,
    get_gm_thread_reaction_activity,
    get_gm_thread_mention_activity,
    get_pm_reaction_activity,
    get_pm_thread_activity,
    get_pm_thread_reaction_activity,
    get_pm_thread_mention_activity,
    get_task_comment_activity,
    get_task_comment_reaction_activity,
    get_dm_mention_activity,
    get_dm_thread_mention_activity,
    get_gm_mention_activity,
    get_pm_mention_activity,
    get_task_comment_mention_activity,
)


#############################
# Activity views
# 1. Thread messages and Task comments
# 2. User mentioned DM, GM, PM messages and task comments
# 3. Reacted messages

# chatType = {1: DM, 2: GM, 3: PM, 4: Task Comment}
# activityType = {1: message or comment, 2: reaction, 3: mention}
#############################


class ActivityView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id
        team_id = request.GET.get("team_id")

        if team_id is None:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Filter messages of the last 30 days
        n_days_ago = timezone.now() - timedelta(days=30)

        my_dm_ids = list(
            UserDMMapping.objects.filter(Q(team_id=team_id, user_id=request_user_id)).values_list(
                "dm_id", flat=True
            )
        )
        gm_ids = list(
            GMMembers.objects.filter(
                Q(gm__owner_team=team_id, attendee=request_user_id)
            ).values_list("gm", flat=True)
        )
        project_ids = list(
            ProjectMembers.objects.filter(Q(team=team_id, attendee=request_user_id)).values_list(
                "project_id", flat=True
            )
        )

        all_activities = (
            # For DM thread and mention messages;
            #   activity_type: 1,3
            #   sender: not <request_user_id>
            list(
                ActivityFact.objects.filter(~Q(sender=request_user_id))
                .filter(~Q(activity_type=2))
                .filter(
                    team=team_id, chat_type=1, chat_id__in=my_dm_ids, ts_created_at__gte=n_days_ago
                )
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
            # For DM reaction messages;
            #   activity_type: 2
            #   sender: <request_user_id>
            #   latest_reaction_user: not <request_user_id>
            + list(
                ActivityFact.objects.filter(Q(sender=request_user_id))
                .filter(Q(activity_type=2))
                .filter(~Q(latest_reaction_user=request_user_id))
                .filter(
                    team=team_id, chat_type=1, chat_id__in=my_dm_ids, ts_created_at__gte=n_days_ago
                )
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
            # For GM thread and mention messages;
            #   activity_type: 1,3
            #   sender: not <request_user_id>
            + list(
                ActivityFact.objects.filter(~Q(sender=request_user_id))
                .filter(~Q(activity_type=2))
                .filter(
                    team=team_id, chat_type=2, chat_id__in=gm_ids, ts_created_at__gte=n_days_ago
                )
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
            # For GM reaction messages;
            #   activity_type: 2
            #   sender: <request_user_id>
            #   latest_reaction_user: not <request_user_id>
            + list(
                ActivityFact.objects.filter(Q(sender=request_user_id))
                .filter(Q(activity_type=2))
                .filter(~Q(latest_reaction_user=request_user_id))
                .filter(
                    team=team_id, chat_type=2, chat_id__in=gm_ids, ts_created_at__gte=n_days_ago
                )
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
            # For PM thread and mention messages;
            #   activity_type: 1,3
            #   sender: not <request_user_id>
            + list(
                ActivityFact.objects.filter(~Q(sender=request_user_id))
                .filter(~Q(activity_type=2))
                .filter(
                    team=team_id,
                    chat_type=3,
                    chat_id__in=project_ids,
                    ts_created_at__gte=n_days_ago,
                )
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
            # For PM reaction messages;
            #   activity_type: 2
            #   sender: <request_user_id>
            #   latest_reaction_user: not <request_user_id>
            + list(
                ActivityFact.objects.filter(Q(sender=request_user_id))
                .filter(Q(activity_type=2))
                .filter(~Q(latest_reaction_user=request_user_id))
                .filter(
                    team=team_id,
                    chat_type=3,
                    chat_id__in=project_ids,
                    ts_created_at__gte=n_days_ago,
                )
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
            # For task comment and mention;
            #   activity_type: 1,3
            #   sender: not <request_user_id>
            + list(
                ActivityFact.objects.filter(~Q(sender=request_user_id))
                .filter(~Q(activity_type=2))
                .filter(
                    team=team_id,
                    chat_type=4,
                    chat_id__in=project_ids,
                    ts_created_at__gte=n_days_ago,
                )
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
            # For task comment reaction;
            #   activity_type: 2
            #   sender: <request_user_id>
            #   latest_reaction_user: not <request_user_id>
            + list(
                ActivityFact.objects.filter(Q(sender=request_user_id))
                .filter(Q(activity_type=2))
                .filter(~Q(latest_reaction_user=request_user_id))
                .filter(
                    team=team_id,
                    chat_type=4,
                    chat_id__in=project_ids,
                    ts_created_at__gte=n_days_ago,
                )
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
        )

        all_activities = sorted(all_activities, key=lambda x: x["tsSent"], reverse=True)

        return Response(all_activities, status=status.HTTP_200_OK)

    def put(self, request):
        try:
            # Update if already exists
            activity_id = request.data["activity_id"]
            old_activity = ActivityFact.objects.get(activity_id=activity_id)
            serializer = ActivityFactSerializer(old_activity, data=request.data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
        except:
            # Insert if not exists
            serializer = ActivityFactSerializer(data=request.data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)

        print("FILED: request.data:")
        print(request.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ActivityHistoryView(AuthenticatedAPIView):
    def get(self, request):
        # team_id = request.GET.get("team_id")
        # team_name = request.GET.get("team_name")
        # user_id = request.GET.get("user_id")

        # if not team_id or not team_name or not user_id:
        #     return Response(
        #         {"error": "team_id, team_name and user_id are required."},
        #         status=status.HTTP_400_BAD_REQUEST,
        #     )

        # all_activities = {}

        # # Filter messages of the last 30 days
        # n_days_ago = timezone.now() - timedelta(days=30)

        # """
        # The order to update `all_activities` is very important because
        # multiple values with the same the activity_id can exist and
        # they can be replaced/overridden to the later update.
        # High prioritized activity (in this case "mention") must be updated last.

        # Steps will be;
        # Step-1: Append thread message activities.
        # Step-2: Upsert mention message activities.
        # Step-3: Append reaction message activities.
        # """

        # #######################
        # # 1. Thread messages
        # #######################
        # # Fetch all project_ids linking to the user
        # # DM thread messages
        # all_activities, my_all_dm_ids = get_dm_thread_activity.get(
        #     all_activities, user_id, team_id, n_days_ago
        # )

        # # GM thread messages
        # all_activities, my_all_gm_ids = get_gm_thread_activity.get(
        #     all_activities, user_id, team_id, n_days_ago
        # )

        # # PM thread messages
        # all_activities, my_all_project_ids = get_pm_thread_activity.get(
        #     all_activities, user_id, team_id, n_days_ago
        # )

        # # Task comments
        # all_activities = get_task_comment_activity.get(
        #     all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        # )

        # #######################
        # # 2. User mentioned DM, GM, PM messages, and task comment.
        # #######################
        # all_activities = get_dm_mention_activity.get(
        #     all_activities, user_id, team_id, my_all_dm_ids, n_days_ago
        # )

        # all_activities = get_dm_thread_mention_activity.get(
        #     all_activities, user_id, team_id, my_all_dm_ids, n_days_ago
        # )

        # all_activities = get_gm_mention_activity.get(
        #     all_activities, user_id, team_id, my_all_gm_ids, n_days_ago
        # )

        # all_activities = get_gm_thread_mention_activity.get(
        #     all_activities, user_id, team_id, my_all_gm_ids, n_days_ago
        # )

        # all_activities = get_pm_mention_activity.get(
        #     all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        # )

        # all_activities = get_pm_thread_mention_activity.get(
        #     all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        # )

        # all_activities = get_task_comment_mention_activity.get(
        #     all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        # )

        # #######################
        # # 3. Reacted messages/comments
        # #######################
        # # DM message reactions
        # all_activities = get_dm_reaction_activity.get(
        #     all_activities, user_id, team_id, my_all_dm_ids, n_days_ago
        # )

        # # DM thread message reactions
        # all_activities = get_dm_thread_reaction_activity.get(
        #     all_activities, user_id, team_id, my_all_dm_ids, n_days_ago
        # )

        # # GM message reactions
        # all_activities = get_gm_reaction_activity.get(
        #     all_activities, user_id, team_id, my_all_gm_ids, n_days_ago
        # )

        # # GM thread message reactions
        # all_activities = get_gm_thread_reaction_activity.get(
        #     all_activities, user_id, team_id, my_all_gm_ids, n_days_ago
        # )

        # # PM message reactions
        # all_activities = get_pm_reaction_activity.get(
        #     all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        # )

        # # PM thread message reactions
        # all_activities = get_pm_thread_reaction_activity.get(
        #     all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        # )

        # # Task comment reactions
        # all_activities = get_task_comment_reaction_activity.get(
        #     all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        # )

        # all_activities = sorted(all_activities.values(), key=lambda x: x["tsSent"], reverse=True)

        request_user_id = request.user.id
        team_id = request.GET.get("team_id")

        if team_id is None:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Filter messages of the last 30 days
        n_days_ago = timezone.now() - timedelta(days=30)

        my_dm_ids = list(
            UserDMMapping.objects.filter(Q(team_id=team_id, user_id=request_user_id)).values_list(
                "dm_id", flat=True
            )
        )
        gm_ids = list(
            GMMembers.objects.filter(
                Q(gm__owner_team=team_id, attendee=request_user_id)
            ).values_list("gm", flat=True)
        )
        project_ids = list(
            ProjectMembers.objects.filter(Q(team=team_id, attendee=request_user_id)).values_list(
                "project_id", flat=True
            )
        )

        all_activities = (
            # For DM thread and mention messages;
            #   activity_type: 1,3
            #   sender: not <request_user_id>
            list(
                ActivityFact.objects.filter(~Q(sender=request_user_id))
                .filter(~Q(activity_type=2))
                .filter(
                    team=team_id, chat_type=1, chat_id__in=my_dm_ids, ts_created_at__gte=n_days_ago
                )
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
            # For DM reaction messages;
            #   activity_type: 2
            #   sender: <request_user_id>
            #   latest_reaction_user: not <request_user_id>
            + list(
                ActivityFact.objects.filter(Q(sender=request_user_id))
                .filter(Q(activity_type=2))
                .filter(~Q(latest_reaction_user=request_user_id))
                .filter(
                    team=team_id, chat_type=1, chat_id__in=my_dm_ids, ts_created_at__gte=n_days_ago
                )
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
            # For GM thread and mention messages;
            #   activity_type: 1,3
            #   sender: not <request_user_id>
            + list(
                ActivityFact.objects.filter(~Q(sender=request_user_id))
                .filter(~Q(activity_type=2))
                .filter(
                    team=team_id, chat_type=2, chat_id__in=gm_ids, ts_created_at__gte=n_days_ago
                )
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
            # For GM reaction messages;
            #   activity_type: 2
            #   sender: <request_user_id>
            #   latest_reaction_user: not <request_user_id>
            + list(
                ActivityFact.objects.filter(Q(sender=request_user_id))
                .filter(Q(activity_type=2))
                .filter(~Q(latest_reaction_user=request_user_id))
                .filter(
                    team=team_id, chat_type=2, chat_id__in=gm_ids, ts_created_at__gte=n_days_ago
                )
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
            # For PM thread and mention messages;
            #   activity_type: 1,3
            #   sender: not <request_user_id>
            + list(
                ActivityFact.objects.filter(~Q(sender=request_user_id))
                .filter(~Q(activity_type=2))
                .filter(
                    team=team_id,
                    chat_type=3,
                    chat_id__in=project_ids,
                    ts_created_at__gte=n_days_ago,
                )
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
            # For PM reaction messages;
            #   activity_type: 2
            #   sender: <request_user_id>
            #   latest_reaction_user: not <request_user_id>
            + list(
                ActivityFact.objects.filter(Q(sender=request_user_id))
                .filter(Q(activity_type=2))
                .filter(~Q(latest_reaction_user=request_user_id))
                .filter(
                    team=team_id,
                    chat_type=3,
                    chat_id__in=project_ids,
                    ts_created_at__gte=n_days_ago,
                )
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
            # For task comment and mention;
            #   activity_type: 1,3
            #   sender: not <request_user_id>
            + list(
                ActivityFact.objects.filter(~Q(sender=request_user_id))
                .filter(~Q(activity_type=2))
                .filter(
                    team=team_id,
                    chat_type=4,
                    chat_id__in=project_ids,
                    ts_created_at__gte=n_days_ago,
                )
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
            # For task comment reaction;
            #   activity_type: 2
            #   sender: <request_user_id>
            #   latest_reaction_user: not <request_user_id>
            + list(
                ActivityFact.objects.filter(Q(sender=request_user_id))
                .filter(Q(activity_type=2))
                .filter(~Q(latest_reaction_user=request_user_id))
                .filter(
                    team=team_id,
                    chat_type=4,
                    chat_id__in=project_ids,
                    ts_created_at__gte=n_days_ago,
                )
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
        )

        all_activities = sorted(all_activities, key=lambda x: x["tsSent"], reverse=True)

        return Response(all_activities, status=status.HTTP_200_OK)
