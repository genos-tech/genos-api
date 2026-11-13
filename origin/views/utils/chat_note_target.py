"""Where a new chat note may go, and who gets which role on it.

Shared by the REST create (`ChatNoteMasterView.post`) and the agent / MCP
`create_note` tool, so neither can file a note into a conversation the
caller isn't part of (MIP-838: the REST view used to accept any channel
UUID and stored the client's `chat_type` as given).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.services.channel_scope import chat_member_q
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER


class ChatNoteTargetNotFound(Exception):
    """The channel (or thread) doesn't exist for this caller.

    One error for "no such channel", "not your team's channel" and "you
    aren't a member", so a caller can't probe which channel UUIDs exist.
    """


@dataclass(frozen=True)
class ChatNoteTarget:
    channel: Channel
    chat_type: int
    thread_root_id: str | None


def resolve_chat_note_target(*, user_id, channel_id, thread_root_id=None) -> ChatNoteTarget:
    """The channel a new chat note attaches to, checked for `user_id`.

    Requires a live channel the caller is a member of (`chat_member_q`);
    a guest in a channel shared from another team is a member too, so the
    channel's team isn't compared with the caller's. A thread note's root
    must be a message in that same channel. The note's `chat_type` is the
    channel's kind, never a value the client sent.
    """
    try:
        channel = Channel.objects.get(id=channel_id, is_deleted=False)
    except (Channel.DoesNotExist, ValidationError, ValueError, TypeError):
        raise ChatNoteTargetNotFound("Chat not found.")
    # a member by the one rule (`chat_member_q`): a project chat is the project's people, and a
    # stray channel-member row on it is nobody (MIP-1474)
    if not Channel.objects.filter(chat_member_q(user_id), id=channel.id).exists():
        raise ChatNoteTargetNotFound("Chat not found.")

    root = None
    if thread_root_id:
        try:
            exists = Message.objects.filter(id=thread_root_id, channel_id=channel.id).exists()
        except (ValidationError, ValueError, TypeError):
            exists = False
        if not exists:
            raise ChatNoteTargetNotFound("Thread not found.")
        root = str(thread_root_id)

    return ChatNoteTarget(channel=channel, chat_type=int(channel.kind), thread_root_id=root)


def chat_note_roles(*, channel: Channel, creator_id) -> dict[str, int]:
    """`{user_id: role_id}` for a new note on `channel`.

    A chat note is shared per channel (every member sees it in the notes
    panel), so a creator-only OWNER row left every other member at the
    implicit fallback, which 403'd the moment they edited it. By kind:
      DM / MDM: every active member is an OWNER.
      GM:       the GM owner is OWNER; every other active member is an EDITOR.
      other (e.g. PM): only the creator is an OWNER.
    The creator always gets at least an OWNER row, except a GM creator who
    isn't the GM owner, who keeps EDITOR.
    """
    member_ids = list(
        ChannelMember.objects.filter(channel_id=channel.id, is_deleted=False).values_list(
            "user_id", flat=True
        )
    )
    roles: dict[str, int] = {}
    if channel.kind in (ChannelKind.DM, ChannelKind.MDM):
        for uid in member_ids:
            roles[str(uid)] = ROLE_OWNER
    elif channel.kind == ChannelKind.GM:
        owner_id = channel.owner_id
        if owner_id is None:
            # Legacy-bridged GMs may have a null Channel.owner; fall back to
            # the member flagged role="owner".
            owner_id = (
                ChannelMember.objects.filter(channel_id=channel.id, role="owner", is_deleted=False)
                .values_list("user_id", flat=True)
                .first()
            )
        for uid in member_ids:
            roles[str(uid)] = ROLE_OWNER if str(uid) == str(owner_id) else ROLE_EDITOR
    roles.setdefault(str(creator_id), ROLE_OWNER)
    return roles
