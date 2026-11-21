"""Archiving a project or a group chat instead of deleting it (MIP-1693).

An archive is a reversible state. The owner sets `archived_at`; from then on the item is read-only
(one guard, `archived_project_response` / `archived_channel_response`, is called by every write path
that touches it), it leaves the default lists, and the owner restores it in one action. Nothing is
removed or moved: tasks, notes, files, history, chat and dependencies stay as they are, and reads and
links keep working for the people who had access.

Who may archive or restore is checked by the caller (the project owner; the group chat's owner).
Each change writes one security-log row and one workspace-change row, with counts and ids only.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import Channel, Message
from origin.models.common.security_event_models import SecurityAction
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.project.workspace_change_models import ACTION_UPDATE, ENTITY_PROJECT
from origin.models.task.task_models import TaskMaster
from origin.services.security_events import record_security_event
from origin.services.workspace_changes import record as record_workspace_change

ARCHIVED_CODE = "archived"
ARCHIVED_MESSAGE = "This is archived and read-only. Restore it to make changes."


def _archived_response() -> Response:
    return Response(
        {"error": ARCHIVED_MESSAGE, "code": ARCHIVED_CODE},
        status=status.HTTP_409_CONFLICT,
    )


def archived_project_response(project_id) -> Response | None:
    """409 when the project is archived, else None. One guard for every write path that names a
    project: task create and edit, comments, and the project's chat."""
    try:
        project_id = int(project_id)
    except (TypeError, ValueError):
        return None
    archived = (
        ProjectMaster.objects.filter(project_id=project_id, archived_at__isnull=False)
        .values_list("project_id", flat=True)
        .first()
    )
    return _archived_response() if archived is not None else None


def archived_channel_response(channel: Channel) -> Response | None:
    """409 when a group chat is archived, or the project it belongs to is. One guard for the chat's
    write paths (sending, editing a message, reacting)."""
    if channel.archived_at is not None:
        return _archived_response()
    if channel.project_id is not None:
        return archived_project_response(channel.project_id)
    return None


def archived_task_response(task_id) -> Response | None:
    """409 when the task's project is archived, else None. For the task's own writes (edit, comment)."""
    project_id = (
        TaskMaster.objects.filter(task_id=task_id).values_list("project_id", flat=True).first()
    )
    return archived_project_response(project_id)


def project_counts(project: ProjectMaster) -> dict[str, int]:
    """What the archive keeps, as counts. Names never go in the security log."""
    pid = project.project_id
    return {
        "tasks": TaskMaster.objects.filter(project=pid).count(),
        "members": ProjectMembers.objects.filter(project=pid).count(),
    }


def _set_project_archived(
    project: ProjectMaster, *, archived: bool, request=None, actor=None
) -> None:
    now = timezone.now() if archived else None
    # One transaction: the flag, the security row and the workspace-change row land together or not
    # at all (MIP-1886). `save()` rather than `.update()`: `.update()` fires no post_save, so the
    # project-list cache was never cleared and delta-sync clients never saw `ts_updated_at` move.
    with transaction.atomic():
        project.archived_at = now
        project.save(update_fields=["archived_at", "ts_updated_at"])
        counts = project_counts(project)
        record_security_event(
            SecurityAction.PROJECT_ARCHIVED if archived else SecurityAction.PROJECT_RESTORED,
            request=request,
            actor=actor,
            team_id=project.team_id,
            target_type="project",
            target_id=project.project_id,
            metadata={"project_id": project.project_id, **counts},
        )
        record_workspace_change(
            team_id=project.team_id,
            project_id=project.project_id,
            entity_kind=ENTITY_PROJECT,
            entity_id=project.project_id,
            action=ACTION_UPDATE,
            fields=["archived_at"],
        )


def archive_project(project: ProjectMaster, *, request=None, actor=None) -> None:
    _set_project_archived(project, archived=True, request=request, actor=actor)


def restore_project(project: ProjectMaster, *, request=None, actor=None) -> None:
    _set_project_archived(project, archived=False, request=request, actor=actor)


def _set_channel_archived(channel: Channel, *, archived: bool, request=None, actor=None) -> None:
    now = timezone.now() if archived else None
    with transaction.atomic():
        channel.archived_at = now
        channel.save(update_fields=["archived_at", "ts_updated_at"])
        record_security_event(
            SecurityAction.CHANNEL_ARCHIVED if archived else SecurityAction.CHANNEL_RESTORED,
            request=request,
            actor=actor,
            team_id=channel.team_id,
            target_type="channel",
            target_id=str(channel.id),
            metadata={
                "channel_id": str(channel.id),
                "messages": Message.objects.filter(channel=channel).count(),
            },
        )


def archive_group_chat(channel: Channel, *, request=None, actor=None) -> None:
    _set_channel_archived(channel, archived=True, request=request, actor=actor)


def restore_group_chat(channel: Channel, *, request=None, actor=None) -> None:
    _set_channel_archived(channel, archived=False, request=request, actor=actor)
