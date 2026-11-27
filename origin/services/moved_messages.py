"""What is left to do for messages that changed chat (`MessageMovedAway`, MIP-2516).

Two limits on pasted images. One with a name the uploader did not generate (older uploads) stays
with the chat it was uploaded in. And once a message showing a file has moved, only chat messages
are looked at: the same URL copied into a note or a task body no longer opens for someone who is
in none of the chats with such a message.
"""

from __future__ import annotations

import json
import posixpath
import re
from datetime import timedelta
from urllib.parse import unquote

from django.db.models import TextField
from django.db.models.functions import Cast

from origin.models.chat.unified_models import Message, MessageMovedAway
from origin.services.channel_scope import is_chat_member

_INLINE_URL = re.compile(r"chats/([0-9a-fA-F-]{36})/inline/([^\"'\\?#\s)<>]+)")
# Only names the uploader generates (`upload_validation._generated_name`) are carried: one that
# can be guessed could be written into a message to take the file along without ever seeing it.
_GENERATED_NAME = re.compile(r"[0-9a-f]{32}(\.\w+)?")
# A move stamps its rows before it commits, so a sync running meanwhile cannot see them yet
# hands out a later checkpoint. Naming a gone id twice costs nothing.
_COMMIT_OVERLAP = timedelta(minutes=5)


def _inline_files(body) -> set[tuple[str, str]]:
    """`(channel id in the path, storage path)` of every inline upload a message body shows."""
    found = set()
    for channel_id, name in _INLINE_URL.findall(json.dumps(body, ensure_ascii=False)):
        path = posixpath.normpath(f"chats/{channel_id}/inline/{unquote(name)}")
        if path.startswith(f"chats/{channel_id}/inline/") and _GENERATED_NAME.fullmatch(
            path.rsplit("/", 1)[-1]
        ):
            found.add((channel_id.lower(), path))
    return found


def record_moved_away(source_id, messages) -> None:
    """Remember that `messages` (read before the move) are leaving the chat `source_id`.

    A file is recorded only when it was the leaving chat's to show: stored under it, or already
    recorded for this message by an earlier move. A link to some other chat's file records
    nothing, so a move never widens who may read it.
    """
    carried: dict = {}
    for message_id, files in MessageMovedAway.objects.filter(
        message_id__in=[m.pk for m in messages]
    ).values_list("message_id", "inline_files"):
        carried.setdefault(message_id, set()).update(files)
    MessageMovedAway.objects.bulk_create(
        [
            MessageMovedAway(
                channel_id=source_id,
                message_id=m.pk,
                is_thread_reply=m.is_thread_reply,
                inline_files=sorted(
                    {
                        path
                        for channel_id, path in _inline_files(m.body)
                        if channel_id == str(source_id) or path in carried.get(m.pk, ())
                    }
                ),
            )
            for m in messages
        ]
    )


def deleted_ids_since(channel, since, *, is_thread_reply) -> list[str]:
    """Ids for the delta's `deletes`: what left `channel` after `since` and is not back."""
    gone = MessageMovedAway.objects.filter(
        channel=channel,
        is_thread_reply=is_thread_reply,
        moved_at__gt=since - _COMMIT_OVERLAP,
    ).values("message_id")
    back = Message.objects.filter(channel=channel, id__in=gone).values_list("id", flat=True)
    return sorted({str(row["message_id"]) for row in gone} - {str(pk) for pk in back})


def may_read_moved_file(path: str, channel_id: str, user_id) -> bool | None:
    """May `user_id` open the inline upload `path`, stored under the chat `channel_id`?

    None for a file no message ever took elsewhere: the chat in the path decides. Otherwise they
    are in a chat one of those messages is in now, or in a chat the file was stored under or left
    where a message still shows it.
    """
    moved = list(
        MessageMovedAway.objects.filter(inline_files__contains=[path]).values_list(
            "message_id", "channel_id"
        )
    )
    if not moved:
        return None
    current = Message.objects.filter(id__in=[m for m, _ in moved]).values_list(
        "channel_id", flat=True
    )
    if any(is_chat_member(chat, user_id) for chat in set(current)):
        return True
    # Last, and only for their own chats: the text search over the chat's messages.
    left = {str(channel_id), *(str(c) for _, c in moved)}
    mine = [chat for chat in left if is_chat_member(chat, user_id)]
    if not mine:
        return False
    return (
        Message.objects.filter(channel_id__in=mine, deleted_at__isnull=True)
        .annotate(body_json=Cast("body", TextField()))
        .filter(body_json__contains=path.rsplit("/", 1)[-1])
        .exists()
    )
