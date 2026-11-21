"""Deleting a group chat (MIP-1705).

A group chat could only be left, never removed: the owner cannot leave it (`ChannelMemberDetailView`
refuses), so a chat nobody wants any more, a chat made by mistake, or the sample chats made with
the sample data stayed in everyone's list and, when public, in chat search for good.

Two ways, chosen by the owner in the delete dialog:

* **Soft** (the default): `Channel.is_deleted = True`. Nothing else is touched, so the chat can be
  put back by flipping the flag. The chat leaves every list (the app's chat list, the agent and MCP
  chat tools, search, the join directory, the message and member endpoints: they all read
  `is_deleted=False`, which is how a deleted PROJECT's chat already disappears, see
  `project_delete`), and its search chunks are dropped. Its cached thread summaries are removed
  (they are a cache of the chat's text; a restored chat writes new ones, MIP-1802).
* **Hard**: the messages and everything that hangs off them (reactions, mentions, attachments and
  their files, flags, reminders, activity rows), the members, pins, read cursors and the channel
  row itself are removed for good. `Message.channel` is PROTECT, so the messages go first.

What stays in BOTH ways is the chat's NOTES. A chat note is durable authored knowledge: its
`channel` FK is SET_NULL on purpose (`ChatNoteMaster`), and a deleted chat's notes stay readable to
whoever holds a role on them (the decision of MIP-1505). What does end is the access the chat's
MEMBERSHIP wrote on those notes, exactly as when someone leaves a chat or a project is deleted
(`revoke_membership_roles`); an explicit share the owner made stays.

Who may delete (the owner, and only a group chat) is checked by the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, Message, MessageAttachment
from origin.models.common.security_event_models import SecurityAction
from origin.models.common.team_models import ExternalGrant
from origin.services import thread_summary_cleanup
from origin.services.channel_scope import chat_member_rows
from origin.services.chat_note_access import revoke_membership_roles
from origin.services.external_grants import active_grants_for_object, revoke_grant
from origin.services.realtime_push import push_channel_deleted
from origin.services.security_events import record_security_event

logger = logging.getLogger(__name__)

SOFT = "soft"
HARD = "hard"
MODES = (SOFT, HARD)

# Messages are deleted in slices, so a chat with tens of thousands of them does not make Django
# hold every row and every cascaded reaction in memory at once.
_MESSAGE_BATCH = 1000


class ChannelDeleteError(Exception):
    """The chat cannot be deleted this way. `code` is stable; the message is for a person."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ChannelDeleteReport:
    mode: str
    messages_deleted: int = 0
    members: int = 0
    files_to_remove: int = 0


