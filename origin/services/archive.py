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

from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.chat.unified_models import Channel, Message
from origin.models.common.security_event_models import SecurityAction
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.project.workspace_change_models import (
    ACTION_UPDATE,
    ENTITY_CHANNEL,
    ENTITY_PROJECT,
)
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


def ensure_project_writable(project_id, refused) -> None:
    """Raise `refused(ARCHIVED_MESSAGE, 409)` when the project is archived (MIP-1887).

    For the services whose own refusal class takes `(message, status)` (SprintRefused,
    CatalogRefused, ProjectRefused): REST, MCP and the in-app agent already turn those into their
    own error, so one call here covers all three."""
    if archived_project_response(project_id) is not None:
        raise refused(ARCHIVED_MESSAGE, status.HTTP_409_CONFLICT)


# Argument names that point at a project's data, per kind of target. The MCP gate reads these.
_TASK_ARGS = ("task_id", "parent_task_id", "blocker_task_id", "blocked_task_id")


def _project_ids_in(args, found: set[int]) -> None:
    """Project ids a tool call's arguments touch: its own `project_id`, and the project of every
    task, milestone or sprint it names (nested lists of items too)."""
    from origin.models.task.milestone_models import MilestoneMaster
    from origin.models.task.sprint_models import Sprint

    if isinstance(args, list):
        for item in args:
            _project_ids_in(item, found)
        return
    if not isinstance(args, dict):
        return

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    for key, value in args.items():
        if isinstance(value, (list, dict)):
            _project_ids_in(value, found)
            continue
        number = _int(value)
        if number is None:
            continue
        if key == "project_id":
            found.add(number)
        elif key in _TASK_ARGS:
            pid = TaskMaster.objects.filter(task_id=number).values_list("project_id", flat=True)
            found.update(p for p in pid[:1] if p is not None)
        elif key == "milestone_id":
            pid = MilestoneMaster.objects.filter(milestone_id=number).values_list(
                "project_id", flat=True
            )
            found.update(p for p in pid[:1] if p is not None)
        elif key == "sprint_id":
            pid = Sprint.objects.filter(sprint_id=number).values_list("project_id", flat=True)
            found.update(p for p in pid[:1] if p is not None)


def archived_refusal_for_args(args: dict) -> str | None:
    """The shared refusal text when a write tool call touches an archived project, else None.
    For the MCP gate (MIP-1887): checks the project the call names, and the project of each task,
    milestone and sprint it names, so a move OUT of or INTO an archive is refused too."""
    found: set[int] = set()
    _project_ids_in(args, found)
    if not found:
        return None
    archived = ProjectMaster.objects.filter(project_id__in=found, archived_at__isnull=False)
    return ARCHIVED_MESSAGE if archived.exists() else None


def archived_channel_response(channel: Channel) -> Response | None:
    """409 when a group chat is archived, or the project it belongs to is. One guard for the chat's
    write paths (sending, editing a message, reacting)."""
    if channel.archived_at is not None:
        return _archived_response()
    if channel.project_id is not None:
        return archived_project_response(channel.project_id)
    return None


def archived_project_of_task(task_id):
    """The project id a task lives in (None for a missing task)."""
    return TaskMaster.objects.filter(task_id=task_id).values_list("project_id", flat=True).first()


def archived_task_response(task_id) -> Response | None:
    """409 when the task's project is archived, else None. For the task's own writes (edit, comment)."""
    return archived_project_response(archived_project_of_task(task_id))


def archived_task_note_response(note_id) -> Response | None:
    """409 when the project a task note lives in is archived (a note's own writes: autosave, delete,
    move out, attachments, version restore). A missing note is None: the caller answers 404."""
    project_id = (
        TaskNoteMaster.objects.filter(note_id=note_id).values_list("project_id", flat=True).first()
    )
    return archived_project_response(project_id)


def archived_chat_note_response(note_id) -> Response | None:
    """409 when the note's channel is archived, or its project is. A missing note is None."""
    channel = Channel.objects.filter(chat_notes__note_id=note_id).first()
    return archived_channel_response(channel) if channel is not None else None


def archived_note_refusal(family: str, note_id) -> str | None:
    """The refusal text when a note lives in an archived project or chat, else None (MIP-1939).
    For the note tools of the in-app agent and MCP, which write note rows directly. Personal-family
    notes are never archived."""
    if family == "task":
        response = archived_task_note_response(note_id)
    elif family == "chat":
        response = archived_chat_note_response(note_id)
    else:
        return None
    return ARCHIVED_MESSAGE if response is not None else None


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
        record_workspace_change(
            team_id=channel.team_id,
            entity_kind=ENTITY_CHANNEL,
            entity_uuid=channel.id,
            action=ACTION_UPDATE,
            fields=["archived_at"],
        )


def archive_group_chat(channel: Channel, *, request=None, actor=None) -> None:
    _set_channel_archived(channel, archived=True, request=request, actor=actor)


def restore_group_chat(channel: Channel, *, request=None, actor=None) -> None:
    _set_channel_archived(channel, archived=False, request=request, actor=actor)


def archived_refusal(project) -> str | None:
    """The words that refuse new work in an archived project (a loaded `ProjectMaster`), else None.
    For the agent and MCP create tools, which run after the membership check so a stranger learns
    nothing about a project's state."""
    if getattr(project, "archived_at", None) is not None:
        return ARCHIVED_MESSAGE
    return None


def mark_archived_hits(hits: list[dict]) -> None:
    """Set `archived: True` on each search hit that lives in an archived project or group chat
    (MIP-1852). Search still finds archived items and ranks them as before; this only labels them.
    The key is added only when true, so a live hit is byte for byte what it was. At most two
    queries, none when no hit names a project or a chat."""
    project_ids: set[int] = set()
    chat_ids: set[UUID] = set()
    for h in hits:
        try:
            if h.get("project_id") not in (None, ""):
                project_ids.add(int(h["project_id"]))
        except (TypeError, ValueError):
            pass
        try:
            if h.get("chat_id"):
                chat_ids.add(UUID(str(h["chat_id"])))
        except (TypeError, ValueError, AttributeError):
            pass
    archived_projects = (
        set(
            ProjectMaster.objects.filter(
                project_id__in=project_ids, archived_at__isnull=False
            ).values_list("project_id", flat=True)
        )
        if project_ids
        else set()
    )
    archived_chats = (
        {
            str(c)
            for c in Channel.objects.filter(id__in=chat_ids)
            .filter(Q(archived_at__isnull=False) | Q(project__archived_at__isnull=False))
            .values_list("id", flat=True)
        }
        if chat_ids
        else set()
    )
    for h in hits:
        try:
            in_project = h.get("project_id") not in (None, "") and (
                int(h["project_id"]) in archived_projects
            )
        except (TypeError, ValueError):
            in_project = False
        if in_project or (h.get("chat_id") and str(h["chat_id"]).lower() in archived_chats):
            h["archived"] = True
