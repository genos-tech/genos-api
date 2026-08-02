"""Public API v1.

    GET    /api/public/v1/me/                    who this key acts as
    GET    /api/public/v1/projects/              projects the key can see
    GET    /api/public/v1/tasks/                 tasks, optionally by project
    POST   /api/public/v1/tasks/                 create a task      (write)
    GET    /api/public/v1/tasks/<id>/            one task
    PATCH  /api/public/v1/tasks/<id>/            update a task      (write)

## Scoping

Every read goes through `scope_guards`, the same helpers the app uses.
That is the point: the public API must not be able to see anything its
principal cannot see through the UI, so it borrows the app's ACL rather
than reimplementing it. A second implementation is how the two drift.

A **team key** is pinned to its own team — the `team_id` parameter is
ignored for it, because letting a key name a different team would make
the binding advisory. A **personal token** must name a team it belongs
to, since a person can be in several.

## Field names

snake_case here, unlike the camelCase the app's own endpoints return.
The app's casing is an artefact of what its client wanted; a public API
is a contract with strangers and should look like the ecosystem it lives
in. Diverging deliberately once is better than exporting an accident
forever.
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.views.public.permissions import HasApiKey, HasRequiredScope, PublicApiThrottle
from origin.views.utils.scope_guards import (
    can_access_task,
    is_project_member,
    is_team_member,
    member_project_ids,
)

MAX_PAGE = 100


class PublicApiView(APIView):
    """Shared base: API key required, scope enforced, throttled per key."""

    permission_classes = [HasApiKey, HasRequiredScope]
    throttle_classes = [PublicApiThrottle]

    def resolve_team(self, request):
        """The team this request operates in, or `None`.

        A team key answers this by itself. A personal token has to say,
        and has to belong.
        """
        key = request.auth
        if key.team_id:
            return TeamMaster.objects.filter(team_id=key.team_id, is_deleted=False).first()
        team_id = request.query_params.get("team_id") or (
            request.data.get("team_id") if hasattr(request.data, "get") else None
        )
        if not team_id:
            return None
        if not is_team_member(team_id, request.user.id):
            return None
        return TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()


def _team_required():
    return Response(
        {
            "error": "team_id is required for a personal access token, "
            "and must name a team you belong to."
        },
        status=status.HTTP_400_BAD_REQUEST,
    )


def _serialize_project(p: ProjectMaster) -> dict:
    return {
        "id": p.project_id,
        "name": p.project_name,
        "code": p.code,
        "team_id": str(p.team_id) if p.team_id else None,
        "is_private": p.is_private,
        "created_at": p.ts_created_at,
    }


def _serialize_task(t: TaskMaster) -> dict:
    return {
        "id": t.task_id,
        "display_id": t.display_id,
        "title": t.title,
        "status": t.status,
        "priority": t.priority,
        "project_id": t.project_id,
        "assignee_id": str(t.assignee_id) if t.assignee_id else None,
        "reporter_id": str(t.reporter_id) if t.reporter_id else None,
        "due_date": t.due_date,
        "start_date": t.start_date,
        "created_at": t.ts_created_at,
        "updated_at": t.ts_updated_at,
    }


class MeView(PublicApiView):
    """Key introspection. The first call an integrator makes, and the one
    that tells them whether their key works before anything else fails
    for a subtler reason."""

    def get(self, request):
        key = request.auth
        return Response(
            {
                "user_id": str(request.user.id),
                "username": request.user.username,
                "email": request.user.email,
                "key": {
                    "id": str(key.id),
                    "name": key.name,
                    "scope": key.scope,
                    "team_id": str(key.team_id) if key.team_id else None,
                },
            },
            status=status.HTTP_200_OK,
        )


class ProjectListView(PublicApiView):
    def get(self, request):
        team = self.resolve_team(request)
        if team is None:
            return _team_required()
        project_ids = member_project_ids(request.user.id, team_id=team.team_id)
        projects = ProjectMaster.objects.filter(
            project_id__in=project_ids, is_deleted=False
        ).order_by("project_id")
        return Response(
            {"projects": [_serialize_project(p) for p in projects]},
            status=status.HTTP_200_OK,
        )


class TaskListCreateView(PublicApiView):
    def get(self, request):
        team = self.resolve_team(request)
        if team is None:
            return _team_required()

        # Default scope is every project the principal belongs to —
        # never the whole team, which is the shape that made
        # GetTeamTasksView a finding in ACL_AUDIT.md.
        allowed = member_project_ids(request.user.id, team_id=team.team_id)
        raw_project = request.query_params.get("project_id")
        if raw_project:
            try:
                project_id = int(raw_project)
            except (TypeError, ValueError):
                return Response(
                    {"error": "project_id must be an integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if project_id not in allowed:
                return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
            allowed = [project_id]

        try:
            limit = min(int(request.query_params.get("limit", 50)), MAX_PAGE)
            offset = max(int(request.query_params.get("offset", 0)), 0)
        except (TypeError, ValueError):
            return Response(
                {"error": "limit and offset must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = TaskMaster.objects.filter(
            project_id__in=allowed, is_deleted=False, is_init_task=False
        ).order_by("-ts_updated_at")
        total = qs.count()
        tasks = qs[offset : offset + limit]
        return Response(
            {
                "tasks": [_serialize_task(t) for t in tasks],
                "total": total,
                "limit": limit,
                "offset": offset,
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        team = self.resolve_team(request)
        if team is None:
            return _team_required()

        title = (request.data.get("title") or "").strip()
        raw_project = request.data.get("project_id")
        if not title or raw_project is None:
            return Response(
                {"error": "title and project_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not is_project_member(raw_project, request.user.id):
            # 404 rather than 403: project ids are sequential integers.
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        task = TaskMaster.objects.create(
            team=team,
            project_id=raw_project,
            title=title[:255],
            status=request.data.get("status") or "Open",
            priority=request.data.get("priority"),
            # The KEY's user is the reporter. Attribution has to name a
            # real person: "created by an integration" is not something
            # the task surfaces can render, and a null reporter would
            # drop the task out of `can_access_task` for its own author.
            reporter=request.user,
        )
        return Response(_serialize_task(task), status=status.HTTP_201_CREATED)


class TaskDetailView(PublicApiView):
    def get(self, request, task_id: int):
        if not can_access_task(task_id, request.user.id):
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)
        task = TaskMaster.objects.filter(task_id=task_id, is_deleted=False).first()
        if task is None:
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(_serialize_task(task), status=status.HTTP_200_OK)

    def patch(self, request, task_id: int):
        if not can_access_task(task_id, request.user.id):
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)
        task = TaskMaster.objects.filter(task_id=task_id, is_deleted=False).first()
        if task is None:
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)

        # An allowlist, not `**request.data`. The app's own task PUT
        # accepts a broad payload; a public API that forwarded arbitrary
        # keys would let an integrator write columns nobody meant to
        # expose, and every new model field would silently join the API.
        fields = []
        for field, cap in (("title", 255), ("status", None), ("priority", None)):
            if field in request.data:
                value = request.data.get(field)
                if field == "title":
                    value = (value or "").strip()
                    if not value:
                        return Response(
                            {"error": "title cannot be empty."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    value = value[:cap]
                setattr(task, field, value)
                fields.append(field)

        if not fields:
            return Response(
                {"error": "No writable fields supplied."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        task.ts_updated_at = timezone.now()
        task.save(update_fields=[*fields, "ts_updated_at"])
        return Response(_serialize_task(task), status=status.HTTP_200_OK)