def delete_group_chat(channel: Channel, *, mode: str = SOFT, request=None) -> ChannelDeleteReport:
    """Soft- or hard-delete one group chat. See the module note for what each one takes.

    Atomic, because ATOMIC_REQUESTS is off: a failure half way through a hard delete must not
    leave a chat with its messages gone and its members still listed.

    The delete writes a `channel.deleted` security event (MIP-1716) inside the same transaction,
    so a delete that rolls back leaves no event and an event is never written for a delete that
    did not happen. `request` is only for the event's source and address.
    """
    if mode not in MODES:
        raise ChannelDeleteError("bad_mode", f"mode must be one of {', '.join(MODES)}.")
    if channel.kind != ChannelKind.GM:
        raise ChannelDeleteError("not_a_group", "Only a group chat can be deleted.")

    channel_id = channel.id
    team_id = channel.team_id
    kind = int(channel.kind)
    hard = mode == HARD
    report = ChannelDeleteReport(mode=mode)

    # Read BEFORE anything goes: who to tell, and when each joined (for the note roles).
    members = list(chat_member_rows(channel).values_list("user_id", "ts_joined_at"))
    report.members = len(members)
    member_ids = [str(user_id) for user_id, _ in members if user_id]
    # Counted now, for the event: a hard delete removes the rows, and a soft one never counts them.
    message_count = Message._base_manager.filter(channel_id=channel_id).count()
    title = channel.title
    files: list[str] = []

    with transaction.atomic():
        for user_id, joined_at in members:
            revoke_membership_roles(user_id=user_id, channel_id=channel_id, joined_at=joined_at)

        # The cached thread summaries (LLM digests of the replies) go with the chat in BOTH modes:
        # they hold its text and key it by a bare uuid, so nothing cascades to them (MIP-1802). A
        # restored chat writes a fresh one on the next ask. Their search chunks go on commit.
        thread_summary_cleanup.forget_channels([channel_id])

        if hard:
            files = _hard_delete(channel, report)
        else:
            # `update`, not `save`: nothing else on the row may change, and `ts_updated_at` is
            # stamped by hand because `update` skips `auto_now`.
            Channel.objects.filter(pk=channel_id).update(
                is_deleted=True, ts_updated_at=timezone.now()
            )

        record_security_event(
            SecurityAction.CHANNEL_DELETED,
            request=request,
            team_id=team_id,
            target_type="channel",
            target_id=channel_id,
            # A hard delete is a request to be forgotten: the title must not outlive the chat in
            # the log for the whole retention period (MIP-1584). A soft delete keeps it.
            target_label="" if hard else (title or ""),
            metadata={
                "channel_id": str(channel_id),
                "channel": "" if hard else title,
                "mode": mode,
                "members": report.members,
                "messages": message_count,
            },
        )

        # After commit, best-effort, so a rolled-back delete never touches the index, the storage
        # or anyone's open browser.
        transaction.on_commit(lambda: _forget(channel_id, kind, files), robust=True)
        push_channel_deleted(user_ids=member_ids, team_id=team_id, channel_id=channel_id, kind=kind)

    logger.info(
        "group chat deleted channel=%s team=%s mode=%s members=%s messages=%s",
        channel_id,
        team_id,
        mode,
        report.members,
        report.messages_deleted,
    )
    report.files_to_remove = len(files)
    return report


def _hard_delete(channel: Channel, report: ChannelDeleteReport) -> list[str]:
    """Remove the chat's rows for good. Returns the storage paths to delete after the commit."""
    # A shared chat: end what admitted other teams' people, or the grants would name a chat that
    # no longer exists. `actor=None` skips the permission check; the caller proved the owner.
    if channel.is_external:
        for grant in list(active_grants_for_object(ExternalGrant.ObjectType.CHANNEL, channel.id)):
            revoke_grant(grant)

    files = [
        name
        for name in MessageAttachment.objects.filter(message__channel_id=channel.id).values_list(
            "file", flat=True
        )
        if name
    ]
    if channel.profile_image_file:
        files.append(channel.profile_image_file.name)

    messages = Message._base_manager.filter(channel_id=channel.id)
    while True:
        ids = list(messages.order_by("seq").values_list("id", flat=True)[:_MESSAGE_BATCH])
        if not ids:
            break
        # A reply goes with its thread root through the CASCADE, so a slice can remove more rows
        # than it named; the loop ends when the channel has none left. The count is what the
        # delete reports it removed, not how many ids were named (MIP-1717).
        _, removed = Message._base_manager.filter(id__in=ids).delete()
        report.messages_deleted += removed.get(Message._meta.label, 0)

    # Members, direct pairs, read cursors, pins, activity and tag assignments cascade; the chat's
    # notes are SET_NULL and stay.
    Channel.objects.filter(pk=channel.pk).delete()
    return files


def _forget(channel_id, kind: int, files: list[str]) -> None:
    """What a delete leaves outside the database: search chunks and stored files. Best-effort."""
    from origin.search_engine.purge import purge_chat

    purge_chat(channel_id, kind)
    for name in files:
        try:
            default_storage.delete(name)
        except Exception:  # noqa: BLE001 — a missing or unreachable file must not undo a delete
            logger.warning("could not remove %s after deleting chat %s", name, channel_id)
