"""Moving a personal-family note between folders (My Notes and Team Notes).

One implementation for the REST move (`PersonalNoteMoveView`) and the
agent / MCP `update_note` tool, so a folder move can't be allowed on one
surface and refused on the other, and narrowing a note's access always
purges it from search.
"""

from __future__ import annotations

from dataclasses import dataclass

from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.search_engine.chunkers.note_chunker import refresh_personal_notes
from origin.services import change_feed
from origin.views.utils.note_acl_resync import collect_note_subtree_ids, purge_notes_from_index
from origin.views.utils.note_folder_role import get_folder_role
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_VIEWER


class NoteMoveRefused(Exception):
    """The move isn't allowed. `status` is the HTTP code the REST view sends."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def is_team_folder(folder_id, team_id) -> bool:
    if folder_id is None:
        return False
    return PersonalNoteFolder.objects.filter(
        folder_id=folder_id, team=team_id, scope=PersonalNoteFolder.SCOPE_TEAM
    ).exists()


def owns_personal_folder(folder_id, team_id, owner_id) -> bool:
    return PersonalNoteFolder.objects.filter(
        folder_id=folder_id, team=team_id, owner=owner_id, scope=PersonalNoteFolder.SCOPE_PERSONAL
    ).exists()


@dataclass(frozen=True)
class MovePlan:
    target_folder_id: int | None
    leaves_team_space: bool


def authorize_personal_note_move(
    *, user_id, team_id, note: PersonalNoteMaster, target_folder_id
) -> MovePlan:
    """Check that `user_id` may file `note` into `target_folder_id` (None =
    top level of My Notes); raises `NoteMoveRefused` if not.

    Moving INTO the team space: any editor of the destination may file a
    note there, and an editor of the source team folder may rearrange it —
    the sidebar being rearranged is the team's, not a colleague's private
    one, so the owner-only rule that guards personal folders doesn't apply.

    Moving OUT of the team space (to a personal folder or to the top level)
    is owner-only even for a team-folder editor: it would pull shared
    content into one person's private sidebar.
    """
    is_owner = str(note.owner_id) == str(user_id)
    source_team_role = (
        get_folder_role(user_id, note.folder_id, team_id)
        if is_team_folder(note.folder_id, team_id)
        else None
    )
    target_is_team = is_team_folder(target_folder_id, team_id)

    if target_is_team:
        if not is_owner and (source_team_role is None or source_team_role > ROLE_EDITOR):
            raise NoteMoveRefused("You do not have permission to move this note.", 403)
        if (get_folder_role(user_id, target_folder_id, team_id) or ROLE_VIEWER) > ROLE_EDITOR:
            raise NoteMoveRefused("You do not have permission to add notes to that folder.", 403)
    else:
        if not is_owner:
            raise NoteMoveRefused("Only the note owner can move it.", 403)
        if target_folder_id is not None and not owns_personal_folder(
            target_folder_id, team_id, user_id
        ):
            raise NoteMoveRefused("Target folder not found.", 400)

    return MovePlan(
        target_folder_id=target_folder_id,
        leaves_team_space=source_team_role is not None and not target_is_team,
    )


def apply_personal_note_move(*, team_id, note: PersonalNoteMaster, plan: MovePlan) -> None:
    """Carry out a move `authorize_personal_note_move` allowed.

    The note is re-rooted (`parent_note_id=None`): folders own ROOT-level
    notes, and the note's own child subtree rides along implicitly.
    """
    # Queryset .update() on purpose: skips auto_now so the move neither
    # reshuffles the -tsUpdated sidebar ordering nor routes the note into
    # the incremental reindex window (folder membership isn't indexed).
    PersonalNoteMaster.objects.filter(note_id=note.note_id).update(
        folder_id=plan.target_folder_id,
        parent_note_id=None,
    )
    note.folder_id = plan.target_folder_id
    note.parent_note_id = None
    # The `.update()` fires no `post_save`, so the change feed is told by hand
    # (MIP-970): the note moves between folders (My Notes / Team Notes), which
    # is exactly what `notes_personal` carries.
    change_feed.bump_notes(team_id, personal=True)
    change_feed.bump_notes(note.team_id, personal=True)

    # Crossing the team boundary CHANGES the note's ACL, which the
    # `.update()` above deliberately hides from the reindexer. Moving out
    # of the team space narrows access, so purge now rather than leave the
    # note findable by the team for another reindex cycle. Moving in only
    # broadens — the widened window handles it.
    if plan.leaves_team_space:
        purge_notes_from_index(collect_note_subtree_ids(note.note_id, team_id))

    # The folder path is part of the note's index entry now (MIP-839), and
    # the `.update()` above hides the move from the incremental window.
    # After the purge: a note that just left Team Notes is re-added with
    # its new, narrower access.
    refresh_personal_notes(collect_note_subtree_ids(note.note_id, team_id))


def move_personal_note(*, user_id, team_id, note: PersonalNoteMaster, target_folder_id) -> None:
    """Authorize and apply in one step (the REST move)."""
    plan = authorize_personal_note_move(
        user_id=user_id, team_id=team_id, note=note, target_folder_id=target_folder_id
    )
    apply_personal_note_move(team_id=team_id, note=note, plan=plan)
