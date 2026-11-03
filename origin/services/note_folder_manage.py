"""Create and rename note folders: one set of rules for the pages' views and the
agent's tools (MIP-883).

Both note spaces keep their folders in `PersonalNoteFolder`, told apart by `scope`:
My Notes folders belong to one person; Team Notes folders carry the access rules for
every note filed in them. The views in `personal_note_folder_views` and
`team_note_folder_views` call this module, and so does `manage_note_folder`; each
caller keeps its own authentication and response shape, and every refusal here is a
`FolderRefused` carrying the message and status the pages already return.

A Team Notes folder is created by the page with a roster, groups and tags; those
stay in the view and run through `after_create`, inside the same transaction.
Moving a folder, changing its visibility and deleting it are not here.
"""

from __future__ import annotations

from collections.abc import Callable

from django.db import transaction

from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.views.utils.note_folder_role import get_folder_role, is_team_member
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER

NAME_MAX = 255
VALID_VISIBILITIES = {
    PersonalNoteFolder.VISIBILITY_PUBLIC,
    PersonalNoteFolder.VISIBILITY_PRIVATE,
}


class FolderRefused(Exception):
    """A folder change the rules do not allow. `status` is the HTTP code the page
    answers with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def clean_name(raw) -> str:
    name = "" if raw is None else str(raw).strip()
    if name == "" or len(name) > NAME_MAX:
        raise FolderRefused("Folder name must be 1-255 characters.", 400)
    return name


def _can_write(role_id) -> bool:
    return role_id is not None and role_id <= ROLE_EDITOR


def create_personal_folder(*, team_id, owner_id, name, parent_folder_id=None) -> PersonalNoteFolder:
    """A My Notes folder, at the top or inside one of the owner's own."""
    name = clean_name(name)
    if (
        parent_folder_id is not None
        and not PersonalNoteFolder.objects.filter(
            folder_id=parent_folder_id,
            team=team_id,
            owner=owner_id,
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
        ).exists()
    ):
        raise FolderRefused("Parent folder not found.", 400)
    return PersonalNoteFolder.objects.create(
        team_id=team_id,
        owner_id=owner_id,
        parent_folder_id=parent_folder_id,
        name=name,
        scope=PersonalNoteFolder.SCOPE_PERSONAL,
    )


def rename_personal_folder(*, team_id, owner_id, folder_id, name) -> PersonalNoteFolder:
    folder = PersonalNoteFolder.objects.filter(
        folder_id=folder_id,
        team=team_id,
        owner=owner_id,
        scope=PersonalNoteFolder.SCOPE_PERSONAL,
    ).first()
    if folder is None:
        raise FolderRefused("Folder not found.", 404)
    folder.name = clean_name(name)
    folder.save(update_fields=["name", "ts_updated_at"])
    return folder


def create_team_folder(
    *,
    team_id,
    user_id,
    name,
    parent_folder_id=None,
    visibility=None,
    after_create: Callable[[PersonalNoteFolder, str, bool], None] | None = None,
) -> tuple[PersonalNoteFolder, str]:
    """A Team Notes folder. Returns `(folder, owning_team_id)`.

    A top-level folder must state its visibility (it has no ancestor to inherit
    from); a subfolder defaults to inheriting, and an explicit visibility is how it
    narrows. The creator always gets an explicit Owner row. `after_create(folder,
    owning_team_id, is_host_member)` runs in the same transaction, for what only the
    page adds: the roster, groups and tags.
    """
    name = clean_name(name)

    # For a SUBFOLDER the team is the parent's, not the team the caller happens to be
    # viewing: a folder shared with you belongs to the host, and a child filed under
    # the viewer's team would sit in a tree neither side can see.
    owning_team_id = team_id
    if parent_folder_id is not None:
        parent_team_id = (
            PersonalNoteFolder.objects.filter(
                folder_id=parent_folder_id, scope=PersonalNoteFolder.SCOPE_TEAM
            )
            .values_list("team_id", flat=True)
            .first()
        )
        if parent_team_id is None:
            raise FolderRefused("Parent folder not found.", 400)
        owning_team_id = str(parent_team_id)

    # Team membership gates a ROOT folder only. A subfolder is authorized by the role
    # on its parent, which is what lets an editor admitted through a share organize
    # the work inside it.
    is_host_member = is_team_member(owning_team_id, user_id)
    if parent_folder_id is None:
        if not is_host_member:
            raise FolderRefused("You are not a member of this team.", 403)
        if visibility not in VALID_VISIBILITIES:
            raise FolderRefused('A top-level folder must be "public" or "private".', 400)
    else:
        if visibility is not None and visibility not in VALID_VISIBILITIES:
            raise FolderRefused('visibility must be "public", "private", or null to inherit.', 400)
        if not _can_write(get_folder_role(user_id, parent_folder_id, owning_team_id)):
            raise FolderRefused("You do not have permission to add folders here.", 403)
        if not is_host_member:
            # An outsider's subfolder inherits and grants nobody: "public" would hand
            # every member of a company they don't work for a folder inside their own
            # share.
            visibility = None

    with transaction.atomic():
        folder = PersonalNoteFolder.objects.create(
            team_id=owning_team_id,
            owner_id=user_id,
            parent_folder_id=parent_folder_id,
            name=name,
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        # The creator's OWNER row is written explicitly rather than inferred, so the
        # roster and member count tell the truth and ownership can later be
        # transferred like any other grant.
        NoteFolderPermission.objects.create(
            team_id=owning_team_id,
            folder=folder,
            user_id=user_id,
            role_id=ROLE_OWNER,
            granted_by_id=user_id,
        )
        if after_create is not None:
            after_create(folder, owning_team_id, is_host_member)
    return folder, owning_team_id


def rename_team_folder(*, team_id, user_id, folder_id, name) -> PersonalNoteFolder:
    """Rename a Team Notes folder of this team: Editor or stronger on it, and a member
    of the team. (The page also lets an outsider rename a subfolder they made inside a
    share; that stays on the page.)"""
    folder = PersonalNoteFolder.objects.filter(
        folder_id=folder_id, team=team_id, scope=PersonalNoteFolder.SCOPE_TEAM
    ).first()
    if folder is None:
        raise FolderRefused("Folder not found.", 404)
    if not _can_write(get_folder_role(user_id, folder.folder_id, team_id)):
        raise FolderRefused("You do not have permission to modify this folder.", 403)
    if not is_team_member(team_id, user_id):
        raise FolderRefused("Only members of the owning team can manage this folder.", 403)
    folder.name = clean_name(name)
    folder.save(update_fields=["name", "ts_updated_at"])
    return folder
