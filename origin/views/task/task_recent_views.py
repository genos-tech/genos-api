"""Tasks the caller opened (MIP-1063): record an open, and the sidebar's Recents list."""

from rest_framework import status
from rest_framework.response import Response

from origin.models.task.task_models import TaskMaster
from origin.services.task_recents import can_open, record_open, visible_recents
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import as_int, as_uuid, validate_request_data

from .common_color import status_color

# Recents includes finished tasks on purpose: it is how you check one right
# after closing or deleting it.
RECENT_STATUSES = ["Open", "WIP", "Blocked", "Pending", "Closed", "Deleted"]


def _serialize(row) -> dict:
    """The row shape of `GET /search/teamTasks/`, plus `tsOpened`."""
    task = row.task
    colors = status_color(task.status)
    return {
        "projectId": task.project_id,
        "projectName": task.project.project_name,
        "projectCode": task.project.code,
        "projectTags": task.tags,
        "taskId": task.task_id,
        "displayId": task.display_id,
        "title": task.title,
        "isMilestone": bool(task.is_milestone),
        "status": {
            "code": 0,
            "status": task.status,
            "color": colors["chipColor"],
            "textColor": colors["textColor"],
        },
        "tsUpdated": task.ts_updated_at,
        "tsOpened": row.ts_opened_at,
    }


class RecordTaskOpenView(AuthenticatedAPIView):
    """The caller just opened a task: move it to the top of their recents."""

    def post(self, request):
        task_id = as_int(request.data.get("task_id"))
        if res := validate_request_data({"task_id": task_id}):
            return res
        task = TaskMaster.objects.select_related("project").filter(task_id=task_id).first()
        # One answer for "no such task" and "not yours to open": task ids are
        # sequential, so telling them apart would be an existence oracle.
        if task is None or not can_open(request.user.id, task):
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)
        row = record_open(user_id=request.user.id, task=task)
        return Response(
            {"taskId": task.task_id, "tsOpened": row.ts_opened_at}, status=status.HTTP_200_OK
        )


class RecentTasksView(AuthenticatedAPIView):
    """The caller's opened tasks they can still open, newest open first."""

    def get(self, request):
        raw_team_id = request.GET.get("team_id")
        if res := validate_request_data({"team_id": raw_team_id}):
            return res
        # Answered here, in its own words: the net under every view (MIP-1060) would
        # also give a 400, but it logs a warning for each one it catches.
        team_id = as_uuid(raw_team_id)
        if team_id is None:
            return Response(
                {"error": "team_id must be a valid id."}, status=status.HTTP_400_BAD_REQUEST
            )
        rows = visible_recents(request.user.id, team_id, RECENT_STATUSES)
        return Response([_serialize(r) for r in rows], status=status.HTTP_200_OK)
