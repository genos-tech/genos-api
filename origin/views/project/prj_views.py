from collections import defaultdict
from django.db.models import Exists, OuterRef, Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.project.prj_models import *
from origin.models.common.inbox_models import InboxItems
from origin.serializers.project.prj_serializers import *


class ProjectMasterView(AuthenticatedAPIView):
    def post(self, request):
        serializer = ProjectMasterSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        error["hint"] = "Try with different project_name"
        return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        request_user_id = request.user.id
        team_id = request.GET.get("team_id")
        project_id = request.GET.get("project_id")

        if not team_id or not project_id:
            return Response(
                {"error": "Both 'team_id' and 'project_id' are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            target_project = ProjectMaster.objects.get(team=team_id, project_id=project_id)
            if str(request_user_id) == str(target_project.owner.id):
                target_project.delete()
                return Response(
                    {"message": f"Project `{project_id}` deleted successfully."},
                    status=status.HTTP_204_NO_CONTENT,
                )
            else:
                return Response(
                    {"message": f"Only project owner can delete the project."},
                    status=status.HTTP_200_OK,
                )
        except ProjectMaster.DoesNotExist:
            return Response(
                {"message": "Project not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


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


class ProjectsView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        attendee_id = request.GET.get("attendee_id")

        if not team_id or not attendee_id:
            return Response(
                {"error": "team_id and attendee_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        member_exists_subquery = ProjectMembers.objects.filter(
            project=OuterRef("project_id"), team=team_id, attendee=attendee_id
        )

        projects = ProjectMaster.objects.filter(team=team_id).annotate(
            is_joined=Exists(member_exists_subquery)
        )
        project_tags = defaultdict(list)
        for project_tag in ProjectTags.objects.filter(team=team_id):
            project_tags[project_tag.project.project_id].append(
                {
                    "tagName": project_tag.tag_name,
                    "tagColor": project_tag.tag_color,
                    "tagTextColor": project_tag.tag_text_color,
                }
            )

        team_projects = [
            {
                "projectId": project.project_id,
                "projectName": str(project.project_name),
                "projectTags": project_tags[project.project_id],
                "isJoined": project.is_joined,
                "systemUserId": project.project_system_user.id,
            }
            for project in projects
        ]

        return Response(team_projects, status=status.HTTP_200_OK)


class JoinProjectView(AuthenticatedAPIView):
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

        error = serializer.errors
        error["hint"] = f"Failed to join project: {request.data["project_id"]}"
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class JoinProjectFromInboxView(AuthenticatedAPIView):
    def post(self, request):
        team_id = request.data["team_id"]
        inbox_item_id = int(request.data["item_id"])

        inbox_item = InboxItems.objects.filter(item_id=inbox_item_id).values_list(
            "sender", "item_optionals"
        )[0]

        attendee_id = inbox_item[0]
        project_id = inbox_item[1]["project_id"]

        # Check if the attendee is not joined yet.
        is_joined = ProjectMembers.objects.filter(
            Q(team_id=team_id, project_id=project_id, attendee_id=attendee_id)
        ).exists()

        data = {"team": team_id, "project": project_id, "attendee": attendee_id}
        if is_joined:
            return Response(data, status=status.HTTP_201_CREATED)
        else:
            project_exists = ProjectMaster.objects.filter(
                Q(team=team_id, project_id=project_id)
            ).exists()
            if project_exists:
                serializer = ProjectMembersSerializer(data=data)
                if serializer.is_valid():
                    serializer.save()
                    return Response(serializer.data, status=status.HTTP_201_CREATED)
            else:
                return Response(data, status=status.HTTP_200_OK)

        error = serializer.errors
        error["hint"] = f"Failed to join project: {project_id}"
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class ProjectMembersView(AuthenticatedAPIView):
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
            .values("attendee__id", "attendee__username")
        )

        project_members = list(attendees)

        return Response({"project_members": project_members}, status=status.HTTP_200_OK)


class ProjectTagsView(AuthenticatedAPIView):
    def post(self, request):
        tag_count = ProjectTags.objects.filter(project=request.data["project_id"]).count()

        data = {
            "team": request.data["team_id"],
            "project": request.data["project_id"],
            "tag_id": tag_count + 1,
            "tag_name": request.data["tag_name"],
            "tag_color": request.data["tag_color"],
            "tag_text_color": request.data["tag_text_color"],
        }

        serializer = ProjectTagsSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        team_id = request.GET.get("team_id")
        project_id = request.GET.get("project_id")

        if not team_id or not project_id:
            return Response(
                {"error": "team_id and project_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tags = ProjectTags.objects.filter(team=team_id, project=project_id).values(
            "tag_name", "tag_color", "tag_text_color"
        )

        response_body = []
        for tag in tags:
            response_body.append(
                {
                    "tagName": tag["tag_name"],
                    "tagColor": tag["tag_color"],
                    "tagTextColor": tag["tag_text_color"],
                }
            )

        return Response(response_body, status=status.HTTP_200_OK)
