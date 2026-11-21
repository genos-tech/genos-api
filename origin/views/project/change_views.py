"""`GET /api/v2/project/changes/`: who changed a project, its sprints and its tags (MIP-1758).

Members of the project only (the same 404 as every project read, for a stranger or a missing id).
A row names the actor by id and the source by its kind (and the connected app's family, when it is
one). No title, value or key is in it: the changed FIELD NAMES only.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from origin.models.project.prj_models import ProjectMaster
from origin.services.workspace_changes import for_project
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import require_project_member_or_response


class ProjectChangesView(AuthenticatedAPIView):
    def get(self, request):
        project_id = request.GET.get("project_id")
        if res := require_project_member_or_response(request.user, project_id):
            return res
        project = ProjectMaster.objects.filter(project_id=project_id).first()
        if project is None:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
        before = request.GET.get("before")
        try:
            before_id = int(before) if before else None
        except (TypeError, ValueError):
            return Response(
                {"error": "before must be a change id."}, status=status.HTTP_400_BAD_REQUEST
            )
        rows = for_project(project.project_id, project.team_id, before=before_id)
        return Response({"changes": rows}, status=status.HTTP_200_OK)
