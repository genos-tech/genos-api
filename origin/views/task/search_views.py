from collections import defaultdict

from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.project.prj_models import *
from origin.models.task.task_models import *
from .common_color import STATUS_COLOR_MAP

STATUS_MAP = {
    "open": "Open",
    "wip": "WIP",
    "pending": "Pending",
    "closed": "Closed",
    "deleted": "Deleted",
}


class GetSearchTeamTasksView(AuthenticatedAPIView):
    def get(self, request):
        """
        Get all tasks
        """
        request_user_id = request.user.id
        team_id = request.GET.get("team_id")
        project_id = int(request.GET.get("project_id"))
        statuses = request.GET.get("statuses")
        statuses = [STATUS_MAP.get(status.lower(), "N/A") for status in str(statuses).split(",")]
        top_n = int(request.GET.get("top_n"))

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        team_tasks = []

        # Get all tasks in all  project
        if project_id == -1:
            project_ids = list(
                ProjectMembers.objects.filter(
                    Q(team=team_id, attendee=request_user_id)
                ).values_list("project_id", flat=True)
            )

            tasks = (
                TaskMaster.objects.filter(
                    team=team_id, status__in=statuses, project__in=project_ids
                )
                .values_list(
                    "project__project_id",
                    "project__project_name",
                    "tags",
                    "task_id",
                    "title",
                    "status",
                    "ts_updated_at",
                )
                .order_by("project__project_id", "ts_updated_at")
                .reverse()
            )
        else:
            tasks = (
                TaskMaster.objects.filter(
                    team=team_id, status__in=statuses, project=int(project_id)
                )
                .values_list(
                    "project__project_id",
                    "project__project_name",
                    "tags",
                    "task_id",
                    "title",
                    "status",
                    "ts_updated_at",
                )
                .order_by("ts_updated_at")
                .reverse()
            )

        _team_tasks = defaultdict(list)
        for task in list(tasks)[:top_n]:
            _team_tasks[str(task[5]).lower()].append(
                {
                    "projectId": task[0],
                    "projectName": task[1],
                    "projectTags": task[2],
                    "taskId": task[3],
                    "title": task[4],
                    "status": {
                        "code": 0,
                        "status": task[5],
                        "color": STATUS_COLOR_MAP[task[5].lower()]["chipColor"],
                        "textColor": STATUS_COLOR_MAP[task[5].lower()]["textColor"],
                    },
                    "tsUpdated": task[6],
                }
            )

        team_tasks = []
        for _status in STATUS_MAP.keys():
            if _status in _team_tasks:
                team_tasks.extend(_team_tasks[_status.lower()])

        return Response(team_tasks, status=status.HTTP_200_OK)
