"""Starring notes and note folders: the page's rule and the agent's (MIP-883).

A favorite is a private bookmark, so it is the least risky thing a tool can do to a
note, but the sidebar prints the favorite's title: only something the caller can open
may be starred, or a bookmark would leak the name of a note or folder they can't see.
"""

from __future__ import annotations

from origin.models.note.favorite_note_models import NoteFavoriteMaster, NoteFolderFavorite
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.views.utils.note_folder_role import get_folder_role


def can_see_folder(user_id, folder: PersonalNoteFolder) -> bool:
    """A My Notes folder is its owner's alone; a Team Notes folder, including one
    another team shared, is visible to whoever its chain gives a role, resolved in the
    folder's own team."""
    if folder.scope == PersonalNoteFolder.SCOPE_TEAM:
        return get_folder_role(user_id, folder.folder_id, folder.team_id) is not None
    return str(folder.owner_id) == str(user_id)


def star_folder(*, team_id, user_id, folder: PersonalNoteFolder) -> bool:
    """True when it was newly starred, False when it already was."""
    _fav, created = NoteFolderFavorite.objects.get_or_create(
        user_id=user_id, folder=folder, defaults={"team_id": team_id}
    )
    return created


def unstar_folder(*, user_id, folder_id) -> bool:
    """True when a star was removed, False when there was none."""
    deleted, _ = NoteFolderFavorite.objects.filter(user_id=user_id, folder_id=folder_id).delete()
    return bool(deleted)


def star_note(*, team_id, user_id, note_type: int, note_id: int) -> bool:
    """True when newly starred. The identity of a favorite is the person and the note
    (the table's unique key), not the team the page happened to be showing."""
    _fav, created = NoteFavoriteMaster.objects.get_or_create(
        user_id=user_id, note_type=note_type, note_id=note_id, defaults={"team_id": team_id}
    )
    return created


def unstar_note(*, user_id, note_type: int, note_id: int) -> bool:
    deleted, _ = NoteFavoriteMaster.objects.filter(
        user_id=user_id, note_type=note_type, note_id=note_id
    ).delete()
    return bool(deleted)
