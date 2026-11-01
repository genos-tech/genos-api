import re
from collections import defaultdict

from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.project.prj_models import *
from origin.models.task.task_models import *
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import is_project_member, reachable_project_ids

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
            # Includes projects another team shared with this one.
            scope_filter = Q(project__in=reachable_project_ids(request_user_id, team_id))
        else:
            # The `-1` branch above derives the scope from membership,
            # which made this endpoint LOOK scoped. Naming a project
            # explicitly skipped that entirely and searched it — so the
            # one safe path was the default, and the parameterised path
            # was not.
            if not is_project_member(project_id, request_user_id):
                return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
            scope_filter = Q(project=project_id)

        # No `team=` clause: both branches above have already narrowed to
        # projects this person is in, and a project shared by another team
        # holds tasks filed under THAT team — the clause only ever dropped
        # the shared work this endpoint is asked for.
        tasks = (
            TaskMaster.objects.filter(scope_filter, status__in=statuses, is_init_task=False)
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
                "is_milestone",
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
                scope_filter, is_init_task=False
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
                    "isMilestone": bool(task[10]),
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


# "MIP-12" / "mip-12" / "#MIP-12" (a code is a letter, then up to five
# letters or digits; see prj_views._PROJECT_CODE_RE).
_DISPLAY_ID_RE = re.compile(r"^#?([A-Za-z][A-Za-z0-9]{1,5})-(\d{1,9})$")


class GetTaskByDisplayIdView(AuthenticatedAPIView):
    def get(self, request):
        """One task by its display id, whatever its status (MIP-739).

        The task lists behind the `#` menus and the task search bar hold
        open work only; this is how an exact id still finds a closed task.
        Scoped like `GetSearchTeamTasksView`'s all-projects branch: only
        projects this person can reach. Same row shape as that view."""
        team_id = request.GET.get("team_id")
        match = _DISPLAY_ID_RE.match((request.GET.get("display_id") or "").strip())
        if not team_id or not match:
            return Response(
                {"error": "team_id and a display_id like PRJ-12 are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        code, number = match.group(1).upper(), int(match.group(2))
        task = (
            TaskMaster.objects.filter(
                project__in=reachable_project_ids(request.user.id, team_id),
                project__code=code,
                project_task_number=number,
                is_init_task=False,
                is_deleted=False,
            )
            .select_related("project")
            .first()
        )
        if task is None:
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)
        colors = status_color(task.status)
        return Response(
            {
                "projectId": task.project_id,
                "projectName": task.project.project_name,
                "projectCode": task.project.code,
                "projectTags": task.tags,
                "taskId": task.task_id,
                "displayId": f"{task.project.code}-{task.project_task_number}",
                "title": task.title,
                "isMilestone": bool(task.is_milestone),
                "status": {
                    "code": 0,
                    "status": task.status,
                    "color": colors["chipColor"],
                    "textColor": colors["textColor"],
                },
                "tsUpdated": task.ts_updated_at,
            },
            status=status.HTTP_200_OK,
        )
