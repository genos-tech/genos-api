"""Archive and restore a project, and a group chat (MIP-1693).

Only the owner may archive or restore (the same person who may delete today). A stranger gets the
same 404 as a missing project, as the delete view does; a member who is not the owner gets 403.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.project.prj_models import ProjectMaster
from origin.services.archive import (
    archive_group_chat,
    archive_project,
    restore_group_chat,
    restore_project,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import is_project_member


def _owned_project(request):
    team_id = request.GET.get("team_id")
    project_id = request.GET.get("project_id")
    if not team_id or not project_id:
        return None, Response(
            {"error": "Both 'team_id' and 'project_id' are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    project = ProjectMaster.objects.filter(
        team=team_id, project_id=project_id, is_deleted=False
    ).first()
    if project is None or not is_project_member(project.project_id, request.user.id):
        return None, Response({"message": "Project not found."}, status=status.HTTP_404_NOT_FOUND)
    if project.owner_id is None or str(project.owner_id) != str(request.user.id):
        return None, Response(
            {"message": "Only the project owner can archive or restore it."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return project, None


class ProjectArchiveView(AuthenticatedAPIView):
    """POST /api/v2/project/archive/?team_id=&project_id= — read-only, reversible, nothing removed."""

    def post(self, request):
        project, error = _owned_project(request)
        if error is not None:
            return error
        if project.archived_at is None:
            archive_project(project, request=request)
        return Response(
            {"project_id": project.project_id, "archived": True},
            status=status.HTTP_200_OK,
        )


class ProjectRestoreView(AuthenticatedAPIView):
    """POST /api/v2/project/restore/?team_id=&project_id= — the owner puts an archive back."""

    def post(self, request):
        project, error = _owned_project(request)
        if error is not None:
            return error
        if project.archived_at is not None:
            restore_project(project, request=request)
        return Response(
            {"project_id": project.project_id, "archived": False},
            status=status.HTTP_200_OK,
        )


def _owned_group_chat(request, channel_id):
    channel = Channel.objects.filter(id=channel_id, is_deleted=False).first()
    is_member = (
        channel is not None
        and ChannelMember.objects.filter(channel=channel, user=request.user).exists()
    )
    if channel is None or not is_member or channel.kind != ChannelKind.GM:
        return None, Response({"error": "Group chat not found."}, status=status.HTTP_404_NOT_FOUND)
    if channel.owner_id is None or str(channel.owner_id) != str(request.user.id):
        return None, Response(
            {"error": "Only the group chat's owner can archive or restore it."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return channel, None


class GroupChatArchiveView(AuthenticatedAPIView):
    """POST /api/v3/channels/{id}/archive/ — the owner archives a group chat."""

    def post(self, request, channel_id):
        channel, error = _owned_group_chat(request, channel_id)
        if error is not None:
            return error
        if channel.archived_at is None:
            archive_group_chat(channel, request=request)
        return Response({"id": str(channel.id), "archived": True}, status=status.HTTP_200_OK)


class GroupChatRestoreView(AuthenticatedAPIView):
    """POST /api/v3/channels/{id}/restore/ — the owner puts a group chat back."""

    def post(self, request, channel_id):
        channel, error = _owned_group_chat(request, channel_id)
        if error is not None:
            return error
        if channel.archived_at is not None:
            restore_group_chat(channel, request=request)
        return Response({"id": str(channel.id), "archived": False}, status=status.HTTP_200_OK)
