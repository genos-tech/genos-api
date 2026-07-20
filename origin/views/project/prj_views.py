import re
import time
from collections import defaultdict

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from origin.models.chat.unified_models import Channel
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import *
from origin.serializers.project.prj_serializers import *
from origin.services.member_roles import (
    ASSIGNABLE_ROLES,
    OWNER,
    can_manage,
    is_assignable,
    resolve_project_role,
)
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

        # Gate split three ways. `code` used to be open to ANY
        # authenticated caller — not even project membership was checked
        # — so anyone could rewrite any project's display-id prefix. It
        # joins rename under owner/editor. Transfer stays owner-only.
        actor_role = resolve_project_role(project, request.user.id)
        if (
            new_code is not None or new_name is not None or new_owner_id is not None
        ) and not can_manage(actor_role):
            return Response(
                {"error": "Only the project owner or an editor can edit the project."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if new_owner_id is not None and actor_role != OWNER:
            return Response(
                {"error": "Only the project owner can transfer ownership."},
                status=status.HTTP_403_FORBIDDEN,
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
                    {"error": "Code must be 2-6 uppercase letters/digits, starting with a letter."},
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

        # Authorization for these two happened above (rename => manager,
        # transfer => owner). What is left here is validation.
        if new_name is not None or new_owner_id is not None:
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
                    "member_role",
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
                        # Permission role. Distinct from "role" above
                        # (job title). The owner's row reads "viewer";
                        # the client overlays "owner" via ownerUserId.
                        "memberRole": attendee["member_role"],
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

            # Labels currently assigned to THIS project. Uncached
            # endpoint (the profile modal refetches on every open), so
            # unlike `ProjectsView` there's nothing to invalidate here.
            project_labels = [
                _label_payload(assignment.label)
                for assignment in ProjectLabelAssignment.objects.filter(
                    project=project_data["project_id"]
                )
                .select_related("label")
                .order_by("label__name")
            ]

            res = {
                "projectId": project_data["project_id"],
                "projectName": project_data["project_name"],
                "code": project_data.get("code"),
                "ownerUserId": project_data["owner_id"],
                "profileImagePath": project_data["profile_image_file_name"],
                "isPrivate": project_data["is_private"],
                "tsCreatedAt": project_data["ts_created_at"],
                "projectMembers": project_members,
                "projectLabels": project_labels,
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
            if target_project.owner and str(request_user_id) == str(target_project.owner.id):
                with transaction.atomic():
                    # Every project gets a PM channel from the
                    # `_ensure_pm_channel_for_project` post_save signal, and
                    # `Channel.project` is PROTECT — so deleting the project
                    # while that row still points at it raises ProtectedError
                    # and surfaces as a bare 500. Hard-deleting the channel
                    # isn't the way out either: `Message.channel` is PROTECT
                    # too, deliberately, so chat history outlives its
                    # container.
                    #
                    # Soft-delete the channel and release the FK instead.
                    # `_user_channels_qs` filters `is_deleted=False`, so the
                    # PM channel leaves every sidebar while its messages stay
                    # on disk. Atomic because ATOMIC_REQUESTS is off — without
                    # it a failure between the two statements would strand a
                    # detached channel against a live project.
                    Channel.objects.filter(project=target_project).update(
                        is_deleted=True, project=None
                    )
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

        # Team-scoped labels applied to each project (the sidebar chips).
        # One grouped query over the join table — `select_related("label")`
        # keeps it off the N+1 path. Same shape as the `project_tags` loop
        # above, but note these are a DIFFERENT axis: `projectTags` are
        # tags for the TASKS inside a project, `projectLabels` tag the
        # project itself.
        project_labels = defaultdict(list)
        for assignment in (
            ProjectLabelAssignment.objects.filter(label__team=team_id)
            .select_related("label")
            .order_by("label__name")
        ):
            project_labels[assignment.project_id].append(_label_payload(assignment.label))

        team_projects = [
            {
                "projectId": project.project_id,
                "projectName": str(project.project_name),
                "projectTags": project_tags[project.project_id],
                "projectLabels": project_labels[project.project_id],
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


class ProjectTaskTemplateView(AuthenticatedAPIView):
    """CRUD for project-scoped custom task/milestone body templates.

    Shared project-wide and managed by any project member (same trust
    model as ProjectTags). Unlike tag rename/delete, edit/delete here
    NEVER touch existing tasks: a template's body is copied into the
    task at creation, so no task holds a reference to rewrite.
    """

    @staticmethod
    def _is_member(project_id, user):
        return ProjectMembers.objects.filter(project=project_id, attendee=user).exists()

    @staticmethod
    def _serialize(template):
        return {
            "id": template.id,
            "templateName": template.template_name,
            "body": template.body,
            "createdBy": template.created_by_id,
            "tsUpdatedAt": template.ts_updated_at,
        }

    def post(self, request):
        project_id = request.data.get("project_id")
        template_name = (request.data.get("template_name") or "").strip()
        body = request.data.get("body")

        if not project_id or not template_name or body is None:
            return Response(
                {"error": "project_id, template_name and body are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not self._is_member(project_id, request.user):
            return Response(
                {"error": "Not a member of this project."},
                status=status.HTTP_403_FORBIDDEN,
            )

        data = {
            "team": request.data.get("team_id"),
            "project": project_id,
            "template_name": template_name,
            "body": body,
            "created_by": request.user.id,
        }
        serializer = ProjectTaskTemplateSerializer(data=data)
        if serializer.is_valid():
            instance = serializer.save()
            return Response(self._serialize(instance), status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        team_id = request.GET.get("team_id")
        project_id = request.GET.get("project_id")

        if not team_id or not project_id:
            return Response(
                {"error": "team_id and project_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not self._is_member(project_id, request.user):
            return Response(
                {"error": "Not a member of this project."},
                status=status.HTTP_403_FORBIDDEN,
            )

        templates = (
            ProjectTaskTemplate.objects.filter(team=team_id, project=project_id)
            .order_by("ts_updated_at")
            .reverse()
        )
        return Response(
            [self._serialize(t) for t in templates],
            status=status.HTTP_200_OK,
        )

    def put(self, request):
        template_id = request.data.get("id")
        project_id = request.data.get("project_id")

        if not template_id or not project_id:
            return Response(
                {"error": "id and project_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not self._is_member(project_id, request.user):
            return Response(
                {"error": "Not a member of this project."},
                status=status.HTTP_403_FORBIDDEN,
            )

        template = ProjectTaskTemplate.objects.filter(id=template_id, project=project_id).first()
        if not template:
            return Response(
                {"error": "Template not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if "template_name" in request.data:
            name = (request.data.get("template_name") or "").strip()
            if not name:
                return Response(
                    {"error": "template_name cannot be empty."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            template.template_name = name
        if "body" in request.data:
            template.body = request.data["body"]

        # Surface a duplicate name as a clean 400 rather than a 500. The
        # unique (project, template_name) constraint is DB-enforced.
        try:
            template.save()
        except IntegrityError:
            return Response(
                {"error": "A template with this name already exists."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Deliberately does NOT touch TaskMaster: a template's body is
        # copied into tasks at creation, so no existing task references
        # this template. (This is the key difference from tag rename,
        # which rewrites every referencing task.)
        return Response(self._serialize(template), status=status.HTTP_200_OK)

    def delete(self, request):
        template_id = request.data.get("id")
        project_id = request.data.get("project_id")

        if not template_id or not project_id:
            return Response(
                {"error": "id and project_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not self._is_member(project_id, request.user):
            return Response(
                {"error": "Not a member of this project."},
                status=status.HTTP_403_FORBIDDEN,
            )

        template = ProjectTaskTemplate.objects.filter(id=template_id, project=project_id).first()
        if not template:
            return Response(
                {"error": "Template not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        template.delete()
        # Plain row delete — existing tasks created from this template
        # keep their bodies.
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProjectTemplateDefaultsView(AuthenticatedAPIView):
    """Per-project default body template, one for tasks (and subtasks) and
    one for milestones. The value is a create-form picker string (a
    built-in id or a custom "custom:{id}") — see ProjectMaster. Any
    project member may read or set them, mirroring the template CRUD.
    """

    @staticmethod
    def _is_member(project_id, user):
        return ProjectMembers.objects.filter(project=project_id, attendee=user).exists()

    @staticmethod
    def _payload(project):
        return {
            "task": project.default_task_template,
            "milestone": project.default_milestone_template,
        }

    def get(self, request):
        project_id = request.GET.get("project_id")
        if not project_id:
            return Response(
                {"error": "project_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not self._is_member(project_id, request.user):
            return Response(
                {"error": "Not a member of this project."},
                status=status.HTTP_403_FORBIDDEN,
            )
        project = ProjectMaster.objects.filter(project_id=project_id).first()
        if not project:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._payload(project), status=status.HTTP_200_OK)

    def put(self, request):
        project_id = request.data.get("project_id")
        kind = request.data.get("kind")
        # `value` may be omitted / null to CLEAR a default (fall back to
        # the built-in). An empty string is normalized to None.
        value = request.data.get("value") or None

        if not project_id or kind not in ("task", "milestone"):
            return Response(
                {"error": "project_id and kind ('task'|'milestone') are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not self._is_member(project_id, request.user):
            return Response(
                {"error": "Not a member of this project."},
                status=status.HTTP_403_FORBIDDEN,
            )
        project = ProjectMaster.objects.filter(project_id=project_id).first()
        if not project:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        field = "default_task_template" if kind == "task" else "default_milestone_template"
        setattr(project, field, value)
        project.save(update_fields=[field, "ts_updated_at"])
        return Response(self._payload(project), status=status.HTTP_200_OK)


class ProjectTaskFieldRulesView(AuthenticatedAPIView):
    """Owner-configured required/default rules for the metadata fields of
    tasks and milestones created under a project (ProjectMaster.
    task_field_rules). GET is member-gated — every member's create form
    needs the rules (and ownerUserId, so the client can gate the
    customize UI without a second fetch). PUT is OWNER-gated and
    shape-validates the whole blob, which it replaces atomically (an
    empty dict clears all rules). Enforcement is UI-only: task/milestone
    creation endpoints never consult these rules.
    """

    ALLOWED_PRIORITY = {"Minimal", "Low", "Normal", "High", "Critical"}
    ALLOWED_EFFORT = {"Minimal", "Low", "Moderate", "High", "Extensive"}
    # NOTE: no "sprint" (dropped from the feature), no "status" (always
    # auto-set to "Open" at creation — never customizable), and no
    # "project" (always required, never stored).
    ALLOWED_FIELDS = {
        "dueDate",
        "effortLevel",
        "priority",
        "tags",
        "reporter",
        "assignee",
    }

    @staticmethod
    def _is_member(project_id, user):
        return ProjectMembers.objects.filter(project=project_id, attendee=user).exists()

    @staticmethod
    def _payload(project):
        return {
            "rules": project.task_field_rules or {},
            "ownerUserId": str(project.owner.id) if project.owner else None,
        }

    @classmethod
    def _validate_default(cls, field, cfg):
        """Per-field default-value checks. Returns an error string or None."""
        if field == "dueDate":
            offset = cfg.get("defaultOffsetDays")
            # bool is an int subclass — reject it explicitly.
            if offset is not None and (
                isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 3650
            ):
                return "'defaultOffsetDays' must be null or an integer between 0 and 3650."
            return None
        if field == "tags":
            names = cfg.get("defaultTagNames")
            if names is None:
                return None
            if not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names):
                return "'defaultTagNames' must be a list of non-empty strings."
            return None
        default = cfg.get("default")
        if default is None:
            return None
        if field in ("effortLevel", "priority"):
            allowed = {
                "effortLevel": cls.ALLOWED_EFFORT,
                "priority": cls.ALLOWED_PRIORITY,
            }[field]
            if default not in allowed:
                return f"'{default}' is not a valid default for '{field}'."
            return None
        # assignee / reporter: "creator" or a user id. Ids are validated
        # shape-only — the client drops ids that left the team live.
        if not isinstance(default, str) or not default:
            return f"'default' for '{field}' must be null, 'creator', or a user id."
        return None

    @classmethod
    def _validate(cls, rules):
        """Whitelist-validate the whole blob. Returns an error string or None."""
        if not isinstance(rules, dict):
            return "rules must be an object."
        for field, cfg in rules.items():
            if field not in cls.ALLOWED_FIELDS:
                return f"Unknown field '{field}'."
            if not isinstance(cfg, dict):
                return f"Config for '{field}' must be an object."
            allowed_cfg = {"required"}
            if field == "dueDate":
                allowed_cfg |= {"defaultOffsetDays"}
            elif field == "tags":
                allowed_cfg |= {"defaultTagNames"}
            else:
                allowed_cfg |= {"default"}
            for key in cfg:
                if key not in allowed_cfg:
                    return f"Unknown key '{key}' for '{field}'."
            if "required" in cfg and not isinstance(cfg["required"], bool):
                return f"'required' for '{field}' must be a boolean."
            if err := cls._validate_default(field, cfg):
                return err
        return None

    def get(self, request):
        project_id = request.GET.get("project_id")
        if not project_id:
            return Response(
                {"error": "project_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not self._is_member(project_id, request.user):
            return Response(
                {"error": "Not a member of this project."},
                status=status.HTTP_403_FORBIDDEN,
            )
        project = ProjectMaster.objects.filter(project_id=project_id).first()
        if not project:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._payload(project), status=status.HTTP_200_OK)

    def put(self, request):
        project_id = request.data.get("project_id")
        rules = request.data.get("rules")
        if not project_id or rules is None:
            return Response(
                {"error": "project_id and rules are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        project = ProjectMaster.objects.filter(project_id=project_id).first()
        if not project:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
        if not can_manage(resolve_project_role(project, request.user.id)):
            return Response(
                {"error": "Only the project owner or an editor can configure task field rules."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if err := self._validate(rules):
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        project.task_field_rules = rules
        project.save(update_fields=["task_field_rules", "ts_updated_at"])
        return Response(self._payload(project), status=status.HTTP_200_OK)


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

        try:
            project_data = ProjectMaster.objects.get(project_id=project_id)
        except (ProjectMaster.DoesNotExist, ValueError, TypeError):
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        # This endpoint had NO authorization check at all — any
        # authenticated user could replace any project's avatar. The FE
        # never surfaced that, so the hole was invisible. Same class as
        # the team-image hole closed in the Team PR.
        if not can_manage(resolve_project_role(project_data, request.user.id)):
            return Response(
                {"error": "Only the project owner or an editor can change the project image."},
                status=status.HTTP_403_FORBIDDEN,
            )

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


def _label_payload(label):
    """Serialize one `ProjectLabel` into the frontend's camelCase shape.

    `labelId` is the contract: assignment and every mutation address a
    label by id, never by name. `ProjectTagsView` keys on `tag_name`,
    which is why a rename there has to rewrite every referencing task.
    """
    return {
        "labelId": label.label_id,
        "name": label.name,
        "color": label.color,
        "textColor": label.text_color,
    }


def _require_project_manager(request, project_id):
    """Resolve a project and assert the caller may manage it.

    Returns `(project, None)` on success or `(None, Response)` to return
    verbatim. The catalog is TEAM-shared and any project owner may edit
    it, so the gate is "you own the project you're acting from" — the UI
    only ever exposes these actions inside ModalProjectProfile, which
    always has a project in hand. Mirrors the 403 in
    `ProjectMasterView.put`: the UI hides the controls, and the server
    re-checks so a hand-crafted request can't bypass them.
    """
    if not project_id:
        return None, Response(
            {"error": "project_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        project = ProjectMaster.objects.get(project_id=project_id)
    except (ProjectMaster.DoesNotExist, ValueError, TypeError):
        return None, Response(
            {"error": "Project not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    if not can_manage(resolve_project_role(project, request.user.id)):
        return None, Response(
            {"error": "Only the project owner or an editor can manage project labels."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return project, None


def _invalidate_label_project_list(team_id):
    """Wipe every team member's cached `/project/projects/` payload.

    That payload embeds `projectLabels`, so any catalog rename/recolor/
    delete or assignment change makes it stale for up to the 60s TTL.
    Enumerated + `delete_many` rather than `delete_pattern`, matching
    `_invalidate_project_list`: exact, and it works on the LocMemCache
    the test suite uses (where `delete_pattern` is a silent no-op).

    Called explicitly from the mutating views rather than wired as a
    `post_save`/`post_delete` receiver in `signals/cache_invalidation.py`
    like the other project keys. Two reasons: the assignment replace path
    uses `bulk_create`, which fires NO `post_save` at all (a receiver
    would silently miss exactly the most common mutation); and
    `ProjectLabelAssignment` carries no `team` column, so a `post_delete`
    receiver firing under a label cascade would have to re-read a row
    that is being deleted to find the team. The views already hold
    `project.team_id`, so doing it here is both correct and cheaper.
    """
    if team_id is None:
        return
    attendee_ids = TeamMembers.objects.filter(team_id=team_id).values_list("attendee_id", flat=True)
    keys = [f"project:list:{team_id}:{attendee_id}" for attendee_id in attendee_ids if attendee_id]
    if keys:
        cache.delete_many(keys)


class ProjectLabelsView(AuthenticatedAPIView):
    """Team-scoped catalog of labels used to organize PROJECTS.

    GET is open to any authenticated team member (the chips render for
    everyone); every mutation is owner-gated via `_require_project_owner`.

    Note this is a different axis from `ProjectTagsView` — that one
    manages tags applied to TASKS within a single project. See the
    `ProjectLabel` model docstring.
    """

    def get(self, request):
        team_id = request.GET.get("team_id")
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        labels = ProjectLabel.objects.filter(team=team_id).order_by("name")
        # Assigned-project counts so the manage UI can warn about the
        # blast radius before a delete ("used by 4 projects"). One
        # grouped query — not a per-label count().
        counts = defaultdict(int)
        for label_id in ProjectLabelAssignment.objects.filter(label__team=team_id).values_list(
            "label_id", flat=True
        ):
            counts[label_id] += 1

        return Response(
            [{**_label_payload(label), "projectCount": counts[label.label_id]} for label in labels],
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        """Create a catalog label. Owner-gated via the acting project."""
        project, err = _require_project_manager(request, request.data.get("project_id"))
        if err:
            return err

        name = (request.data.get("name") or "").strip()
        if not name:
            return Response(
                {"error": "name is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(name) > 30:
            return Response(
                {"error": "name must be 30 characters or fewer."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # The DB constraint is case-sensitive; block case-variant
        # duplicates here so "Client" / "client" can't both exist.
        if ProjectLabel.objects.filter(team=project.team_id, name__iexact=name).exists():
            return Response(
                {"error": "A label with that name already exists in this team."},
                status=status.HTTP_409_CONFLICT,
            )

        label = ProjectLabel.objects.create(
            team_id=project.team_id,
            name=name,
            color=request.data.get("color") or "#7c3aed",
            text_color=request.data.get("text_color") or "#ffffff",
            created_by=request.user,
        )
        # A brand-new label is unassigned, so no project's payload
        # changed yet — nothing to invalidate here (assign does it).
        return Response(
            {**_label_payload(label), "projectCount": 0},
            status=status.HTTP_201_CREATED,
        )

    def put(self, request):
        """Rename / recolor a catalog label.

        Applies team-wide: every project carrying this label re-renders
        with the new value. That's the point of the normalized model —
        no per-referrer rewrite like `ProjectTagsView.put` needs.
        """
        project, err = _require_project_manager(request, request.data.get("project_id"))
        if err:
            return err

        label_id = request.data.get("label_id")
        try:
            label = ProjectLabel.objects.get(label_id=label_id, team=project.team_id)
        except (ProjectLabel.DoesNotExist, ValueError, TypeError):
            return Response({"error": "Label not found."}, status=status.HTTP_404_NOT_FOUND)

        update_fields = []
        if "name" in request.data:
            name = (request.data.get("name") or "").strip()
            if not name:
                return Response(
                    {"error": "name cannot be empty."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if len(name) > 30:
                return Response(
                    {"error": "name must be 30 characters or fewer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            collision = (
                ProjectLabel.objects.filter(team=project.team_id, name__iexact=name)
                .exclude(label_id=label.label_id)
                .exists()
            )
            if collision:
                return Response(
                    {"error": "A label with that name already exists in this team."},
                    status=status.HTTP_409_CONFLICT,
                )
            label.name = name
            update_fields.append("name")
        if "color" in request.data:
            label.color = request.data.get("color") or label.color
            update_fields.append("color")
        if "text_color" in request.data:
            label.text_color = request.data.get("text_color") or label.text_color
            update_fields.append("text_color")

        if update_fields:
            update_fields.append("ts_updated_at")
            label.save(update_fields=update_fields)
            _invalidate_label_project_list(project.team_id)

        return Response(_label_payload(label), status=status.HTTP_200_OK)

    def delete(self, request):
        """Remove a label from the catalog.

        Assignments cascade away, so every project simply stops showing
        the chip. Nothing else is rewritten — projects reference the
        label by FK, so there is no denormalized copy to clean up.
        """
        project, err = _require_project_manager(
            request, request.data.get("project_id") or request.GET.get("project_id")
        )
        if err:
            return err

        label_id = request.data.get("label_id") or request.GET.get("label_id")
        try:
            label = ProjectLabel.objects.get(label_id=label_id, team=project.team_id)
        except (ProjectLabel.DoesNotExist, ValueError, TypeError):
            return Response({"error": "Label not found."}, status=status.HTTP_404_NOT_FOUND)

        label.delete()
        _invalidate_label_project_list(project.team_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class ProjectLabelAssignmentView(AuthenticatedAPIView):
    """Which catalog labels apply to ONE project. Owner-gated."""

    def put(self, request):
        """Replace this project's label set with `label_ids` (full set).

        A full-set PUT rather than add/remove deltas: the picker UI is a
        multi-select whose state IS the desired set, and replacing
        idempotently avoids the double-click / stale-checkbox races an
        incremental API invites.
        """
        project, err = _require_project_manager(request, request.data.get("project_id"))
        if err:
            return err

        raw_ids = request.data.get("label_ids")
        if not isinstance(raw_ids, list):
            return Response(
                {"error": "label_ids must be a list."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Scope to the project's OWN team so a caller can't attach
        # another team's label by guessing an id.
        valid_labels = ProjectLabel.objects.filter(team=project.team_id, label_id__in=raw_ids)
        valid_ids = set(valid_labels.values_list("label_id", flat=True))

        with transaction.atomic():
            ProjectLabelAssignment.objects.filter(project=project.project_id).exclude(
                label_id__in=valid_ids
            ).delete()
            existing = set(
                ProjectLabelAssignment.objects.filter(project=project.project_id).values_list(
                    "label_id", flat=True
                )
            )
            ProjectLabelAssignment.objects.bulk_create(
                [
                    ProjectLabelAssignment(label_id=lid, project_id=project.project_id)
                    for lid in valid_ids - existing
                ],
                ignore_conflicts=True,
            )

        _invalidate_label_project_list(project.team_id)
        return Response(
            [_label_payload(label) for label in valid_labels.order_by("name")],
            status=status.HTTP_200_OK,
        )


class ProjectMemberRoleView(AuthenticatedAPIView):
    """Set another member's permission role within a project.

    Mirrors `TeamMemberRoleView` — owner or editor may call it, the
    target must not be the owner (that's a transfer), and the new role
    must be assignable (editor/viewer, never owner).

    No cache invalidation needed: `/project/profile/` — the only payload
    carrying `memberRole` — is uncached, and `/project/projects/` (which
    is cached) carries no member rows.
    """

    def put(self, request):
        project_id = request.data.get("project_id")
        user_id = request.data.get("user_id")
        member_role = request.data.get("member_role")

        if not project_id or not user_id or not member_role:
            return Response(
                {"error": "project_id, user_id and member_role are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            project = ProjectMaster.objects.get(project_id=project_id)
        except (ProjectMaster.DoesNotExist, ValueError, TypeError):
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        if not can_manage(resolve_project_role(project, request.user.id)):
            return Response(
                {"error": "Only the project owner or an editor can change member roles."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not is_assignable(member_role):
            return Response(
                {"error": f"member_role must be one of {list(ASSIGNABLE_ROLES)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if project.owner_id is not None and str(project.owner_id) == str(user_id):
            return Response(
                {
                    "error": "The project owner's role cannot be changed. "
                    "Transfer ownership instead."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        row = ProjectMembers.objects.filter(project_id=project_id, attendee_id=user_id).first()
        if row is None:
            return Response(
                {"error": "That user is not a member of this project."},
                status=status.HTTP_404_NOT_FOUND,
            )

        row.member_role = member_role
        row.save(update_fields=["member_role", "ts_updated_at"])

        return Response(
            {"userId": str(user_id), "memberRole": member_role},
            status=status.HTTP_200_OK,
        )
