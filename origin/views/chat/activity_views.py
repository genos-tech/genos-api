from django.db.models import Q

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

from origin.views.chat.modules.activity.get_message_activities import (
    get as get_message_activities,
)
from origin.views.chat.modules.activity.get_mention_activities import (
    get as get_mention_activities,
)
from origin.views.chat.modules.activity.get_reaction_activities import (
    get as get_reaction_activities,
)

from origin.views.utils.request_validators import validate_request_data, validate_request_user

"""
chatType = {1: DM, 2: GM, 3: PM, 4: Task Comment}
activityType = {1: message or comment, 2: reaction, 3: mention}
"""


class ActivityView(AuthenticatedAPIView):
    def put(self, request):
        request_data = request.data

        # For mention messages, the activity_id is
        # for non-thread messages: <activity_type>-<chat_type>-<chat_id>-<message_id>.
        # for thread messages: <activity_type>-<chat_type>-<chat_id>-<thread_id>-<message_id>.
        # But, the activity_type is always 1 in the database.
        # When we response the activities, we'll change it to 3 if the request user is mentioned in the message.
        # So, we need to change it to 1 if the activity_type is 3 to keep the activity_id consistent in the database.
        if request_data["activity_id"][0] == "3":
            request_data["activity_id"] = "1" + request_data["activity_id"][1:]

        try:
            old_activity = ActivityFact.objects.get(activity_id=request_data["activity_id"])
            serializer = ActivityFactSerializer(old_activity, data=request_data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
        except:
            # Insert if not exists
            serializer = ActivityFactSerializer(data=request_data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        data = {
            "team": request.data["team_id"],
            "activity_id": request.data["activity_id"],
        }

        if res := validate_request_data(data):
            return res

        if data["activity_id"][0] == "3":
            data["activity_id"] = "1" + data["activity_id"][1:]

        try:
            activity = ActivityFact.objects.get(team=data["team"], activity_id=data["activity_id"])
            activity.delete()
            return Response(
                {"message": f"Activity deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except ActivityFact.DoesNotExist:
            return Response(
                {"error": "Activity not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class ActivityHistoryView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "user_id": request.GET.get("user_id"),
            "period_days": (
                int(request.GET.get("period_days")) if request.GET.get("period_days") else 30
            ),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        # Filter messages of the last <period_days> days
        n_days_ago = timezone.now() - timedelta(days=max(min(data["period_days"], 30), 1))

        my_dm_ids = list(
            UserDMMapping.objects.filter(
                Q(team_id=data["team_id"], user_id=request_user_id)
            ).values_list("dm_id", flat=True)
        )
        gm_ids = list(
            GMMembers.objects.filter(
                Q(gm__owner_team=data["team_id"], attendee=request_user_id)
            ).values_list("gm", flat=True)
        )
        project_ids = list(
            ProjectMembers.objects.filter(
                Q(team=data["team_id"], attendee=request_user_id)
            ).values_list("project_id", flat=True)
        )

        all_activities = (
            # For DM thread messages except mention messages;
            #   chat_type: 1
            #   activity_type: 1
            get_message_activities(
                payload=data,
                chat_type=1,
                chat_ids=my_dm_ids,
                n_days_ago=n_days_ago,
            )
            # For DM reaction messages;
            #   chat_type: 1
            #   activity_type: 2
            + get_reaction_activities(
                payload=data, chat_type=1, chat_ids=my_dm_ids, n_days_ago=n_days_ago
            )
            # For DM mention messages;
            #   chat_type: 1
            #   activity_type: 3 (In database, it's 1, but we'll change it to 3
            #                   when the request user is mentioned in the message.)
            + get_mention_activities(
                payload=data, chat_type=1, chat_ids=my_dm_ids, n_days_ago=n_days_ago
            )
            # For GM thread messages except mention messages;
            #   chat_type: 2
            #   activity_type: 1
            + get_message_activities(
                payload=data, chat_type=2, chat_ids=gm_ids, n_days_ago=n_days_ago
            )
            # For GM reaction messages;
            #   chat_type: 2
            #   activity_type: 2
            + get_reaction_activities(
                payload=data, chat_type=2, chat_ids=gm_ids, n_days_ago=n_days_ago
            )
            # For GM mention messages;
            #   chat_type: 2
            #   activity_type: 3 (In database, it's 1, but we'll change it to 3
            #                   when the request user is mentioned in the message.)
            + get_mention_activities(
                payload=data, chat_type=2, chat_ids=gm_ids, n_days_ago=n_days_ago
            )
            # For PM thread and mention messages;
            #   chat_type: 3
            #   activity_type: 1
            + get_message_activities(
                payload=data, chat_type=3, chat_ids=project_ids, n_days_ago=n_days_ago
            )
            # For PM reaction messages;
            #   chat_type: 3
            #   activity_type: 2
            + get_reaction_activities(
                payload=data, chat_type=3, chat_ids=project_ids, n_days_ago=n_days_ago
            )
            # For PM mention messages;
            #   chat_type: 3
            #   activity_type: 3 (In database, it's 1, but we'll change it to 3
            #                   when the request user is mentioned in the message.)
            + get_mention_activities(
                payload=data, chat_type=3, chat_ids=project_ids, n_days_ago=n_days_ago
            )
            # For task comments except mention messages;
            #   chat_type: 4
            #   activity_type: 1
            + get_message_activities(
                payload=data, chat_type=4, chat_ids=project_ids, n_days_ago=n_days_ago
            )
            # For task comment reaction;
            #   chat_type: 4
            #   activity_type: 2
            + get_reaction_activities(
                payload=data, chat_type=4, chat_ids=project_ids, n_days_ago=n_days_ago
            )
            # For task comment mention messages;
            #   chat_type: 4
            #   activity_type: 3 (In database, it's 1, but we'll change it to 3
            #                   when the request user is mentioned in the message.)
            + get_mention_activities(
                payload=data, chat_type=4, chat_ids=project_ids, n_days_ago=n_days_ago
            )
        )

        return Response(all_activities, status=status.HTTP_200_OK)
