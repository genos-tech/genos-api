"""Receivers that tell the change feed a watched row was written (MIP-970).

Wired from `OriginConfig.ready`. Each receiver only reads ids already on the
instance and queues keys (`origin.services.change_feed`); the actual bump runs
after the writing transaction commits. The one exception is `ChannelMember`,
which carries no team column and costs one lookup per channel.

Registering a receiver for a model makes Django send `post_delete` for every row
of every cascade that reaches it (no "fast delete"), so receivers here are kept
free of queries: deleting a project is O(rows) instead of one statement per
table. What that costs is measured in the PR.

Writes that never reach a receiver (queryset `.update()`, `bulk_create`) call
`change_feed.bump_*` by hand at the write site. Tasks are covered at
`task_cache.invalidate_project_tasks_cache` and `update_tasks_recorded`, which
every task write path already goes through.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save

from origin.models.chat.unified_models import Channel, ChannelMember
from origin.models.common.team_models import ExternalGrant, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import (
    NoteFolderPermission,
    NoteFolderTag,
    NotePermissionMaster,
)
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint, SprintConfig
from origin.services import change_feed

# NotePermissionMaster.note_type: 1 personal, 2 task, 3 chat.
_PERMISSION_KINDS = {1: "personal", 2: "task", 3: "chat"}


def _on_write(*models):
    """Register one receiver for post_save and post_delete of each model."""

    def decorate(fn):
        for model in models:
            post_save.connect(
                fn, sender=model, weak=False, dispatch_uid=f"cf-save-{model.__name__}"
            )
            post_delete.connect(
                fn, sender=model, weak=False, dispatch_uid=f"cf-delete-{model.__name__}"
            )
        return fn

    return decorate


# --- tasks: sprints and milestones ride in the project-tasks payload --------
#
# Task rows themselves are bumped through `invalidate_project_tasks_cache`
# (see `task_signals`). A sprint or a milestone is part of the same answer (the
# focus handler reloads sprints and tasks together), so its writes move the
# same keys.


@_on_write(Sprint, SprintConfig, MilestoneMaster)
def _tasks_changed(sender, instance, **kwargs):
    change_feed.bump_tasks(instance.team_id, instance.project_id)


# --- projects ---------------------------------------------------------------


@_on_write(ProjectMaster)
def _project_changed(sender, instance, **kwargs):
    team_id = instance.team_id
    # The list itself; the project's own task keys; and the names/codes that
    # task notes and PM chat notes embed.
    change_feed.bump_projects(team_id)
    change_feed.bump_tasks(team_id, instance.project_id)
    change_feed.bump_notes(team_id, task=True, chat=True)


@_on_write(ProjectTags, ProjectLabel)
def _project_list_changed(sender, instance, **kwargs):
    # `projectTags` / `projectLabels` are part of the list payload.
    change_feed.bump_projects(instance.team_id)


@_on_write(ProjectMembers)
def _project_member_changed(sender, instance, **kwargs):
    team_id = instance.team_id
    # `isJoined` in the list; which task notes the person sees; which projects
    # `search/teamTasks` spans (`tasks:t`, via `bump_tasks`).
    change_feed.bump_projects(team_id)
    change_feed.bump_notes(team_id, task=True)
    change_feed.bump_tasks(team_id, instance.project_id)


# --- teams, grants ----------------------------------------------------------


@_on_write(TeamMembers)
def _team_member_changed(sender, instance, **kwargs):
    # Joining, leaving, or a role change alters what the person can see in
    # every collection.
    change_feed.bump_team(instance.team_id)


@_on_write(ExternalGrant)
def _grant_changed(sender, instance, **kwargs):
    # Offered, accepted, revoked: the guest team's view of the host's
    # projects, folders and chats changes, and the host's roster of who can
    # see them. (The guest's tokens also fold in the host's keys at read time.)
    change_feed.bump_team(instance.owner_team_id)
    change_feed.bump_team(instance.guest_team_id)


# --- notes ------------------------------------------------------------------


@_on_write(PersonalNoteMaster, PersonalNoteFolder, NoteFolderPermission, NoteFolderTag)
def _personal_notes_changed(sender, instance, **kwargs):
    # My Notes, shared-with-me, team folders and team notes are all
    # `notes_personal`: team notes ARE `PersonalNoteMaster` rows in team folders.
    change_feed.bump_notes(instance.team_id, personal=True)


@_on_write(TaskNoteMaster)
def _task_notes_changed(sender, instance, **kwargs):
    change_feed.bump_notes(instance.team_id, task=True)


@_on_write(ChatNoteMaster)
def _chat_notes_changed(sender, instance, **kwargs):
    change_feed.bump_notes(instance.team_id, chat=True)


@_on_write(NotePermissionMaster)
def _note_permission_changed(sender, instance, **kwargs):
    kind = _PERMISSION_KINDS.get(instance.note_type)
    if kind is None:
        # An unknown family: say "everything changed" rather than nothing.
        change_feed.bump_notes(instance.team_id, personal=True, task=True, chat=True)
    else:
        change_feed.bump_notes(instance.team_id, **{kind: True})


@_on_write(Channel)
def _channel_changed(sender, instance, **kwargs):
    # Channel titles and deletion are embedded in chat note meta.
    change_feed.bump_notes(instance.team_id, chat=True)


@_on_write(ChannelMember)
def _channel_member_changed(sender, instance, **kwargs):
    # Chat notes are visible to a channel's members. `ChannelMember` has no
    # team column: one lookup per channel (None once the channel is gone: a
    # channel delete bumps through `_channel_changed`).
    team_id = change_feed.channel_team(instance.channel_id)
    change_feed.bump_notes(team_id, chat=True)
