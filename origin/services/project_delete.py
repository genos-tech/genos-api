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

from origin.models.chat.unified_models import Channel
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster


def project_contents(project: ProjectMaster) -> dict[str, int]:
    """How much a delete would take with it, for the person to read."""
    pid = project.project_id
    return {
        "tasks": TaskMaster.objects.filter(project=pid).count(),
        "milestones": MilestoneMaster.objects.filter(project=pid).count(),
        "sprints": Sprint.objects.filter(project=pid).count(),
        "members": ProjectMembers.objects.filter(project=pid).count(),
    }


def delete_project(project: ProjectMaster) -> None:
    # Atomic because ATOMIC_REQUESTS is off: without it a failure between the two
    # statements would strand a detached channel against a live project.
    with transaction.atomic():
        Channel.objects.filter(project=project).update(is_deleted=True, project=None)
        project.delete()
