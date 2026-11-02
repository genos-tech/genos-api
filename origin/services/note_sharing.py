"""Sharing a note with one teammate, or taking the share back: the page's rules and the
agent's (MIP-883).

A share is a `NotePermissionMaster` row. Only the note's Owner (an explicit Owner row,
which is how the app records the author) may grant or revoke; the person must be on the
team; the role is Editor or Viewer, never a second Owner; and nobody changes their own
role. Neither path notifies the person.

Taking a share back narrows who may read the note, so the note's search chunks are
purged afterwards (best-effort, the reindex sweep retries): the index stamps a note's
readers when it is built, and without this the person could still find it in search.
"""

from __future__ import annotations

from django.db import transaction

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.common_note_models import NotePermissionMaster
from origin.search_engine.purge import purge_note
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_OWNER,
    get_explicit_role,
)

_LABEL_BY_CODE = {NOTE_TYPE_PERSONAL: "personal", NOTE_TYPE_TASK: "task", NOTE_TYPE_CHAT: "chat"}


class SharingRefused(Exception):
    """A share change the rules do not allow. `status` is the HTTP code the page answers
    with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def share_note(
    *, team_id, granter_id, note_type: int, note_id: int, target_user_id, role_id: int
) -> NotePermissionMaster:
    """Give `target_user_id` the role on the note, or change the one they have."""
    if get_explicit_role(granter_id, note_type, note_id) != ROLE_OWNER:
        raise SharingRefused("Only the note owner can grant roles.", 403)
    if str(target_user_id) == str(granter_id):
        raise SharingRefused("You cannot change your own role.", 400)
    if not TeamMembers.objects.filter(
        team=team_id, attendee=target_user_id, is_deleted=False
    ).exists():
        raise SharingRefused("Target user is not in the same team.", 400)
    # A second Owner would make two people who can each take the other's access away.
    if role_id == ROLE_OWNER:
        raise SharingRefused("Owner transfer is not supported in this version.", 400)
    try:
        team = TeamMaster.objects.get(team_id=team_id)
        target = CustomUser.objects.get(id=target_user_id)
    except (TeamMaster.DoesNotExist, CustomUser.DoesNotExist):
        raise SharingRefused("Team or target user not found.", 404)
    with transaction.atomic():
        permission, _ = NotePermissionMaster.objects.update_or_create(
            user=target,
            note_type=note_type,
            note_id=note_id,
            defaults={"team": team, "role_id": role_id},
        )
    return permission


def unshare_note(*, granter_id, note_type: int, note_id: int, target_user_id) -> None:
    """Take the person's explicit role away."""
    if get_explicit_role(granter_id, note_type, note_id) != ROLE_OWNER:
        raise SharingRefused("Only the note owner can revoke roles.", 403)
    if str(target_user_id) == str(granter_id):
        raise SharingRefused("Owners cannot revoke themselves.", 400)
    deleted, _ = NotePermissionMaster.objects.filter(
        user=target_user_id, note_type=note_type, note_id=note_id
    ).delete()
    if deleted == 0:
        raise SharingRefused("No matching role found.", 404)
    label = _LABEL_BY_CODE.get(note_type)
    if label is not None:
        purge_note(label, note_id)
