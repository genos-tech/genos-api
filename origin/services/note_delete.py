"""Deleting notes and note folders: the pages' rules and the agent's (MIP-883).

Everything here is a HARD delete. A note goes with its permission rows, its version
history and its search chunks; there is no trash and no undo. A folder goes with
every folder below it and every note filed in any of them, and each such note's own
`parent_note_id` chain.

The three note tables share one shape, so one function deletes any of them. The two
folder spaces differ, and so do their rules, kept as the pages have them:

* **My Notes** (`delete_personal_folder`): the owner deletes the whole subtree. The
  page's confirm dialog is the only gate, and it removes notes that were shared with
  other people, too.
* **Team Notes** (`delete_team_folder`): only a folder's Owner, and it REFUSES (409,
  with counts) while anyone else's note or folder is inside, so one person can't
  destroy a colleague's work.

Each caller keeps its own authentication and its own extra refusals; a `DeleteRefused`
carries the message and status the page answers with.
"""

from __future__ import annotations

from django.db import transaction

from origin.models.chat.unified_models import Activity
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine.purge import purge_note, purge_note_folders
from origin.views.utils.note_acl_resync import (
    collect_folder_subtree_ids,
    collect_note_ids_in_folders,
    purge_notes_from_index,
)
from origin.views.utils.note_folder_role import get_folder_role
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_OWNER,
    delete_note_permissions,
)
from origin.views.utils.note_version import delete_note_versions

# family label -> (table, the NOTE_TYPE_* the permission and version tables use)
FAMILIES = {
    "personal": (PersonalNoteMaster, NOTE_TYPE_PERSONAL),
    "task": (TaskNoteMaster, NOTE_TYPE_TASK),
    "chat": (ChatNoteMaster, NOTE_TYPE_CHAT),
}


