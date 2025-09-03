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

        all_activities = {}

        # Filter messages of the last 30 days
        n_days_ago = timezone.now() - timedelta(days=30)

        """
        The order to update all_activities is very important because
        if multiple values with the same the activity_id exist,
        they're going to be replaced to the later update.
        High prioritized activity must be updated very last.
        """

        #######################
        # 1. Thread messages
        #######################
        # Fetch all project_ids linking to the user
        # DM thread messages
        all_activities, my_all_dm_ids = get_dm_thread_activity.get(
            all_activities, user_id, team_id, n_days_ago
        )

        # GM thread messages
        all_activities, my_all_gm_ids = get_gm_thread_activity.get(
            all_activities, user_id, team_id, n_days_ago
        )

        # PM thread messages
        all_activities, my_all_project_ids = get_pm_thread_activity.get(
            all_activities, user_id, team_id, n_days_ago
        )

        # Task comments
        all_activities = get_task_comment_activity.get(
            all_activities, team_id, my_all_project_ids, n_days_ago
        )

        #######################
        # 2. User mentioned DM, GM, PM messages, and task comment.
        #######################
        all_activities = get_dm_mention_activity.get(
            all_activities, user_id, team_id, my_all_dm_ids, n_days_ago
        )

        all_activities = get_dm_thread_mention_activity.get(
            all_activities, user_id, team_id, my_all_dm_ids, n_days_ago
        )

        all_activities = get_gm_mention_activity.get(
            all_activities, user_id, team_id, my_all_gm_ids, n_days_ago
        )

        all_activities = get_gm_thread_mention_activity.get(
            all_activities, user_id, team_id, my_all_gm_ids, n_days_ago
        )

        all_activities = get_pm_mention_activity.get(
            all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        )

        all_activities = get_pm_thread_mention_activity.get(
            all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        )

        all_activities = get_task_comment_mention_activity.get(
            all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        )

        #######################
        # 3. Reacted messages/comments
        #######################
        # DM message reactions
        all_activities = get_dm_reaction_activity.get(
            all_activities, user_id, team_id, my_all_dm_ids, n_days_ago
        )

        # DM thread message reactions
        all_activities = get_dm_thread_reaction_activity.get(
            all_activities, user_id, team_id, my_all_dm_ids, n_days_ago
        )

        # GM message reactions
        all_activities = get_gm_reaction_activity.get(
            all_activities, user_id, team_id, my_all_gm_ids, n_days_ago
        )

        # GM thread message reactions
        all_activities = get_gm_thread_reaction_activity.get(
            all_activities, user_id, team_id, my_all_gm_ids, n_days_ago
        )

        # PM message reactions
        all_activities = get_pm_reaction_activity.get(
            all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        )

        # PM thread message reactions
        all_activities = get_pm_thread_reaction_activity.get(
            all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        )

        # Task comment reactions
        all_activities = get_task_comment_reaction_activity.get(
            all_activities, user_id, team_id, my_all_project_ids, n_days_ago
        )

        all_activities = sorted(all_activities.values(), key=lambda x: x["tsSent"], reverse=True)

        return Response(all_activities, status=status.HTTP_200_OK)
