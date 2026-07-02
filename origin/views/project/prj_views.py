import re
import time
from collections import defaultdict

from django.core.cache import cache
from django.db.models import Exists, OuterRef, Q
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from origin.models.common.inbox_models import InboxItems
from origin.models.project.prj_models import *
from origin.serializers.project.prj_serializers import *
from origin.services.project_code import derive_project_code
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import validate_request_data

_PROJECT_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]{1,5}$")


class ProjectMasterView(AuthenticatedAPIView):
    def put(self, request):
        """Update editable project fields: `code`, `project_name`, and
        `owner_id`. `code` is open to any caller (display-id prefix);
        `project_name` and `owner_id` are gated to the project owner so
        the existing owner can hand off before leaving. The new owner
        must already be a member of the project."""
        project_id = request.data.get("project_id")
        new_code = request.data.get("code")
        new_name = request.data.get("project_name")
        new_owner_id = request.data.get("owner_id")
        if not project_id:
            return Response(
                {"error": "project_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            project = ProjectMaster.objects.get(project_id=project_id)
        except ProjectMaster.DoesNotExist:
            return Response(
                {"error": "Project not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if new_code is not None:
            new_code = (new_code or "").strip().upper()
            if not new_code:
                return Response(
                    {"error": "Code cannot be empty."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not _PROJECT_CODE_RE.match(new_code):
                return Response(
                    {
                        "error": "Code must be 2-6 uppercase letters/digits, starting with a letter."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if (
                ProjectMaster.objects.filter(team=project.team_id, code=new_code)
                .exclude(project_id=project.project_id)
                .exists()
            ):
                return Response(
                    {"error": "Another project in this team already uses that code."},
                    status=status.HTTP_409_CONFLICT,
                )
            project.code = new_code
            project.save(update_fields=["code", "ts_updated_at"])

        # Owner-only edits: project_name + owner transfer. Re-check the
        # ownership server-side even though the UI hides the inputs —
        # protects against hand-crafted requests.
        if new_name is not None or new_owner_id is not None:
            if not project.owner or str(project.owner.id) != str(request.user.id):
                return Response(
                    {"error": "Only the project owner can change name or owner."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            owner_update_fields = []
            if new_name is not None:
                new_name = (new_name or "").strip()
                if not new_name:
                    return Response(
                        {"error": "project_name cannot be empty."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                collision = (
                    ProjectMaster.objects.filter(team=project.team_id, project_name=new_name)
                    .exclude(project_id=project.project_id)
                    .exists()
                )
                if collision:
                    return Response(
                        {"error": "Another project in this team already uses that name."},
                        status=status.HTTP_409_CONFLICT,
                    )
                project.project_name = new_name
                owner_update_fields.append("project_name")

            if new_owner_id is not None and str(new_owner_id) != str(project.owner.id):
                is_member = ProjectMembers.objects.filter(
                    team=project.team_id,
                    project=project.project_id,
                    attendee_id=new_owner_id,
                ).exists()
                if not is_member:
                    return Response(
                        {"error": "The new owner must already be a member of the project."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                project.owner_id = new_owner_id
                owner_update_fields.append("owner")

            if owner_update_fields:
                owner_update_fields.append("ts_updated_at")
                project.save(update_fields=owner_update_fields)

        return Response(ProjectMasterSerializer(project).data, status=status.HTTP_200_OK)

    def post(self, request):
        # Auto-derive a unique-per-team `code` if the caller didn't
        # supply one. The code is the prefix in human-readable task
        # display IDs (e.g. "GEN-42"). Users can edit it later from
        # project settings.
        data = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
        if not data.get("code"):
            team_id = data.get("team")
            taken = set(
                ProjectMaster.objects.filter(team=team_id, code__isnull=False).values_list(
                    "code", flat=True
                )
            )
            data["code"] = derive_project_code(data.get("project_name", "") or "", taken)

        serializer = ProjectMasterSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        error["hint"] = "Try with different project_name"
        return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):

        data = {
            "team_id": request.GET.get("team_id"),
            "project_id": request.GET.get("project_id"),
        }

        if res := validate_request_data(data):
            return res

        project_data = ProjectMaster.objects.filter(Q(project_id=data["project_id"])).values()

        if len(project_data) == 1:
            project_data = project_data[0]

            raw_project_members = (
                ProjectMembers.objects.filter(Q(project_id=data["project_id"]))
                .values(
                    "team__team_id",
                    "team__team_name",
                    "attendee__id",
                    "attendee__username",
                    "attendee__email",
                    "attendee__profile_image_file_name",
                    "attendee__is_offline_forced",
                    "attendee__role",
                    "attendee__base_country",
                    "attendee__custom_status",
                    "attendee__ts_created_at",
                    "attendee__is_system_user",
                )
                .order_by("attendee__email")
            )

            project_members = []
            for attendee in raw_project_members:
                project_members.append(
                    {
                        "teamId": attendee["team__team_id"],
                        "teamName": attendee["team__team_name"],
                        "userId": attendee["attendee__id"],
                        "userName": attendee["attendee__username"],
                        "userEmail": attendee["attendee__email"],
                        "avatarImgPath": attendee["attendee__profile_image_file_name"],
                        "isOfflineForced": (
                            attendee["attendee__is_offline_forced"]
                            if attendee["attendee__is_offline_forced"]
                            else ""
                        ),
                        "role": (attendee["attendee__role"] if attendee["attendee__role"] else ""),
                        "baseCountry": (
                            attendee["attendee__base_country"]
                            if attendee["attendee__base_country"]
                            else ""
                        ),
                        "customStatus": (
                            attendee["attendee__custom_status"]
                            if attendee["attendee__custom_status"]
                            else ""
                        ),
                        "tsLastSeen": "",
                        "tsJoined": attendee["attendee__ts_created_at"],
                    }
                )

            res = {
                "projectId": project_data["project_id"],
                "projectName": project_data["project_name"],
                "code": project_data.get("code"),
                "ownerUserId": project_data["owner_id"],
                "profileImagePath": project_data["profile_image_file_name"],
                "isPrivate": project_data["is_private"],
                "tsCreatedAt": project_data["ts_created_at"],
                "projectMembers": project_members,
            }

            return Response(res, status=status.HTTP_200_OK)
        else:
            return Response(
                {"error": "Project not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

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
                    {"message": "Only project owner can delete the project."},
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

        cache_key = f"project:list:{team_id}:{attendee_id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached, status=status.HTTP_200_OK)

        member_exists_subquery = ProjectMembers.objects.filter(
            project=OuterRef("project_id"), team=team_id, attendee=attendee_id
        )

        projects = (
            ProjectMaster.objects.filter(team=team_id)
            .annotate(is_joined=Exists(member_exists_subquery))
            .order_by("ts_updated_at")
            .reverse()
        )
        project_tags = defaultdict(list)
        for project_tag in (
            ProjectTags.objects.filter(team=team_id).order_by("ts_updated_at").reverse()
        ):
            if project_tag.project:
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
                "isPrivate": project.is_private,
                "isJoined": project.is_joined,
                "systemUserId": project.project_system_user.id,
            }
            for project in projects
        ]

        cache.set(cache_key, team_projects, timeout=60)
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
        error["hint"] = f"Failed to join project: {request.data['project_id']}"
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class LeaveProjectView(AuthenticatedAPIView):
    """Hard-delete the requester's project membership.

    Project owners cannot leave (would orphan the project). `ProjectMembers`
    has no soft-delete flag, so removing the row is the canonical exit;
    re-join goes through the existing `JoinProjectView` which simply
    inserts a fresh row.
    """

    def post(self, request):
        team_id = request.data.get("team_id")
        project_id = request.data.get("project_id")
        attendee_id = request.data.get("attendee_id")
        if not team_id or not project_id or not attendee_id:
            return Response(
                {"error": "team_id, project_id, and attendee_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if str(request.user.id) != str(attendee_id):
            return Response(
                {"error": "You can only leave a project on your own behalf."},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            project = ProjectMaster.objects.get(project_id=project_id, team=team_id)
        except ProjectMaster.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        if project.owner and str(project.owner.id) == str(attendee_id):
            return Response(
                {"error": "The project owner cannot leave the project."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        deleted, _ = ProjectMembers.objects.filter(
            team=team_id, project=project_id, attendee=attendee_id
        ).delete()
        if deleted == 0:
            return Response(
                {"error": "You are not a member of this project."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {"team_id": team_id, "project_id": project_id, "attendee_id": attendee_id},
            status=status.HTTP_200_OK,
        )


class JoinProjectFromInboxView(AuthenticatedAPIView):
    def post(self, request):
        team_id = request.data["team_id"]
        inbox_item_id = int(request.data["item_id"])

        inbox_item = InboxItems.objects.filter(item_id=inbox_item_id).values_list(
            "sender", "item_optionals"
        )[0]

        attendee_id = inbox_item[0]
        project_id = inbox_item[1]["project_id"]
        project_name = inbox_item[1]["project_name"]

        # Check if the attendee is not joined yet.
        is_joined = ProjectMembers.objects.filter(
            Q(team_id=team_id, project_id=project_id, attendee_id=attendee_id)
        ).exists()

        data = {"team": team_id, "project": project_id, "attendee": attendee_id}
        if is_joined:
            data["projectName"] = project_name
            return Response(data, status=status.HTTP_201_CREATED)
        else:
            project_exists = ProjectMaster.objects.filter(
                Q(team=team_id, project_id=project_id)
            ).exists()
            if project_exists:
                serializer = ProjectMembersSerializer(data=data)
                if serializer.is_valid():
                    serializer.save()
                    res = serializer.data
                    res["projectName"] = project_name
                    return Response(res, status=status.HTTP_201_CREATED)
            else:
                data["projectName"] = project_name
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

        tags = (
            ProjectTags.objects.filter(team=team_id, project=project_id)
            .order_by("ts_updated_at")
            .reverse()
            .values("tag_name", "tag_color", "tag_text_color")
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

    def put(self, request):
        project_id = request.data.get("project_id")
        old_tag_name = request.data.get("old_tag_name")

        if not project_id or not old_tag_name:
            return Response(
                {"error": "project_id and old_tag_name are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tag = ProjectTags.objects.filter(project=project_id, tag_name=old_tag_name).first()

        if not tag:
            return Response(
                {"error": "Tag not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if "tag_name" in request.data:
            tag.tag_name = request.data["tag_name"]
        if "tag_color" in request.data:
            tag.tag_color = request.data["tag_color"]
        if "tag_text_color" in request.data:
            tag.tag_text_color = request.data["tag_text_color"]
        tag.save()

        # Also update all tasks that reference this tag by old name
        if "tag_name" in request.data and request.data["tag_name"] != old_tag_name:
            from origin.models.task.task_models import TaskMaster

            tasks = TaskMaster.objects.filter(
                project=project_id, tags__contains=[{"tagName": old_tag_name}]
            )
            for task in tasks:
                updated_tags = []
                for t in task.tags:
                    if t.get("tagName") == old_tag_name:
                        updated_tags.append(
                            {
                                "tagName": request.data["tag_name"],
                                "tagColor": request.data.get("tag_color", tag.tag_color),
                                "tagTextColor": request.data.get(
                                    "tag_text_color", tag.tag_text_color
                                ),
                            }
                        )
                    else:
                        updated_tags.append(t)
                task.tags = updated_tags
                task.save(update_fields=["tags"])
        elif "tag_color" in request.data or "tag_text_color" in request.data:
            from origin.models.task.task_models import TaskMaster

            tasks = TaskMaster.objects.filter(
                project=project_id, tags__contains=[{"tagName": tag.tag_name}]
            )
            for task in tasks:
                updated_tags = []
                for t in task.tags:
                    if t.get("tagName") == tag.tag_name:
                        updated_tags.append(
                            {
                                "tagName": tag.tag_name,
                                "tagColor": tag.tag_color,
                                "tagTextColor": tag.tag_text_color,
                            }
                        )
                    else:
                        updated_tags.append(t)
                task.tags = updated_tags
                task.save(update_fields=["tags"])

        return Response(
            {
                "tagName": tag.tag_name,
                "tagColor": tag.tag_color,
                "tagTextColor": tag.tag_text_color,
            },
            status=status.HTTP_200_OK,
        )

    def delete(self, request):
        project_id = request.data.get("project_id")
        tag_name = request.data.get("tag_name")

        if not project_id or not tag_name:
            return Response(
                {"error": "project_id and tag_name are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tag = ProjectTags.objects.filter(project=project_id, tag_name=tag_name).first()

        if not tag:
            return Response(
                {"error": "Tag not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        tag.delete()

        # Remove the tag from all tasks that reference it
        from origin.models.task.task_models import TaskMaster

        tasks = TaskMaster.objects.filter(
            project=project_id, tags__contains=[{"tagName": tag_name}]
        )
        for task in tasks:
            task.tags = [t for t in task.tags if t.get("tagName") != tag_name]
            task.save(update_fields=["tags"])

        return Response(status=status.HTTP_204_NO_CONTENT)


class ProjectProfileImageView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser]

    def put(self, request):
        project_id = request.POST.get("project_id")
        profile_image = request.FILES.get("profile_image")

        data = {
            "project_id": project_id,
            "profile_image": profile_image,
        }

        if res := validate_request_data(data):
            return res

        project_data = ProjectMaster.objects.get(project_id=project_id)

        # Only update the FileField
        new_profile_image_data = {
            "profile_image_url": profile_image,
        }

        serializer = ProjectMasterSerializer(
            project_data, data=new_profile_image_data, partial=True
        )
        if serializer.is_valid():
            saved_user = serializer.save()

            # At this point, Django has stored the file, possibly renamed
            # Now get the actual stored filename
            stored_file_name = saved_user.profile_image_url.name.split("/")[-1]
            # Append `?v=<ms-timestamp>` so the served URL is unique per
            # upload — mirrors UserProfileImageView. Today's storage is
            # FileSystemStorage, which collision-suffixes filenames, so the
            # path already changes per upload; but if media moves to S3/R2
            # (see settings — `AWS_S3_FILE_OVERWRITE` defaults True there),
            # the FE's fixed `profile.jpg` name would reuse the same path
            # and the browser would serve the stale cached avatar. The
            # query string is ignored by media path-matching, and the
            # `profile_image_url` FileField keeps the clean path. The
            # `_ensure_pm_channel_for_project` signal then carries this
            # busted string onto the PM `Channel.profile_image_url`.
            cache_buster = int(time.time() * 1000)
            saved_user.profile_image_file_name = (
                f"project_profiles/{project_id}/{stored_file_name}?v={cache_buster}"
            )
            saved_user.save(update_fields=["profile_image_file_name"])

            return Response(ProjectMasterSerializer(saved_user).data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
