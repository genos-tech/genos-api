"""Deleting a project: what the page and the agent share (MIP-882).

A project delete is hard and cascades. Its tasks (with their comments, attachments and
notes), milestones, sprints, members and tags go with it, and there is no undo. The page
puts a confirm dialog in front of that; an agent has no dialog, so its tool asks for the
project's name as well (see `delete_project`).

Chat history outlives the project. Every project has a chat channel, `Channel.project` is
PROTECT, and `Message.channel` is PROTECT too, deliberately, so a project can't be deleted
while its channel still points at it and the channel can't be hard-deleted without losing
messages. The way through is to soft-delete the channel and let go of the project: the
channel leaves every sidebar, its messages stay on disk, and nobody can open them.

Who may delete (the owner) is checked by the caller: the page's view, the tool.
"""

from __future__ import annotations

from django.db import transaction

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.services.chat_note_access import revoke_membership_roles
from origin.services.note_delete import forget_deleted_notes
from origin.views.utils.note_role import NOTE_TYPE_TASK


def project_contents(project: ProjectMaster) -> dict[str, int]:
    """How much a delete would take with it, for the person to read."""
    pid = project.project_id
    return {
        "tasks": TaskMaster.objects.filter(project=pid).count(),
        "milestones": MilestoneMaster.objects.filter(project=pid).count(),
        "sprints": Sprint.objects.filter(project=pid).count(),
        "members": ProjectMembers.objects.filter(project=pid).count(),
    }


def _revoke_chat_note_roles(project: ProjectMaster) -> None:
    """End the roles the project's membership wrote on its chat's notes (MIP-1500).

    The `ProjectMembers` `post_delete` receiver does this for one member who leaves, but it finds
    the chat through `Channel.project`, and by the time the cascade removes the members the chat
    has been detached from the project. So it is done here, per member, while the chat can still
    be found. Same rule as leaving (`revoke_membership_roles`): rows written while the person was
    a member go, an explicit share the owner made to someone outside the project stays.
    """
    channel_ids = list(
        Channel.objects.filter(project=project, kind=ChannelKind.PM).values_list("id", flat=True)
    )
    if not channel_ids:
        return
    members = list(
        ProjectMembers.objects.filter(project=project, attendee_id__isnull=False).values_list(
            "attendee_id", "ts_joined_at"
        )
    )
    for channel_id in channel_ids:
        for attendee_id, joined_at in members:
            revoke_membership_roles(user_id=attendee_id, channel_id=channel_id, joined_at=joined_at)


def delete_project(project: ProjectMaster) -> None:
    # Atomic because ATOMIC_REQUESTS is off: without it a failure between the two
    # statements would strand a detached channel against a live project, and the revoke
    # must not outlive a delete that failed.
    with transaction.atomic():
        _revoke_chat_note_roles(project)
        Channel.objects.filter(project=project).update(is_deleted=True, project=None)
        # The project's task notes go with it (by the project and by their task), and so must
        # what other people hold about them: recents, favorites, mention rows (MIP-1514).
        note_ids = set(
            TaskNoteMaster.objects.filter(project=project).values_list("note_id", flat=True)
        ) | set(
            TaskNoteMaster.objects.filter(task__project=project).values_list("note_id", flat=True)
        )
        project.delete()
        forget_deleted_notes(NOTE_TYPE_TASK, note_ids)
