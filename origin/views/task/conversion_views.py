"""Convert a root task to a milestone and a milestone back to a task (MIP-1042).

The rules and the data changes are in `services/task_milestone_conversion.py`.
These views resolve the ids, check who may ask, and shape the answer so the
client can refresh and navigate (`taskId` and `displayId` are the same before
and after, which is the point: the row is kept).
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.task_milestone_conversion import (
    ConversionRefused,
    convert_milestone_to_task,
    convert_task_to_milestone,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.task.milestone_views import _serialize_milestone
from origin.views.utils.scope_guards import can_access_task, is_project_member


def _int_id(raw):
    """An id from a request body as an int, or None. A bool is not an id."""
    if isinstance(raw, bool) or raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _not_found(kind: str):
    # 404 and never 403: a 403 would say the id exists to someone who can't see it.
    return Response({"error": f"{kind} not found."}, status=status.HTTP_404_NOT_FOUND)


def task_summary(row: TaskMaster) -> dict:
    """The converted row as the client needs it to navigate and refresh."""
    return {
        "taskId": row.task_id,
        "displayId": row.display_id,
        "projectId": row.project_id,
        "isMilestone": bool(row.is_milestone),
        "milestoneId": row.milestone_id if row.is_milestone else None,
    }


class ConvertTaskToMilestoneView(AuthenticatedAPIView):
    """`POST /api/v2/task/convert-to-milestone/`  body: `{task_id}`

    The root task becomes a milestone on the same row. Answers
    `{milestone: <the milestone, as GET /milestone/<id>/ shapes it>, task: {...}}`.
    A task that can't be converted is a 400 with a stable `code`
    (`not_a_root_task`, `task_in_milestone`, `already_a_milestone`,
    `task_deleted`, `task_not_saved`).
    """

    def post(self, request):
        task_id = _int_id(request.data.get("task_id"))
        if task_id is None:
            return Response(
                {"error": "task_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST
            )
        # Who can see the task, then who belongs to its project: an assignee who
        # was never added to the project can open the task but can't restructure
        # the project's milestones (the same bar as creating or deleting one).
        if not can_access_task(task_id, request.user.id):
            return _not_found("Task")
        project_id = TaskMaster.objects.filter(task_id=task_id).values_list("project_id", flat=True)
        if not project_id or not is_project_member(project_id[0], request.user.id):
            return _not_found("Task")

        try:
            milestone = convert_task_to_milestone(task_id)
        except ConversionRefused as refused:
            return Response({"error": refused.message, "code": refused.code}, status=refused.status)

        milestone = (
            MilestoneMaster.objects.select_related("task", "reporter", "project")
            .prefetch_related("milestone_assignees__user", "task__collaborators")
            .get(milestone_id=milestone.milestone_id)
        )
        return Response(
            {"milestone": _serialize_milestone(milestone), "task": task_summary(milestone.task)},
            status=status.HTTP_200_OK,
        )


class ConvertMilestoneToTaskView(AuthenticatedAPIView):
    """`POST /api/v2/milestone/convert-to-task/`  body: `{milestone_id}`

    The milestone becomes an ordinary root task on the same row; the tasks in it
    stay under it as sub-tasks. Answers
    `{task: {...}, convertedMilestoneId, subTasksKept}`. Afterwards the old
    `GET /milestone/<id>/` answers 410 with the task (see `MilestoneView.get`).
    """

    def post(self, request):
        milestone_id = _int_id(request.data.get("milestone_id"))
        if milestone_id is None:
            return Response(
                {"error": "milestone_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST
            )
        project_id = MilestoneMaster.objects.filter(
            milestone_id=milestone_id, is_deleted=False
        ).values_list("project_id", flat=True)
        if not project_id or not is_project_member(project_id[0], request.user.id):
            return _not_found("Milestone")

        try:
            row, milestone, kept = convert_milestone_to_task(milestone_id)
        except ConversionRefused as refused:
            return Response({"error": refused.message, "code": refused.code}, status=refused.status)
        return Response(
            {
                "task": task_summary(row),
                "convertedMilestoneId": milestone.milestone_id,
                "subTasksKept": kept,
            },
            status=status.HTTP_200_OK,
        )
