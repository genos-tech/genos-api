"""Ending the roles a chat note's membership wrote, when the membership ends (MIP-1482).

`chat_note_roles` writes an explicit `NotePermissionMaster` row for the people in a chat when a
chat note is created, and `get_explicit_role` honours it ahead of every membership check. Leaving
the chat (or the project whose chat it is, or the team) never took the row back, and neither did a
move of the note to another chat, so a person who had left kept Editor or Owner on the note.

The table has no column that says who wrote a row, and the same row shape is also what the owner
writes on purpose to share a note with someone OUTSIDE its chat (`note_sharing.share_note`). That
share is the note's own ACL and must outlive a membership change. The two are told apart by when
the row was written relative to the person's membership of the chat:

  * written while the person was a member (`row.ts_created_at >= membership.ts_joined_at`): it
    came from membership (the creation fan-out, or the owner changing the role of someone who
    was in the chat), so it ends with the membership;
  * written before the person joined, or when they never were a member: a share, kept.

A person who leaves the TEAM loses every chat-note row inside it, shares included: a share can
only go to a team member, and nobody outside the team holds access to its notes.

Only chat notes: task notes and personal notes in team folders have the same shape of problem and
are separate work.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from origin.models.chat.unified_models import ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMembers

log = logging.getLogger(__name__)

NOTE_TYPE_CHAT = 3


def _mark_search(channel_ids) -> None:
    """The note chunks' ACL is stamped at index time: queue a refresh (best effort)."""
    from origin.search_engine.acl_dirty import mark_channels_dirty

    mark_channels_dirty(channel_ids)


def revoke_membership_roles(*, user_id, channel_id, joined_at=None) -> int:
    """Delete `user_id`'s membership-derived roles on the chat notes of `channel_id`.

    Call it when the person's membership of that chat ends. `joined_at` is when that membership
    began (`ChannelMember.ts_joined_at` / `ProjectMembers.ts_joined_at`); rows written before it
    are shares and stay. `None` revokes every row the person holds on the chat's notes.
    Returns the number of rows deleted.
    """
    if not user_id or not channel_id:
        return 0
    rows = NotePermissionMaster.objects.filter(
        user_id=user_id,
        note_type=NOTE_TYPE_CHAT,
        note_id__in=ChatNoteMaster.objects.filter(channel_id=channel_id).values("note_id"),
    )
    if joined_at is not None:
        rows = rows.filter(ts_created_at__gte=joined_at)
    deleted, _ = rows.delete()
    if deleted:
        _mark_search([channel_id])
    return deleted


def revoke_team_roles(*, user_id, team_id) -> int:
    """Delete every role `user_id` holds on a chat note of `team_id` (they left the team)."""
    if not user_id or not team_id:
        return 0
    notes = ChatNoteMaster.objects.filter(team_id=team_id)
    channel_ids = list(
        notes.filter(
            note_id__in=NotePermissionMaster.objects.filter(
                user_id=user_id, note_type=NOTE_TYPE_CHAT
            ).values("note_id"),
            channel_id__isnull=False,
        )
        .values_list("channel_id", flat=True)
        .distinct()
    )
    deleted, _ = NotePermissionMaster.objects.filter(
        user_id=user_id,
        note_type=NOTE_TYPE_CHAT,
        note_id__in=notes.values("note_id"),
    ).delete()
    if deleted:
        _mark_search(channel_ids)
    return deleted


def _joined_at_by_user(channel) -> dict[str, object]:
    """`{user_id: when they joined}` for the live members of `channel`, by `chat_member_q`'s rule:
    a project chat is the project's people, any other chat its live member rows."""
    if channel.kind == ChannelKind.PM:
        pairs = ProjectMembers.objects.filter(
            project_id=channel.project_id, attendee_id__isnull=False
        ).values_list("attendee_id", "ts_joined_at")
    else:
        pairs = ChannelMember.objects.filter(channel=channel, is_deleted=False).values_list(
            "user_id", "ts_joined_at"
        )
    return {str(uid): joined for uid, joined in pairs}


def rederive_on_move(*, moves, target) -> int:
    """A chat note moved to `target`: end the roles the OLD chat's membership wrote.

    `moves` is `[(note_id, old_channel, owner_id), ...]`, read before the notes were re-filed
    (the note and its descendants). A row is dropped when its holder was a member of the old chat
    and the row was written while they were (`revoke_membership_roles`'s rule); shares stay. The
    note's author keeps their row while they are a member of `target`, so the owner of a note is
    not left without one just because it moved to a chat they are in. Everyone else in `target`
    reads the note through the chat (`chat_note_member_q`), which needs no row.
    Returns the number of rows deleted.
    """
    deleted_total = 0
    joined_cache: dict = {}
    target_joined = None
    touched = {str(target.id)}
    with transaction.atomic():
        for note_id, old_channel, owner_id in moves:
            if old_channel is None or old_channel.id == target.id:
                continue
            joined = joined_cache.get(old_channel.id)
            if joined is None:
                joined = joined_cache[old_channel.id] = _joined_at_by_user(old_channel)
            if target_joined is None:
                target_joined = _joined_at_by_user(target)
            touched.add(str(old_channel.id))
            for row in NotePermissionMaster.objects.filter(
                note_type=NOTE_TYPE_CHAT, note_id=note_id
            ):
                since = joined.get(str(row.user_id))
                if since is None or row.ts_created_at < since:
                    continue  # not a member of the old chat, or a share: kept
                if owner_id is not None and str(row.user_id) == str(owner_id):
                    if str(row.user_id) in target_joined:
                        # The author, still in the chat the note now lives in: the row now
                        # stands on THAT membership, so it is dated from the move (a row older
                        # than their joining the new chat would read as a share when they leave).
                        NotePermissionMaster.objects.filter(pk=row.pk).update(
                            ts_created_at=timezone.now()
                        )
                        continue
                row.delete()
                deleted_total += 1
    if deleted_total:
        _mark_search(touched)
    return deleted_total
