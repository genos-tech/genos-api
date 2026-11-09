"""`GET /api/v2/task/progress/`: what changed at or under a task or milestone (MIP-1365).

The web client's counterpart of the MCP tool `get_progress_history`: one read of
`TaskProgressFact` (MIP-1351), which holds a row per change per ancestor, so it is
one indexed lookup however deep the tree is.

Each entry is the activity row EXACTLY as `GET /api/v2/task/activity/` serialises it
(so the feed that already knows how to say "changed status from Open to Closed" can
draw it) plus `sourceTask`, the task the change happened to, and `depth`, how far
below the one asked about (0 is its own change, 1 a direct child's...).

Access is PROJECT MEMBERSHIP, not `can_access_task`: the answer names tasks other
than the one asked about, and an assignee who is not a member can read their own task
but has no business reading what happened to its siblings.
"""

from datetime import datetime, timezone

from django.db.models import Q
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.response import Response

from origin.models.task.task_models import TaskMaster
from origin.models.task.task_progress_models import TaskProgressFact
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.task.task_activity_views import _serialize_activity
from origin.views.utils.scope_guards import is_project_member

DEFAULT_LIMIT = 30
MAX_LIMIT = 100


def _encode_cursor(fact: TaskProgressFact) -> str:
    return f"{fact.ts.timestamp():.6f}_{fact.fact_id}"


def _decode_cursor(raw):
    """`(ts, fact_id)` of a cursor, or None for absent OR unreadable input (the first
    page: never a 500 for a made-up cursor)."""
    if not raw:
        return None
    try:
        stamp, fact_id = str(raw).rsplit("_", 1)
        return datetime.fromtimestamp(float(stamp), tz=timezone.utc), int(fact_id)
    except (ValueError, OverflowError, OSError):
        return None


class TaskProgressListView(AuthenticatedAPIView):
    """`GET /api/v2/task/progress/?task_id=&min_depth=&since=&limit=&cursor=`

    Newest first. `min_depth=1` leaves out the task's own changes (they are in its own
    Activity feed), which is how the "under it" view asks. `limit` is 1-100 (default
    30). `nextCursor`, when not null, is passed back as `cursor` for the next page.
    """

    def get(self, request):
        try:
            task_id = int(request.GET.get("task_id"))
            limit = int(request.GET.get("limit") or DEFAULT_LIMIT)
            min_depth = int(request.GET.get("min_depth") or 0)
        except (TypeError, ValueError):
            return Response(
                {"error": "task_id is required; limit and min_depth must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        limit = max(1, min(limit, MAX_LIMIT))
        min_depth = max(0, min_depth)

        # 404 for a task that is not there AND for one the caller cannot read: ids are
        # sequential, and a 403 would turn this into an existence oracle.
        task = (
            TaskMaster.objects.filter(task_id=task_id, is_deleted=False)
            .values("project_id", "team_id")
            .first()
        )
        if task is None or not is_project_member(task["project_id"], request.user.id):
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)

        facts = TaskProgressFact.objects.filter(ancestor_task_id=task_id, depth__gte=min_depth)
        since = request.GET.get("since")
        if since:
            when = parse_datetime(since)
            if when is None:
                return Response(
                    {"error": "since must be an ISO datetime."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            facts = facts.filter(ts__gte=when)
        cursor = _decode_cursor(request.GET.get("cursor"))
        if cursor is not None:
            ts, fact_id = cursor
            facts = facts.filter(Q(ts__lt=ts) | Q(ts=ts, fact_id__lt=fact_id))

        # One more than asked for, to know whether there is a next page.
        rows = list(
            facts.select_related("activity__actor", "source_task__project").order_by(
                "-ts", "-fact_id"
            )[: limit + 1]
        )
        more = len(rows) > limit
        rows = rows[:limit]

        entries = []
        for fact in rows:
            source = fact.source_task
            entries.append(
                {
                    **_serialize_activity(fact.activity),
                    "factId": fact.fact_id,
                    "depth": fact.depth,
                    "sourceTask": {
                        "id": str(source.task_id),
                        "displayId": source.display_id,
                        "title": source.title,
                        "isMilestone": source.is_milestone,
                        "isDeleted": source.is_deleted,
                    },
                }
            )
        return Response(
            {"entries": entries, "nextCursor": _encode_cursor(rows[-1]) if more else None},
            status=status.HTTP_200_OK,
        )
