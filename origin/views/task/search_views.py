from collections import defaultdict

from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.project.prj_models import *
from origin.models.task.task_models import *
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

from .common_color import status_color

STATUS_MAP = {
    "open": "Open",
    "wip": "WIP",
    "blocked": "Blocked",
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
        raw_project_id = request.GET.get("project_id")
        raw_statuses = request.GET.get("statuses")
        raw_top_n = request.GET.get("top_n")

        if not team_id or not raw_project_id or not raw_statuses or not raw_top_n:
            return Response(
                {"error": "team_id, project_id, statuses, and top_n are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project_id = int(raw_project_id)
        statuses = [STATUS_MAP.get(s.lower(), "N/A") for s in str(raw_statuses).split(",")]
        top_n = int(raw_top_n)
        include_all = request.GET.get("include_all", "false") == "true"

        team_tasks = []

        # Get all tasks in all project
        if project_id == -1:
            project_ids = list(
                ProjectMembers.objects.filter(
                    Q(team=team_id, attendee=request_user_id)
                ).values_list("project_id", flat=True)
            )
            scope_filter = Q(project__in=project_ids)
        else:
            scope_filter = Q(project=project_id)

        tasks = (
            TaskMaster.objects.filter(
                scope_filter, team=team_id, status__in=statuses, is_init_task=False
            )
            .values_list(
                "project__project_id",
                "project__project_name",
                "tags",
                "task_id",
                "title",
                "status",
                "ts_updated_at",
                "root_task_id",
                "project__code",
                "project_task_number",
            )
            .order_by("ts_updated_at")
            .reverse()
        )

        # Unless the caller explicitly asked for finished tasks (or for
        # everything via include_all), hide tasks that live under a
        # Closed/Deleted container: walking the full parent chain covers
        # both "sub-task of a closed task" and "task in a closed
        # milestone" (closing a milestone mirrors Closed onto its
        # backing task, which is the children's parent).
        parent_by_id: dict[int, int | None] = {}
        finished_task_ids: set[int] = set()
        if statuses != ["Closed"] and statuses != ["Deleted"] and include_all == False:
            for _tid, _pid, _status in TaskMaster.objects.filter(
                scope_filter, team=team_id, is_init_task=False
            ).values_list("task_id", "parent_task_id", "status"):
                parent_by_id[_tid] = _pid
                if _status in ("Closed", "Deleted"):
                    finished_task_ids.add(_tid)

        def has_finished_ancestor(task_id: int) -> bool:
            seen = set()
            ancestor = parent_by_id.get(task_id)
            while ancestor is not None and ancestor not in seen:
                if ancestor in finished_task_ids:
                    return True
                seen.add(ancestor)
                ancestor = parent_by_id.get(ancestor)
            return False

        _team_tasks = defaultdict(list)
        for task in list(tasks)[: top_n if top_n != -1 else 100000]:
            # Skip tasks whose parent chain contains a closed/deleted task
            if has_finished_ancestor(task[3]):
                continue

            # Compute display id from the tuple positions added above.
            # Mirrors `TaskMaster.display_id` semantics — falls back to
            # "#<task_id>" when project lacks a code or task lacks a
            # number (pre-migration / orphan tasks).
            _code = task[8]
            _num = task[9]
            _display_id = f"{_code}-{_num}" if _code and _num is not None else f"#{task[3]}"

            _team_tasks[str(task[5]).lower()].append(
                {
                    "projectId": task[0],
                    "projectName": task[1],
                    "projectCode": _code,
                    "projectTags": task[2],
                    "taskId": task[3],
                    "displayId": _display_id,
                    "title": task[4],
                    "status": {
                        "code": 0,
                        "status": task[5],
                        "color": status_color(task[5])["chipColor"],
                        "textColor": status_color(task[5])["textColor"],
                    },
                    "tsUpdated": task[6],
                }
            )

        team_tasks = []
        for _status in STATUS_MAP.keys():
            if _status in _team_tasks:
                team_tasks.extend(_team_tasks[_status.lower()])

        team_tasks.sort(key=lambda t: t["projectId"])

        return Response(team_tasks, status=status.HTTP_200_OK)
