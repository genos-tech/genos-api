"""The task bar's typed search (MIP-1990): any task the caller can reach, found by title or id.

`GET /api/v2/search/tasks/` replaces "load every open task, filter in the browser". It is a
plain database query (no OpenSearch, no model call), paged, and scoped the way
`search/teamTasks` is: only projects the caller is a live member of (or was granted).
"""

import re

from django.db.models import Case, IntegerField, Q, Value, When
from rest_framework import status
from rest_framework.response import Response

from origin.models.task.task_models import TaskMaster
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import as_int, as_uuid
from origin.views.utils.scope_guards import is_project_member, reachable_project_ids

from .common_color import status_color
from .search_views import STATUS_MAP

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_QUERY_CHARS = 100
# "WEB-12" / "#web-12": a project code, then the task number.
_ID_RE = re.compile(r"^#?([A-Za-z][A-Za-z0-9]{1,5})-(\d{1,9})$")
# "web12" / "web 12": letters, then the number, the way people type it without the hyphen.
_COMPACT_ID_RE = re.compile(r"^#?([A-Za-z]{2,6})\s?(\d{1,9})$")
# "12" / "#12": a bare task number.
_NUMBER_RE = re.compile(r"^#?(\d{1,9})$")


def _non_negative_int(raw, default):
    if raw in (None, ""):
        return default
    value = as_int(raw)
    return value if value is not None and value >= 0 else None


class SearchTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = as_uuid(request.GET.get("team_id"))
        if team_id is None:
            return Response(
                {"error": "team_id must be a valid id."}, status=status.HTTP_400_BAD_REQUEST
            )
        limit = _non_negative_int(request.GET.get("limit"), DEFAULT_LIMIT)
        offset = _non_negative_int(request.GET.get("offset"), 0)
        if not limit or offset is None:
            return Response(
                {"error": "limit must be a positive and offset a non-negative integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        limit = min(limit, MAX_LIMIT)
        query = (request.GET.get("q") or "").strip()[:MAX_QUERY_CHARS]

        user_id = request.user.id
        raw_project = request.GET.get("project_id")
        if raw_project not in (None, "", "-1"):
            project_id = as_int(raw_project)
            if project_id is None or not is_project_member(project_id, user_id, live_only=True):
                return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
            scope = Q(project=project_id)
        else:
            scope = Q(project__in=reachable_project_ids(user_id, team_id))

        # Deleted is never searchable; Closed is, unless the caller narrows the statuses
        # (the starting list asks for open work only).
        raw_statuses = request.GET.get("statuses")
        statuses = [STATUS_MAP.get(s.strip().lower()) for s in (raw_statuses or "").split(",")]
        statuses = [s for s in statuses if s and s != "Deleted"]
        if raw_statuses and not statuses:
            return Response({"error": "statuses has no usable value."}, status=400)
        tasks = TaskMaster.objects.filter(scope, is_init_task=False, is_deleted=False).exclude(
            status="Deleted"
        )
        if statuses:
            tasks = tasks.filter(status__in=statuses)

        rank = Value(1)
        if query:
            match = Q(title__icontains=query)
            id_match = None
            if m := (_ID_RE.match(query) or _COMPACT_ID_RE.match(query)):
                id_match = Q(project__code__iexact=m.group(1), project_task_number=int(m.group(2)))
            elif m := _NUMBER_RE.match(query):
                id_match = Q(project_task_number=int(m.group(1)))
            if id_match is not None:
                match |= id_match
                rank = Case(
                    When(id_match, then=Value(0)), default=Value(1), output_field=IntegerField()
                )
            tasks = tasks.filter(match)
        rows = list(
            tasks.annotate(_rank=rank)
            .select_related("project", "assignee")
            .order_by("_rank", "-task_id")[offset : offset + limit + 1]
        )
        has_more = len(rows) > limit
        return Response(
            {"results": [_row(t) for t in rows[:limit]], "hasMore": has_more},
            status=status.HTTP_200_OK,
        )


def _row(task) -> dict:
    colors = status_color(task.status)
    project = task.project
    return {
        "projectId": task.project_id,
        "projectName": project.project_name,
        "projectCode": project.code,
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
        "assigneeId": str(task.assignee_id) if task.assignee_id else None,
        "assigneeName": task.assignee.username if task.assignee else None,
        "tsUpdated": task.ts_updated_at,
    }
