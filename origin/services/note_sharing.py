"""Sharing a note with one teammate, or taking the share back: the page's rules and the
agent's (MIP-883).

A share is a `NotePermissionMaster` row. Only the note's Owner (an explicit Owner row,
which is how the app records the author) may grant or revoke; the person must be on the
team; the role is Editor or Viewer, never a second Owner; and nobody changes their own
role. Neither path notifies the person.

Taking a share back narrows who may read the note, so the note's search chunks are
purged afterwards (best-effort, the reindex sweep retries): the index stamps a note's
readers when it is built, and without this the person could still find it in search.

The granter's own ledger is told (MIP-1085): `note_share` (a grant, or a change of the
role) or `note_unshare`, with the person's id and the note's family and id, never its
title. The note's owner is the only one who can, so it is always the member's own change.
"""

from __future__ import annotations

from django.db import transaction

from origin.models.common.member_event_models import (
    KIND_NOTE_SHARE,
    KIND_NOTE_UNSHARE,
    OBJECT_NOTE,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.common_note_models import NotePermissionMaster
from origin.search_engine.purge import purge_note
from origin.services import member_events
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


def _note(kind: str, granter_id, team_id, note_type: int, note_id: int, target_user_id) -> None:
    """Tell the granter's ledger (never raises). The note's id carries its family, as
    everywhere else in the ledger: `personal:12`."""
    family = _LABEL_BY_CODE.get(note_type)
    if family is None:
        return
    member_events.record_activity(
        user_id=str(granter_id),
        team_id=team_id,
        kind=kind,
        items=[(OBJECT_NOTE, f"{family}:{note_id}", str(target_user_id))],
    )


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
        _note(KIND_NOTE_SHARE, granter_id, team_id, note_type, note_id, target_user_id)
    return permission


def unshare_note(*, granter_id, note_type: int, note_id: int, target_user_id) -> None:
    """Take the person's explicit role away."""
    if get_explicit_role(granter_id, note_type, note_id) != ROLE_OWNER:
        raise SharingRefused("Only the note owner can revoke roles.", 403)
    if str(target_user_id) == str(granter_id):
        raise SharingRefused("Owners cannot revoke themselves.", 400)
    grants = NotePermissionMaster.objects.filter(
        user=target_user_id, note_type=note_type, note_id=note_id
    )
    team_id = None
    if member_events.recording_activity_for(granter_id):
        team_id = grants.values_list("team_id", flat=True).first()
    with transaction.atomic():
        deleted, _ = grants.delete()
        if deleted == 0:
            raise SharingRefused("No matching role found.", 404)
        _note(KIND_NOTE_UNSHARE, granter_id, team_id, note_type, note_id, target_user_id)
    label = _LABEL_BY_CODE.get(note_type)
    if label is not None:
        purge_note(label, note_id)
