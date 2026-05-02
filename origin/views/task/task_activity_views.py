from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.task.task_activity_models import TaskActivity
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


# Tasks generate audit rows liberally — clamp the default page so we
# never return an unbounded list. The frontend can request more via the
# `limit` / `offset` query params if needed.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _serialize_actor(user) -> dict | None:
    if user is None:
        return None
    return {
        "userId": getattr(user, "id", None),
        "userName": getattr(user, "username", None),
        # `profile_image_file_name` matches the casing used by the
        # other task endpoints (see GetTaskView) so the existing
        # `AvatarWithStatus` component path resolution Just Works.
        "avatarImgPath": getattr(user, "profile_image_file_name", None),
    }


def _serialize_activity(row: TaskActivity) -> dict:
    return {
        "activityId": row.activity_id,
        "actionType": row.action_type,
        "fieldName": row.field_name,
        "oldValue": row.old_value,
        "newValue": row.new_value,
        "metadata": row.metadata or {},
        "actor": _serialize_actor(row.actor),
        "tsCreatedAt": row.ts_created_at.isoformat() if row.ts_created_at else None,
    }


class TaskActivityListView(AuthenticatedAPIView):
    """`GET /api/v2/task/activity/?team_id=&task_id=&limit=&offset=`

    Returns the audit log for a task in **reverse chronological order**
    (newest first). Backs the new "Activity" tab in TaskTabBlock and
    can be used by the chat thread's Activities tab if we ever swap
    the PM-message feed for the structured log.
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

        try:
            limit = int(request.GET.get("limit") or DEFAULT_LIMIT)
            offset = int(request.GET.get("offset") or 0)
        except ValueError:
            return Response(
                {"error": "limit / offset must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        limit = max(1, min(limit, MAX_LIMIT))
        offset = max(0, offset)

        # team_id scopes the read to the requesting user's team — the
        # FK is nullable (legacy rows + cross-project edge cases) so
        # don't filter rows whose team is null; just exclude
        # other-team rows.
        rows = (
            TaskActivity.objects.filter(task_id=task_id)
            .filter(Q(team_id=team_id) | Q(team__isnull=True))
            .select_related("actor")
            .order_by("-ts_created_at", "-activity_id")[offset : offset + limit]
        )

        return Response([_serialize_activity(r) for r in rows], status=status.HTTP_200_OK)
