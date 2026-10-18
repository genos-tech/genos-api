from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.task.task_delivery_models import TaskDelivery
from origin.services.task_delivery import delivery_as_dict
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import can_access_task


class TaskDeliveryListView(AuthenticatedAPIView):
    """`GET /api/v2/task/deliveries/?team_id=&task_id=`

    The branches and PRs delivering a task (MIP-687), for the task page
    (MIP-701). Same shape as the agent tools' `deliveries`
    (`delivery_as_dict`) plus `delivery_id` for a stable list key, and
    the same order as `get_task`: oldest first, the order the work
    happened in.

    The PR title is NOT wrapped here. Wrapping marks GitHub text as data
    for an LLM; the UI renders it as plain text, which React escapes.

    Gated exactly like `TaskActivityListView`: anyone who can open the
    task, and a 404 for everyone else, so a walkable `task_id` reveals
    nothing about tasks the caller cannot see.
    """

    def get(self, request):
        team_id = request.GET.get("team_id")
        raw_task_id = request.GET.get("task_id")
        if not team_id or not raw_task_id:
            return Response(
                {"error": "team_id and task_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            task_id = int(raw_task_id)
        except ValueError:
            return Response(
                {"error": "task_id must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not can_access_task(task_id, request.user.id):
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)

        # `team` is SET_NULL, so a row whose team was deleted keeps its
        # task; keep those, exclude only rows stamped with another team.
        rows = (
            TaskDelivery.objects.filter(task_id=task_id)
            .filter(Q(team_id=team_id) | Q(team__isnull=True))
            .order_by("delivery_id")
        )
        return Response(
            [{"delivery_id": row.delivery_id, **delivery_as_dict(row)} for row in rows],
            status=status.HTTP_200_OK,
        )
