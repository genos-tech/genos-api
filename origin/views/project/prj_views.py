from django.db.models import Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.serializers.project.prj_serializers import (
    ProjectMasterSerializer,
    ProjectMembersSerializer,
)


class ProjectMasterView(AuthenticatedAPIView):
    def post(self, request):
        serializer = ProjectMasterSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        error["hint"] = "Try with different project_name"
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class CheckProjectExistsView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id", None)
        project_name = request.GET.get("project_name", None)

        if not project_name or not team_id:
            return Response(
                {"error": "Both team_id and project_name are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if a Project exists
        exists = ProjectMaster.objects.filter(Q(team=team_id, project_name=project_name)).exists()

        return Response({"project_exists": exists}, status=status.HTTP_200_OK)


class ProjectMembersView(AuthenticatedAPIView):
    def post(self, request):
        data = {
            "team": request.data["team_id"],
            "project": request.data["project_id"],
            "attendee": request.data["attendee_id"],
        }
        serializer = ProjectMembersSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class GetMyProjectsView(AuthenticatedAPIView):
    def get(self, request):
        user_id = request.GET.get("user_id")

        if not user_id:
            return Response(
                {"error": "user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch emails that are connected with the given email
        project_ids = ProjectMembers.objects.filter(Q(attendee=user_id)).values_list(
            "project", flat=True
        )

        return Response({"project_ids": list(project_ids)}, status=status.HTTP_200_OK)


class GetProjectMembersView(AuthenticatedAPIView):
    def get(self, request):
        user_id = request.GET.get("user_id")
        project_id = request.GET.get("project_id")

        if not user_id or not project_id:
            return Response(
                {"error": "user_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        _project_id = ProjectMembers.objects.filter(Q(attendee=user_id)).values("project")
        if len(_project_id) > 0 and _project_id[0]["project"] != project_id:
            return Response(
                {"error": f"You're not in the project `{project_id}`"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attendees = (
            ProjectMembers.objects.filter(project=project_id)
            .select_related("attendee")
            .values("attendee__email", "attendee__username")
        )

        project_members = list(attendees)

        return Response({"project_members": project_members}, status=status.HTTP_200_OK)
