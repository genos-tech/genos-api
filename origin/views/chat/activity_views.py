from pprint import pprint
from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from datetime import timedelta

from origin.views.common.base_auth_api_view import AuthenticatedAPIView

from .modules.activity import (
    get_dm_reaction_activity,
    get_dm_thread_activity,
    get_dm_thread_reaction_activity,
    get_gm_reaction_activity,
    get_gm_thread_activity,
    get_gm_thread_reaction_activity,
    get_pm_reaction_activity,
    get_pm_thread_activity,
    get_pm_thread_reaction_activity,
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
# activityType: {1: message or comment, 2: reaction}
#############################
class ActivityHistoryView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        team_name = request.GET.get("team_name")
        user_id = request.GET.get("user_id")

        if not team_id or not team_name or not user_id:
            return Response(
                {"error": "team_id, team_name and user_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        all_activities = []

        # Filter messages of the last 30 days
        n_days_ago = timezone.now() - timedelta(days=30)

        #######################
        # 1. Thread messages
        #######################
        # Fetch all project_ids linking to the user
        # DM thread messages
        dm_thread_messages, my_all_dm_ids = get_dm_thread_activity.get(
            user_id, team_id, n_days_ago
        )
        all_activities.extend(dm_thread_messages)

        # GM thread messages
        gm_thread_messages, my_all_gm_ids = get_gm_thread_activity.get(
            user_id, team_id, n_days_ago
        )
        all_activities.extend(gm_thread_messages)

        # PM thread messages
        pm_thread_messages, my_all_project_ids = get_pm_thread_activity.get(
            user_id, team_id, n_days_ago
        )
        all_activities.extend(pm_thread_messages)

        # Task comments
        task_comments = get_task_comment_activity.get(team_id, my_all_project_ids, n_days_ago)
        all_activities.extend(task_comments)

        #######################
        # 2. User mentioned DM, GM, PM messages, and task comment.
        #######################
        dm_me_mentioned_messages = get_dm_mention_activity.get(
            user_id, team_id, my_all_dm_ids, n_days_ago
        )
        all_activities.extend(dm_me_mentioned_messages)

        # NOTE: Maybe no need to add me-mentioned thread messages because
        #       the thread messages are added in the step-1 above.
        # dm_me_mentioned_thread_messages = get_dm_thread_mention_activity.get(
        #     user_id, team_id, my_all_dm_ids, n_days_ago
        # )
        # all_activities.extend(dm_me_mentioned_thread_messages)

        gm_me_mentioned_messages = get_gm_mention_activity.get(
            user_id, team_id, my_all_gm_ids, n_days_ago
        )
        all_activities.extend(gm_me_mentioned_messages)

        pm_me_mentioned_messages = get_pm_mention_activity.get(
            user_id, team_id, my_all_project_ids, n_days_ago
        )
        all_activities.extend(pm_me_mentioned_messages)

        task_me_mentioned_comments = get_task_comment_mention_activity.get(
            user_id, team_id, my_all_project_ids, n_days_ago
        )
        all_activities.extend(task_me_mentioned_comments)

        #######################
        # 3. Reacted messages/comments
        #######################
        # DM message reactions
        dm_reacted_messages = get_dm_reaction_activity.get(
            user_id, team_id, my_all_dm_ids, n_days_ago
        )
        all_activities.extend(dm_reacted_messages)

        # DM thread message reactions
        dm_reacted_thread_messages = get_dm_thread_reaction_activity.get(
            user_id, team_id, my_all_dm_ids, n_days_ago
        )
        all_activities.extend(dm_reacted_thread_messages)

        # GM message reactions
        gm_reacted_messages = get_gm_reaction_activity.get(
            user_id, team_id, my_all_gm_ids, n_days_ago
        )
        all_activities.extend(gm_reacted_messages)

        # GM thread message reactions
        gm_reacted_thread_messages = get_gm_thread_reaction_activity.get(
            user_id, team_id, my_all_gm_ids, n_days_ago
        )
        all_activities.extend(gm_reacted_thread_messages)

        # PM message reactions
        pm_reacted_messages = get_pm_reaction_activity.get(
            user_id, team_id, my_all_project_ids, n_days_ago
        )
        all_activities.extend(pm_reacted_messages)

        # PM thread message reactions
        pm_reacted_thread_messages = get_pm_thread_reaction_activity.get(
            user_id, team_id, my_all_project_ids, n_days_ago
        )
        all_activities.extend(pm_reacted_thread_messages)

        # Task comment reactions
        reacted_task_comment = get_task_comment_reaction_activity.get(
            user_id, team_id, my_all_project_ids, n_days_ago
        )
        all_activities.extend(reacted_task_comment)

        return Response(all_activities, status=status.HTTP_200_OK)
