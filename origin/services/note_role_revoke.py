"""Ending the explicit roles a task note's or a team-folder note's membership wrote (MIP-1493).

`get_effective_role` honours an explicit `NotePermissionMaster` row ahead of every membership
check. For a TASK note and for a PERSONAL note the only row the app writes for any reason other
than a share is the AUTHOR's Owner row (`POST /api/v2/note/task/`, `POST /api/v2/note/personal/`,
the agent's `create_note`): it is written while the author is on the project (task note) or on
the team (a note in a Team Notes folder) and stands for that membership. Nothing ever took it
back, so a person who left the project or the team, or whose note moved to a project they are
not on, kept Owner on the note: they could still read it and write to it.

What goes, and what does not:

  * the Owner row of a task note ends when its holder leaves the note's PROJECT, or when the
    note moves to a project the holder is not on;
  * every row a leaver holds on the team's task notes and on the notes filed in its Team Notes
    folders ends when they leave the TEAM, shares included: a share can only go to a team
    member, so nobody outside the team holds access to its notes;
  * a SHARE (the owner's Editor/Viewer grant, `note_sharing.share_note`, or the Viewer row an
    approved access request writes) is the note's own ACL and stays when the person leaves the
    project, and when a note moves;
  * the author's Owner row on their own note in a personal folder (My Notes) stays: that is not
    shared space, and what happens to a leaver's private notes is a separate decision.

Chat notes are `chat_note_access`, a separate module.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.views.utils.note_role import (
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_OWNER,
)

log = logging.getLogger(__name__)


def _search_narrowed(label: str, note_ids) -> None:
    """The note chunks' ACL is stamped at index time (`unshare_note` does the same): drop the
    chunks now, and touch the notes so the incremental reindex restores them with the narrower
    ACL. Best effort; the reindex sweep retries."""
    note_ids = sorted(note_ids)
    if not note_ids:
        return
    try:
        from origin.search_engine.purge import purge_entities

        # One bulk call for every note, the entities `purge_note` names for each.
        code = NOTE_TYPE_TASK if label == "task" else NOTE_TYPE_PERSONAL
        purge_entities(
            [("note", f"note:{label}:{n}") for n in note_ids]
            + [("note_summary", f"note_summary:{code}:{n}") for n in note_ids]
        )
        model = TaskNoteMaster if label == "task" else PersonalNoteMaster
        model.objects.filter(note_id__in=note_ids).update(ts_updated_at=timezone.now())
    except Exception:  # noqa: BLE001 - a role change must commit even when search is down
        log.exception("Failed to narrow the search index for %s notes %s", label, note_ids)


def _delete_rows(rows, label: str) -> int:
    note_ids = set(rows.values_list("note_id", flat=True))
    if not note_ids:
        return 0
    deleted, _ = rows.delete()
    _search_narrowed(label, note_ids)
    return deleted


def revoke_task_author_roles(*, user_id, project_id) -> int:
    """`user_id` left `project_id`: delete their Owner row on its task notes. Shares stay."""
    if not user_id or not project_id:
        return 0
    return _delete_rows(
        NotePermissionMaster.objects.filter(
            user_id=user_id,
            note_type=NOTE_TYPE_TASK,
            role_id=ROLE_OWNER,
            note_id__in=TaskNoteMaster.objects.filter(project_id=project_id).values("note_id"),
        ),
        "task",
    )


def revoke_task_roles_on_move(*, moves, target_project_id) -> int:
    """Task notes moved to `target_project_id`: delete the Owner row of everyone who is not on
    that project. `moves` is `[(note_id, old_project_id), ...]`, read before the notes were
    re-filed; notes that did not change project are skipped. Whoever is on the target project
    keeps their row (their membership carries on), and shares stay."""
    note_ids = {n for n, old in moves if old != target_project_id}
    if not note_ids or not target_project_id:
        return 0
    members = ProjectMembers.objects.filter(project_id=target_project_id).values("attendee_id")
    return _delete_rows(
        NotePermissionMaster.objects.filter(
            note_type=NOTE_TYPE_TASK, role_id=ROLE_OWNER, note_id__in=note_ids
        ).exclude(user_id__in=members),
        "task",
    )


def _team_folder_note_ids(team_id) -> set[int]:
    """Every note filed in a Team Notes folder of `team_id`, child notes included."""
    from origin.views.utils.note_acl_resync import collect_note_ids_in_folders

    folder_ids = set(
        PersonalNoteFolder.objects.filter(
            team_id=team_id, scope=PersonalNoteFolder.SCOPE_TEAM
        ).values_list("folder_id", flat=True)
    )
    if not folder_ids:
        return set()
    return collect_note_ids_in_folders(folder_ids, team_id)


def revoke_team_roles(*, user_id, team_id) -> int:
    """`user_id` left the team: delete every role they hold on its task notes and on the notes
    in its Team Notes folders."""
    if not user_id or not team_id:
        return 0
    with transaction.atomic():
        deleted = _delete_rows(
            NotePermissionMaster.objects.filter(
                user_id=user_id,
                note_type=NOTE_TYPE_TASK,
                note_id__in=TaskNoteMaster.objects.filter(team_id=team_id).values("note_id"),
            ),
            "task",
        )
        shared_space = _team_folder_note_ids(team_id)
        if shared_space:
            deleted += _delete_rows(
                NotePermissionMaster.objects.filter(
                    user_id=user_id, note_type=NOTE_TYPE_PERSONAL, note_id__in=shared_space
                ),
                "personal",
            )
    return deleted
