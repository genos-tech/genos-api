"""
Bridge from legacy chat identity → the v3 unified schema.

The RAG / search-engine and chat-note code still references chats as the
legacy `(chat_type, chat_id[, thread_id])` integer tuple — the frontend's
`ThreadContext` is all `number`, and the OpenSearch index / AgentSession /
ThreadSummary / ChatNoteMaster rows store the same legacy ints. Rather
than migrate all of that to v3 UUIDs, we resolve the legacy ints to the
v3 `Channel` via the `Channel.legacy_chat_id` bridge column and answer
membership / message / thread queries off the unified schema.

This lets the legacy per-type chat models (DMMaster / GMMembers / DMMessages
/ …) be deleted while the int-keyed callers stay unchanged.

chat_type codes: 1=DM, 2=GM, 3=PM, 4=MDM (mirror `chunkers.base.CHAT_TYPE_*`).
PM keeps using `ProjectMembers` (the membership source of truth — not a
legacy chat model).
"""

from __future__ import annotations

from typing import Optional

from origin.models.chat.unified_models import Channel, ChannelMember, Message
from origin.models.project.prj_models import ProjectMembers

_PM = 3  # CHAT_TYPE_PM


def resolve_channel(chat_type_code: int, chat_id) -> Optional[Channel]:
    """Resolve a legacy `(chat_type, chat_id)` to its v3 `Channel`.

    Every backfilled legacy channel carries `legacy_chat_id`; PM channels
    carry the project id there (see `pm_channel_signals`). Returns None
    when no v3 channel bridges the legacy id (e.g. a legacy chat that was
    never backfilled, or a non-numeric id).
    """
    try:
        legacy_id = int(chat_id)
    except (TypeError, ValueError):
        return None
    return Channel.objects.filter(
        kind=chat_type_code, legacy_chat_id=legacy_id, is_deleted=False
    ).first()


def chat_member_user_ids(chat_type_code: int, chat_id) -> set[str]:
    """Active member user ids (str) for a legacy chat reference, via v3.

    DM/GM/MDM resolve through the v3 `Channel` + `ChannelMember`. PM keeps
    using `ProjectMembers`. Empty set when the channel can't be resolved.
    """
    if chat_type_code == _PM:
        return {
            str(uid)
            for uid in ProjectMembers.objects.filter(project_id=chat_id).values_list(
                "attendee_id", flat=True
            )
            if uid
        }
    channel = resolve_channel(chat_type_code, chat_id)
    if channel is None:
        return set()
    return {
        str(uid)
        for uid in ChannelMember.objects.filter(
            channel=channel, is_deleted=False
        ).values_list("user_id", flat=True)
        if uid
    }


def is_chat_member(chat_type_code: int, chat_id, user_id) -> bool:
    """Whether `user_id` is an active member of the legacy chat reference."""
    if chat_type_code == _PM:
        return ProjectMembers.objects.filter(
            project_id=chat_id, attendee_id=user_id
        ).exists()
    channel = resolve_channel(chat_type_code, chat_id)
    if channel is None:
        return False
    return ChannelMember.objects.filter(
        channel=channel, user_id=user_id, is_deleted=False
    ).exists()


def member_ids_by_chat(chat_refs) -> dict[tuple[int, int], list[str]]:
    """Batch form of `chat_member_user_ids` for the chunkers.

    `chat_refs` is an iterable of `(chat_type, chat_id)` tuples. Returns a
    dict mapping each (chat_type, chat_id) to its active member user-id
    list. Used by `note_chunker` to resolve ACLs for many notes at once.
    """
    out: dict[tuple[int, int], list[str]] = {}
    for chat_type_code, chat_id in set(chat_refs):
        out[(chat_type_code, chat_id)] = sorted(
            chat_member_user_ids(chat_type_code, chat_id)
        )
    return out


def resolve_thread_root_id(channel: Channel, thread_id) -> Optional[str]:
    """Map a legacy thread id (the parent message's per-channel `seq`) to
    the v3 thread-root `Message` UUID.

    v3 `Message.seq` mirrors the legacy `message_id`, so a thread's root is
    the top-level message at that seq. Returns the UUID string, or None.
    """
    try:
        seq = int(thread_id)
    except (TypeError, ValueError):
        return None
    root_id = (
        Message.objects.filter(channel=channel, seq=seq, is_thread_reply=False)
        .values_list("id", flat=True)
        .first()
    )
    return str(root_id) if root_id else None
