"""Putting back a group chat that its owner deleted softly (MIP-1715).

`delete_group_chat` (MIP-1705) soft-deletes by setting `Channel.is_deleted` and touching nothing
else, so restoring is the same flag back: the chat, its members, messages, reactions, files, pins
and read cursors return exactly as they were, because they were never moved.

Decisions, all the safe reading:

* **Who**: strictly the chat's OWNER (`Channel.owner_id`), and only while they are still an active,
  non-guest member of the chat's team. A plain member or an editor of the chat cannot (the delete
  was the owner's alone, so is the undo), and a team owner cannot either: a team owner is not
  entitled to read a private group chat they were never in, and restoring would hand them its
  roster and history. The one gap this leaves is an owner who has left the team: nobody can
  restore that chat from the app, and it stays hidden (which is safe) until an operator acts.
* **How long**: forever. No retention policy in `services/retention.py` covers soft-deleted chats
  and none is invented here.
* **What does not come back**: the access the chat's membership had written on its notes.
  `delete_group_chat` revoked it (`revoke_membership_roles`) and the rows are gone, so a member
  sees the chat's notes again only if someone shares them again. An explicit share the owner made
  stays, as it always did.
* **Search**: the delete purged the chat's search chunks. Restoring marks the channel for the
  incremental reindexer (`acl_dirty`), which re-chunks the whole chat on its next pass, so the
  messages become searchable again at the next `opensearch_reindex` run (not instantly).
* **Live**: nothing tells other members' open apps that the chat is back (no existing socket
  notice means "a chat appeared"; the app's own `channel.created` is emitted by its socket
  handler, not by this API). The restoring device refreshes its own list, and everyone else sees
  the chat at their next chat-list refresh (reload, reconnect or refocus).

A hard-deleted chat has no row, so it is simply not found; a chat that was never deleted is not
restorable either (404, same as the list, so a double click is harmless: the second request
changes nothing).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Count, Exists, OuterRef, QuerySet
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.search_engine.acl_dirty import mark_channels_dirty
from origin.services.member_roles import GUEST

logger = logging.getLogger(__name__)


def restorable_channels_qs(user) -> QuerySet[Channel]:
    """The soft-deleted group chats `user` may restore: theirs, in a team they still belong to.

    The ownership check (`owner_id`, the FK, never a member row's `role` column) and the team
    check (an active, non-guest `TeamMembers` row on a live team) are the whole gate, so the list
    and the restore cannot disagree about who may.
    """
    in_team = TeamMembers.objects.filter(
        team_id=OuterRef("team_id"),
        attendee_id=user.id,
        is_deleted=False,
        team__is_deleted=False,
    ).exclude(member_role=GUEST)
    return Channel.objects.filter(
        kind=ChannelKind.GM, is_deleted=True, owner_id=user.id, team_id__isnull=False
    ).filter(Exists(in_team))


def member_counts(channel_ids) -> dict:
    """Live members per channel, for the list."""
    rows = (
        ChannelMember.objects.filter(channel_id__in=list(channel_ids), is_deleted=False)
        .values("channel_id")
        .annotate(n=Count("id"))
    )
    return {r["channel_id"]: r["n"] for r in rows}


def restore_group_chat(channel: Channel) -> bool:
    """Put one soft-deleted group chat back. True if this call restored it, False if it was
    not deleted (already restored by another request, or never deleted).

    The caller proves who may (`restorable_channels_qs`). Atomic, with the flag flipped by a
    conditional `update`, so two concurrent restores restore once and the loser changes nothing.
    `update`, not `save`: nothing else on the row may change, and `ts_updated_at` is stamped by
    hand because `update` skips `auto_now` (the delete did the same).
    """
    with transaction.atomic():
        changed = Channel.objects.filter(
            pk=channel.pk, kind=ChannelKind.GM, is_deleted=True
        ).update(is_deleted=False, ts_updated_at=timezone.now())
        if not changed:
            return False
        # Best-effort and in the same transaction, so a rollback leaves no stale mark.
        mark_channels_dirty([channel.pk])
    logger.info("group chat restored channel=%s team=%s", channel.pk, channel.team_id)
    return True
