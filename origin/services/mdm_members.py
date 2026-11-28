"""The member limit of a multi-person DM (MIP-2549).

A multi-person DM (`ChannelKind.MDM`) holds at most `MDM_MAX_MEMBERS` people, the creator
included. A group chat has no limit. What counts is who is in the chat NOW
(`chat_member_rows`: live rows of live accounts), so a person who left or whose account was
deleted takes no seat. A chat already above the limit keeps everyone; it just takes nobody new.

Every path that writes a member into an MDM asks here: create and add. A cross-team grant cannot
name an MDM at all (MIP-2566).
"""

from __future__ import annotations

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.services.channel_scope import chat_member_rows

MDM_MAX_MEMBERS = 10
MDM_LIMIT_CODE = "mdm_member_limit"
MDM_LIMIT_MESSAGE = (
    f"A multi-person DM can have at most {MDM_MAX_MEMBERS} members. "
    "Use a group chat for more people."
)


def has_room_for(channel: Channel, user_ids) -> bool:
    """May `user_ids` be added to `channel`? True for every kind but a full MDM.

    Adding somebody already in the chat takes no seat, so it is always allowed. Call this inside
    the transaction that writes the rows: it locks the channel row, so two adds that arrive
    together are counted one after the other and cannot both take the last seat.
    """
    if channel.kind != ChannelKind.MDM:
        return True
    Channel.objects.select_for_update().filter(id=channel.id).values_list("id").first()
    present = {str(u) for u in chat_member_rows(channel).values_list("user_id", flat=True)}
    new = {str(u) for u in user_ids} - present
    return not new or len(present) + len(new) <= MDM_MAX_MEMBERS
