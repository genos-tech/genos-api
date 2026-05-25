from django.db.models import Q

from rest_framework.response import Response
from rest_framework import status
from django.utils import timezone
from datetime import timedelta

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.dm_models import *
from origin.models.chat.gm_models import *
from origin.models.chat.pm_models import *
from origin.models.chat.mdm_models import MDMMembers
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

from origin.views.utils.incremental import (
    build_delta_response,
    capture_server_time,
    check_since,
)
from origin.views.utils.request_validators import validate_request_data, validate_request_user

"""
chatType (the `chat_type` column on ActivityFact — namespaces the
`activity_id` PK string. NOT the user-facing chat type code which only
covers 1-4):
    1 = DM
    2 = GM
    3 = PM
    4 = Task comment OR MDM (multi-user direct message).
        - Task comments store `chat_id = project_id` (and `task = task_id`).
        - MDM messages store `chat_id = mdm_id`         (and `task = None`).
        Both share `chat_type = 4` for historical reasons; we discriminate
        downstream via `task`.
    5 = Task body mention. `chat_id = project_id`, `message_id = 0`,
        `task = task_id`. Distinct from 4 to prevent activity_id
        collisions with task-comment / MDM rows.
    6 = Personal note mention. `chat_id = note_id`, `message_id = 0`.
    7 = Task note mention.     `chat_id = note_id`, `message_id = 0`.
    8 = Chat note mention.     `chat_id = note_id`, `message_id = 0`.
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
        if request_data.get("activity_id") and request_data["activity_id"][0] == "3":
            request_data["activity_id"] = "1" + request_data["activity_id"][1:]

        try:
            old_activity = ActivityFact.objects.get(activity_id=request_data["activity_id"])
            serializer = ActivityFactSerializer(old_activity, data=request_data, partial=True)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_200_OK)
        except ActivityFact.DoesNotExist:
            serializer = ActivityFactSerializer(data=request_data)
            if serializer.is_valid():
                serializer.save()
                return Response(serializer.data, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        data = {
            "team": request.data.get("team_id"),
            "activity_id": request.data.get("activity_id"),
        }

        if res := validate_request_data(data):
            return res

        # For mention messages, the activity_id is
        # for non-thread messages: <activity_type>-<chat_type>-<chat_id>-<message_id>.
        # for thread messages: <activity_type>-<chat_type>-<chat_id>-<thread_id>-<message_id>.
        # But, the activity_type is always 1 in the database.
        # When we response the activities, we'll change it to 3 if the request user is mentioned in the message.
        # So, we need to change it to 1 if the activity_type is 3 to keep the activity_id consistent in the database.
        if data["activity_id"] and data["activity_id"][0] == "3":
            data["activity_id"] = "1" + data["activity_id"][1:]

        try:
            activity = ActivityFact.objects.get(team=data["team"], activity_id=data["activity_id"])
            # Soft-delete (not hard) so the incremental-sync path can
            # tell clients to evict the row on their next refresh. A
            # hard delete is invisible to delta sync — the row simply
            # vanishes — leaving stale entries in clients' IDB until
            # they full-reload.
            activity.is_deleted = True
            activity.save(update_fields=["is_deleted", "ts_updated_at"])
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

        # Snapshot server time BEFORE any query runs. The client persists
        # this as the next sync's `since` value; any write that commits
        # during the query window is guaranteed to be picked up next time
        # because its commit_time > server_time. See utils/incremental.py.
        server_time = capture_server_time()
        since, force_full = check_since(request)

        # Full load (since=None): cap window at last <period_days> days
        # (existing behavior, max 30). Incremental load: lower bound is
        # the previous checkpoint, no cap.
        if since is None:
            window_start = timezone.now() - timedelta(days=max(min(data["period_days"], 30), 1))
        else:
            window_start = since

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
        # MDMs share `chat_type = 4` with task-comment activities (see module
        # docstring), so we have to query them separately and union the
        # results — a single `chat_id__in=project_ids+mdm_ids` query would
        # silently match the wrong rows whenever a project_id and an mdm_id
        # happen to collide as integers.
        mdm_ids = list(
            MDMMembers.objects.filter(
                Q(mdm__owner_team=data["team_id"], attendee=request_user_id)
            ).values_list("mdm", flat=True)
        )

        all_activities = (
            # For DM thread messages except mention messages;
            #   chat_type: 1
            #   activity_type: 1
            get_message_activities(
                payload=data,
                chat_type=1,
                chat_ids=my_dm_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For DM reaction messages;
            #   chat_type: 1
            #   activity_type: 2
            + get_reaction_activities(
                payload=data,
                chat_type=1,
                chat_ids=my_dm_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For DM mention messages;
            #   chat_type: 1
            #   activity_type: 3 (In database, it's 1, but we'll change it to 3
            #                   when the request user is mentioned in the message.)
            + get_mention_activities(
                payload=data,
                chat_type=1,
                chat_ids=my_dm_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For GM thread messages except mention messages;
            #   chat_type: 2
            #   activity_type: 1
            + get_message_activities(
                payload=data,
                chat_type=2,
                chat_ids=gm_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For GM reaction messages;
            #   chat_type: 2
            #   activity_type: 2
            + get_reaction_activities(
                payload=data,
                chat_type=2,
                chat_ids=gm_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For GM mention messages;
            #   chat_type: 2
            #   activity_type: 3 (In database, it's 1, but we'll change it to 3
            #                   when the request user is mentioned in the message.)
            + get_mention_activities(
                payload=data,
                chat_type=2,
                chat_ids=gm_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For PM thread and mention messages;
            #   chat_type: 3
            #   activity_type: 1
            + get_message_activities(
                payload=data,
                chat_type=3,
                chat_ids=project_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For PM reaction messages;
            #   chat_type: 3
            #   activity_type: 2
            + get_reaction_activities(
                payload=data,
                chat_type=3,
                chat_ids=project_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For PM mention messages;
            #   chat_type: 3
            #   activity_type: 3 (In database, it's 1, but we'll change it to 3
            #                   when the request user is mentioned in the message.)
            + get_mention_activities(
                payload=data,
                chat_type=3,
                chat_ids=project_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For task comments except mention messages;
            #   chat_type: 4 (task-comment side, chat_id = project_id)
            #   activity_type: 1
            + get_message_activities(
                payload=data,
                chat_type=4,
                chat_ids=project_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For task comment reaction;
            #   chat_type: 4
            #   activity_type: 2
            + get_reaction_activities(
                payload=data,
                chat_type=4,
                chat_ids=project_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For task comment mention messages;
            #   chat_type: 4
            #   activity_type: 3 (In database, it's 1, but we'll change it to 3
            #                   when the request user is mentioned in the message.)
            + get_mention_activities(
                payload=data,
                chat_type=4,
                chat_ids=project_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For MDM messages (thread + GM-style "everyone gets it");
            #   chat_type: 4 (MDM side, chat_id = mdm_id, task IS NULL)
            #   activity_type: 1
            + get_message_activities(
                payload=data,
                chat_type=4,
                chat_ids=mdm_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For MDM reactions;
            #   chat_type: 4
            #   activity_type: 2
            + get_reaction_activities(
                payload=data,
                chat_type=4,
                chat_ids=mdm_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
            # For MDM mentions;
            #   chat_type: 4
            #   activity_type: 3 (rewritten from 1 when the user is mentioned).
            + get_mention_activities(
                payload=data,
                chat_type=4,
                chat_ids=mdm_ids,
                n_days_ago=window_start,
                is_delta_load=(since is not None),
            )
        )

        return Response(
            build_delta_response(
                {"activity": all_activities},
                server_time,
                force_full_reload=force_full,
            ),
            status=status.HTTP_200_OK,
        )
