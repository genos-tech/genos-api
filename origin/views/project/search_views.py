from django.db.models import Count, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.project.prj_models import *
from origin.models.task.task_models import *


class GetTeamTasksView(AuthenticatedAPIView):
    def get(self, request):
        """
        Get all tasks
        """
        team_id = request.GET.get("team_id")

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        team_tasks = []

        # Get all tasks
        tasks = (
            ProjectMaster.objects.prefetch_related("project_tasks_master")
            .filter(team=team_id)
            .values_list(
                "project_id",
                "project_name",
                "project_tasks_master__task_id",
                "project_tasks_master__title",
                "project_tasks_master__status",
            )
            .order_by("project_tasks_master__ts_updated_at")
            .reverse()
        )

        for task in list(tasks):
            if task[2]:  # TODO: delete this
                team_tasks.append(
                    {
                        "projectId": task[0],
                        "projectName": task[1],
                        "taskId": task[2],
                        "title": task[3],
                        "status": task[4],
                    }
                )

        return Response(
            sorted(team_tasks, key=lambda x: x["projectId"]),
            status=status.HTTP_200_OK,
        )
