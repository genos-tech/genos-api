from collections import defaultdict

from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.project.prj_models import *
from origin.models.task.task_models import *
from .common_color import STATUS_COLOR_MAP


class GetSearchTeamTasksView(AuthenticatedAPIView):
    def get(self, request):
        """
        Get all tasks
        """
        team_id = request.GET.get("team_id")
        top_n = int(request.GET.get("top_n"))

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        team_tasks = []

        # Get all tasks
        tasks = (
            TaskMaster.objects.filter(team=team_id)
            .exclude(status__in=["Deleted"])
            .values_list(
                "project__project_id",
                "project__project_name",
                "tags",
                "task_id",
                "title",
                "status",
            )
            .order_by("ts_updated_at")
            .reverse()
        )

        _team_tasks = defaultdict(list)
        for task in list(tasks)[:top_n]:
            if task[2]:  # TODO: delete this
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
                    }
                )

        team_tasks = []
        for _status in ["open", "wip", "pending", "closed"]:
            if _status in _team_tasks:
                team_tasks.extend(_team_tasks[_status.lower()])

        return Response(
            sorted(team_tasks, key=lambda x: x["projectId"]),
            status=status.HTTP_200_OK,
        )
