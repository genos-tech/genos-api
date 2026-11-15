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

Coming back (MIP-1511): a person re-added to a project gets their Owner row back on the task
notes whose `owner_id` is them (`restore_task_author_roles`), so they can share and unshare their
note again. It only fills a gap: a row that exists, an Editor share the person was handed while
away included, is never touched. A task note's author who is NOT on its project reads and writes
nothing through `owner_id` any more (the agent, search, briefing and timeline gates ask the same
role rule the app does); nobody else is made a manager of such an orphan note.

Moving back (MIP-1583): the Owner row a MOVE ends is recorded (`TaskNoteRoleRevoke`), and a move
to a project the author is on writes it back (`restore_task_roles_on_move`), exactly the rows that
were recorded and only for a member of the project the note is in now. The record is consumed when
it is applied, when the person joins a project the note is in (`restore_task_author_roles`, which
so leaves none for a member), and when they leave the team, so it can only ever give back what a
move took, never an access something else ended.

Chat notes are `chat_note_access`, a separate module.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from origin.models.note.common_note_models import NotePermissionMaster, TaskNoteRoleRevoke
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


def restore_task_author_roles(*, user_id, project_id) -> int:
    """`user_id` joined `project_id` (again): write their Owner row on the task notes of the
    project that name them as author and that they hold no row on. Idempotent; returns how many
    rows it wrote."""
    if not user_id or not project_id:
        return 0
    # The person is on the project again: what a move recorded is superseded by this rule.
    TaskNoteRoleRevoke.objects.filter(
        user_id=user_id,
        note_id__in=TaskNoteMaster.objects.filter(project_id=project_id).values("note_id"),
    ).delete()
    held = NotePermissionMaster.objects.filter(user_id=user_id, note_type=NOTE_TYPE_TASK)
    missing = list(
        TaskNoteMaster.objects.filter(project_id=project_id, owner_id=user_id)
        .exclude(note_id__in=held.values("note_id"))
        .values_list("note_id", "team_id")
    )
    if not missing:
        return 0
    NotePermissionMaster.objects.bulk_create(
        [
            NotePermissionMaster(
                team_id=team_id,
                user_id=user_id,
                note_type=NOTE_TYPE_TASK,
                note_id=note_id,
                role_id=ROLE_OWNER,
            )
            for note_id, team_id in missing
        ],
        # a concurrent writer (the note's own create) may have just written it
        ignore_conflicts=True,
    )
    return len(missing)


def revoke_task_roles_on_move(*, moves, target_project_id) -> int:
    """Task notes moved to `target_project_id`: delete the Owner row of everyone who is not on
    that project, and record each one (MIP-1583). `moves` is `[(note_id, old_project_id), ...]`,
    read before the notes were re-filed; notes that did not change project are skipped. Whoever is
    on the target project keeps their row (their membership carries on), and shares stay.

    Returns how many NOTES lost an Owner row (what the caller tells the person who moved them)."""
    old_project = {n: old for n, old in moves if old != target_project_id}
    if not old_project or not target_project_id:
        return 0
    members = ProjectMembers.objects.filter(project_id=target_project_id).values("attendee_id")
    rows = NotePermissionMaster.objects.filter(
        note_type=NOTE_TYPE_TASK, role_id=ROLE_OWNER, note_id__in=old_project
    ).exclude(user_id__in=members)
    with transaction.atomic():
        held = list(rows.values_list("user_id", "note_id"))
        if not held:
            return 0
        # `ignore_conflicts`: an older record of the same grant stays as it is.
        TaskNoteRoleRevoke.objects.bulk_create(
            [
                TaskNoteRoleRevoke(
                    user_id=user_id, note_id=note_id, from_project_id=old_project[note_id]
                )
                for user_id, note_id in held
            ],
            ignore_conflicts=True,
        )
        _delete_rows(rows, "task")
    return len({note_id for _, note_id in held})


def restore_task_roles_on_move(*, moves, target_project_id) -> int:
    """Task notes moved to `target_project_id`: write back the Owner row a move once ended for each
    person who is on that project now (MIP-1583). Only a recorded row is written, never one the
    person did not hold, and never for someone who is not a member of the project the note is in.
    A person who holds any row on the note by now (a share they were handed while away included)
    keeps it as it is; their record is consumed either way. Returns how many rows it wrote."""
    note_ids = {n for n, old in moves if old != target_project_id}
    if not note_ids or not target_project_id:
        return 0
    members = ProjectMembers.objects.filter(project_id=target_project_id).values("attendee_id")
    records = TaskNoteRoleRevoke.objects.filter(note_id__in=note_ids, user_id__in=members)
    with transaction.atomic():
        pairs = list(records.values_list("user_id", "note_id"))
        if not pairs:
            return 0
        held = set(
            NotePermissionMaster.objects.filter(
                note_type=NOTE_TYPE_TASK,
                note_id__in={n for _, n in pairs},
                user_id__in={u for u, _ in pairs},
            ).values_list("user_id", "note_id")
        )
        teams = dict(
            TaskNoteMaster.objects.filter(note_id__in={n for _, n in pairs}).values_list(
                "note_id", "team_id"
            )
        )
        missing = [(u, n) for u, n in pairs if (u, n) not in held and n in teams]
        NotePermissionMaster.objects.bulk_create(
            [
                NotePermissionMaster(
                    team_id=teams[n],
                    user_id=u,
                    note_type=NOTE_TYPE_TASK,
                    note_id=n,
                    role_id=ROLE_OWNER,
                )
                for u, n in missing
            ],
            ignore_conflicts=True,
        )
        records.delete()
    return len(missing)


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
        # A record of a move's removed row goes with the membership it stood for (MIP-1583).
        TaskNoteRoleRevoke.objects.filter(
            user_id=user_id,
            note_id__in=TaskNoteMaster.objects.filter(team_id=team_id).values("note_id"),
        ).delete()
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
