"""What `create_note` / `update_note` accept as a note family and a
destination, shared so the two tools read the same arguments the same way.

The backend has three note tables (personal, task, chat). "team" and
"shared" are sidebar spaces over the personal table (MIP-749): a team note
is a personal note filed in a Team Notes folder, a shared note is someone
else's personal note shared with you. The tools take them as aliases so a
model (or a person reading its call) can say what it means.
"""

from __future__ import annotations

import re
from typing import Any

from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.chunkers.base import CHAT_TYPE_LABEL
from origin.views.utils.note_folder_role import get_folder_role
from origin.views.utils.note_role import ROLE_EDITOR

PERSONAL_FAMILY = frozenset({"personal", "team", "shared"})
NOTE_TYPES = ("personal", "team", "shared", "task", "chat")

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_CHAT_URL = re.compile(rf"/chat/(?:dm|gm|pm|mdm)/({_UUID})(?:/thread/({_UUID}))?")
_BARE_UUID = re.compile(rf"^\s*({_UUID})\s*$")


def normalize_note_type(raw) -> str:
    note_type = (raw or "").lower().strip()
    if note_type not in NOTE_TYPES:
        raise ToolError(f"`note_type` must be one of {list(NOTE_TYPES)} (got {note_type!r}).")
    return note_type


def parse_chat_ref(raw_chat, raw_thread=None) -> tuple[str, str | None]:
    """`(channel_uuid, thread_root_uuid or None)` from a chat UUID or a pasted
    chat / thread URL (`/workspace/chat/gm/<uuid>/thread/<uuid>`). An explicit
    `thread_id` wins over a thread in the URL."""
    text = str(raw_chat or "").strip()
    if not text:
        raise ToolError("`chat_id` is required for note_type='chat'.")
    url = _CHAT_URL.search(text)
    if url:
        channel_id, url_thread = url.group(1), url.group(2)
    else:
        bare = _BARE_UUID.match(text)
        if not bare:
            raise ToolError(
                f"Could not read {text[:80]!r} as a chat. Pass the chat's id (a UUID) or its "
                "Genos URL."
            )
        channel_id, url_thread = bare.group(1), None

    thread = str(raw_thread or "").strip() or url_thread
    if thread:
        if not _BARE_UUID.match(thread):
            raise ToolError(f"`thread_id` must be a thread's id (a UUID), got {thread[:80]!r}.")
        thread = thread.strip()
    return channel_id, thread or None


def writable_team_folder(raw_folder_id, ctx: ToolContext) -> int:
    """A Team Notes folder in the caller's team that they may add notes to
    (Editor or stronger on the folder chain), as an int; ToolError if not."""
    try:
        folder_id = int(raw_folder_id)
    except (TypeError, ValueError):
        raise ToolError(f"`folder_id` must be an integer (got {raw_folder_id!r}).")
    scope = (
        PersonalNoteFolder.objects.filter(folder_id=folder_id, team=ctx.team_id)
        .values_list("scope", flat=True)
        .first()
    )
    if scope != PersonalNoteFolder.SCOPE_TEAM:
        raise ToolError(
            f"Folder {folder_id} is not a Team Notes folder in this team. Call "
            "list_note_folders to see the team folders and their ids."
        )
    role = get_folder_role(ctx.user_id, folder_id, ctx.team_id)
    if role is None or role > ROLE_EDITOR:
        raise ToolError(f"You can't add notes to Team Notes folder {folder_id}.")
    return folder_id


def chat_note_parent_context(note) -> dict[str, Any]:
    """`fetch_note`'s chat parent_context shape, so the controller's chip
    builder and the frontend deep link read a written chat note unchanged."""
    pc = {
        "chat_type": CHAT_TYPE_LABEL.get(note.chat_type),
        "chat_id": str(note.channel_id) if note.channel_id else None,
        "is_thread": bool(note.is_thread),
        "thread_id": str(note.thread_root_id) if note.thread_root_id else None,
    }
    return {k: v for k, v in pc.items() if v is not None}