class DeleteRefused(Exception):
    """A delete the rules do not allow. `status` is the HTTP code the page answers
    with; `extra` is whatever else that answer carries (the team folder's counts)."""

    def __init__(self, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra or {}


_FORGET_BATCH = 500


def forget_deleted_notes(code: int, note_ids) -> None:
    """Drop what other people still hold about notes that are gone (MIP-1489, MIP-1514): their
    "recently opened" and favorite rows, and the @mention / comment `Activity` rows the notes
    wrote for them (surface types 6/7/8, `meta.noteId`), which a full resync of the feed would
    otherwise bring back, pointing at a note that no longer exists. `code` is the `NOTE_TYPE_*`
    of the family.

    EVERY path that removes note rows calls this with the ids it removed: the page and agent
    deletes here, a project or a task delete (the notes cascade), the account erasure and the
    demo team's cleanup. A path that deletes note rows and does not is the bug."""
    ids = sorted({int(n) for n in note_ids})
    for start in range(0, len(ids), _FORGET_BATCH):
        _forget_batch(code, ids[start : start + _FORGET_BATCH])


def _forget_batch(code: int, ids: list[int]) -> None:
    from origin.services.v3_activity import (
        SURFACE_CHAT_NOTE,
        SURFACE_PERSONAL_NOTE,
        SURFACE_TASK_NOTE,
    )

    NoteRecentMaster.objects.filter(note_type=code, note_id__in=ids).delete()
    NoteFavoriteMaster.objects.filter(note_type=code, note_id__in=ids).delete()
    surface = {
        NOTE_TYPE_PERSONAL: SURFACE_PERSONAL_NOTE,
        NOTE_TYPE_TASK: SURFACE_TASK_NOTE,
        NOTE_TYPE_CHAT: SURFACE_CHAT_NOTE,
    }[code]
    # The id is routing data the client wrote: a number, or the same number as text.
    Activity.objects.filter(
        surface_type=surface, meta__noteId__in=ids + [str(n) for n in ids]
    ).delete()


def delete_note(*, team_id, family: str, note_id) -> None:
    """Hard-delete one note of `family` ("personal", "task" or "chat")."""
    model, code = FAMILIES[family]
    with transaction.atomic():
        try:
            note = model.objects.get(team=team_id, note_id=note_id)
        except model.DoesNotExist:
            raise DeleteRefused("Note not found.", 404)
        note.delete()
        delete_note_permissions(code, note_id)
        delete_note_versions(code, note_id)
        forget_deleted_notes(code, [note_id])
    # Best-effort, after the commit: chunkers never revisit deleted rows, and the
    # orphan sweep in the reindex cron retries if this fails.
    purge_note(family, note_id)


def child_note_count(*, team_id, family: str, note_id) -> int:
    """How many notes hang directly off this one (`parent_note_id`)."""
    model, _code = FAMILIES[family]
    return model.objects.filter(team=team_id, parent_note_id=note_id).count()


def _personal_subtree(folder: PersonalNoteFolder, team_id, owner_id) -> tuple[set, set]:
    """The folder, every My Notes folder below it, and the owner's notes filed in any
    of them with their child chains. Owner-filtered: a My Notes tree is one person's."""
    folder_ids = {folder.folder_id}
    frontier = [folder.folder_id]
    while frontier:
        child_ids = list(
            PersonalNoteFolder.objects.filter(
                team=team_id,
                owner=owner_id,
                scope=PersonalNoteFolder.SCOPE_PERSONAL,
                parent_folder_id__in=frontier,
            )
            .exclude(folder_id__in=folder_ids)
            .values_list("folder_id", flat=True)
        )
        if not child_ids:
            break
        folder_ids.update(child_ids)
        frontier = child_ids

    note_ids = set(
        PersonalNoteMaster.objects.filter(
            team=team_id, owner=owner_id, folder_id__in=folder_ids
        ).values_list("note_id", flat=True)
    )
    frontier = list(note_ids)
    while frontier:
        child_notes = list(
            PersonalNoteMaster.objects.filter(
                team=team_id, owner=owner_id, parent_note_id__in=frontier
            )
            .exclude(note_id__in=note_ids)
            .values_list("note_id", flat=True)
        )
        if not child_notes:
            break
        note_ids.update(child_notes)
        frontier = child_notes
    return folder_ids, note_ids


def personal_folder_contents(*, team_id, owner_id, folder_id) -> tuple[set, set]:
    """What deleting this My Notes folder would remove: `(folder_ids, note_ids)`."""
    folder = _owned_personal_folder(team_id, owner_id, folder_id)
    return _personal_subtree(folder, team_id, owner_id)


def _owned_personal_folder(team_id, owner_id, folder_id) -> PersonalNoteFolder:
    try:
        return PersonalNoteFolder.objects.get(
            folder_id=folder_id,
            team=team_id,
            owner=owner_id,
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
        )
    except (PersonalNoteFolder.DoesNotExist, ValueError, TypeError):
        raise DeleteRefused("Folder not found.", 404)


def _hard_delete(note_ids: set, folder_ids: set) -> None:
    PersonalNoteMaster.objects.filter(note_id__in=note_ids).delete()
    NotePermissionMaster.objects.filter(note_type=NOTE_TYPE_PERSONAL, note_id__in=note_ids).delete()
    NoteVersionMaster.objects.filter(note_type=NOTE_TYPE_PERSONAL, note_id__in=note_ids).delete()
    forget_deleted_notes(NOTE_TYPE_PERSONAL, note_ids)
    # NoteFolderPermission and NoteFolderTagLink cascade on the folder FK.
    PersonalNoteFolder.objects.filter(folder_id__in=folder_ids).delete()


def delete_personal_folder(*, team_id, owner_id, folder_id) -> tuple[set, set]:
    """Delete a My Notes folder and everything under it. Returns the ids removed,
    `(folder_ids, note_ids)`, which the page uses to close tabs and drop caches."""
    with transaction.atomic():
        folder = _owned_personal_folder(team_id, owner_id, folder_id)
        folder_ids, note_ids = _personal_subtree(folder, team_id, owner_id)
        _hard_delete(note_ids, folder_ids)
    purge_notes_from_index(note_ids)
    purge_note_folders(folder_ids)
    return folder_ids, note_ids


def team_folder_contents(*, team_id, folder_id) -> tuple[set, set]:
    """What deleting this Team Notes folder would remove: `(folder_ids, note_ids)`,
    anyone's."""
    folder_ids = collect_folder_subtree_ids(folder_id, team_id)
    return folder_ids, collect_note_ids_in_folders(folder_ids, team_id)


def require_team_folder_owner(*, team_id, user_id, folder_id) -> None:
    if get_folder_role(user_id, folder_id, team_id) != ROLE_OWNER:
        raise DeleteRefused("Only the folder owner can delete it.", 403)


def team_folder_foreign_counts(*, user_id, folder_ids, note_ids) -> tuple[int, int]:
    """`(notes, folders)` in the subtree that belong to somebody else."""
    foreign_notes = (
        PersonalNoteMaster.objects.filter(note_id__in=note_ids).exclude(owner=user_id).count()
    )
    foreign_folders = (
        PersonalNoteFolder.objects.filter(folder_id__in=folder_ids).exclude(owner=user_id).count()
    )
    return foreign_notes, foreign_folders


def delete_team_folder(*, team_id, user_id, folder: PersonalNoteFolder) -> tuple[set, set]:
    """Delete a Team Notes folder (already resolved by the caller). Only its Owner may,
    and not while anyone else's note or folder is inside."""
    require_team_folder_owner(team_id=team_id, user_id=user_id, folder_id=folder.folder_id)

    folder_ids, note_ids = team_folder_contents(team_id=team_id, folder_id=folder.folder_id)
    foreign_notes, foreign_folders = team_folder_foreign_counts(
        user_id=user_id, folder_ids=folder_ids, note_ids=note_ids
    )
    if foreign_notes or foreign_folders:
        raise DeleteRefused(
            "This folder still holds content owned by other people.",
            409,
            {"foreignNoteCount": foreign_notes, "foreignFolderCount": foreign_folders},
        )

    with transaction.atomic():
        _hard_delete(note_ids, folder_ids)
    purge_notes_from_index(note_ids)
    purge_note_folders(folder_ids)
    return folder_ids, note_ids
