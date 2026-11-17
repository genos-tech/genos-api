"""Tell the people a note comment reaches, when an AI app made it (MIP-1669).

When a person comments on a note in the app, the note's owner and the people who can open it hear
about it ("X commented on your note"). That fan-out is started by the COMMENTING PERSON'S BROWSER: the
editor wraps its thread store, a socket handler in genos-sockets forwards the comment's coordinates to
`POST /api/v3/activities/surface-comment/`, and the API resolves the recipients from the note.

A comment an AI app writes over MCP (`comment_on_note`, `reply_to_note_comment`) goes straight into the
note's Yjs doc through genos-collab and has no browser to do that, so it reached nobody. This is that
hop, run in the API: the same producer (`create_surface_comment_activities`), the same surface type,
entity key and `meta` the socket handler builds (genos-sockets `utils/activity_emitter.py`), so the row
is indistinguishable from a typed comment's, with the same deterministic id: a retry collapses onto it.
It is how a task comment made over MCP already behaves (`fan_out_task_comment`).

The commenter is never told by this (the producer drops them); they get their own, switchable, AI-app
notice from `mcp_notice`. No @-mention is possible in these comments, so nothing is excluded.

Then, as the surface view does, the Web Push; and, because no sockets handler relays it, the live
`activity.created` to each recipient's open app (`realtime_push.push_activities`, MIP-1660).

Best-effort by design: the comment is already in the note, and the tool must report that, not a 500.
"""

from __future__ import annotations

import logging
from typing import Any

from django.utils import timezone

log = logging.getLogger(__name__)

# genos-sockets' surface chat types: 6/7/8 = personal / task / chat note.
_NOTE_SURFACE = {1: 6, 2: 7, 3: 8}
FIRST_LINE_CHARS = 200


def first_line(text: str) -> str:
    """The comment's first non-empty line, as the editor's own forward sends it."""
    for line in str(text or "").splitlines():
        line = line.strip()
        if line:
            return line if len(line) <= FIRST_LINE_CHARS else line[: FIRST_LINE_CHARS - 1] + "…"
    return ""


def _meta(note, note_type: int, comment_text: str) -> dict[str, Any]:
    """Routing and render fields the app's activity adapter reads off `meta` (the same keys
    genos-sockets `_build_meta` sets for a note surface)."""
    meta: dict[str, Any] = {
        "firstLineContent": first_line(comment_text),
        "tsSent": timezone.now().isoformat(),
        "noteId": int(note.note_id),
        "noteType": note_type,
        "noteTitle": note.title,
    }
    if note_type == 2:
        if getattr(note, "task_id", None):
            meta["taskId"] = int(note.task_id)
        if getattr(note, "project_id", None):
            meta["projectId"] = int(note.project_id)
    elif note_type == 3:
        # A chat note lives inside a chat: carry its routing so a click opens it. Both ids are
        # opaque (a legacy number or a v3 uuid) and are stored as they are.
        if getattr(note, "chat_type", None) is not None and getattr(note, "channel_id", None):
            meta["chatType"] = int(note.chat_type)
            meta["chatId"] = str(note.channel_id)
        if getattr(note, "is_thread", False) and getattr(note, "thread_root_id", None):
            meta["threadId"] = str(note.thread_root_id)
    return meta


def notify_note_comment(note, *, note_type: int, actor_id: str, comment_id: str, text: str) -> int:
    """File the "commented" activities for a comment an AI app just wrote on `note` (a personal (1),
    task (2) or chat (3) note), push them, and return how many rows were created. Never raises."""
    try:
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415
        from origin.services import v3_activity  # noqa: PLC0415
        from origin.services.realtime_push import push_activities  # noqa: PLC0415
        from origin.services.webpush_dispatch import schedule_push_for_activities  # noqa: PLC0415

        surface = _NOTE_SURFACE.get(note_type)
        if surface is None or not comment_id:
            return 0
        rows = v3_activity.create_surface_comment_activities(
            team_id=note.team_id,
            actor=CustomUser.objects.filter(id=actor_id).first(),
            surface_type=surface,
            # The per-comment key genos-sockets uses, so a comment the editor ALSO announced (it
            # cannot, for these) or a retried call collapses onto one row per recipient.
            entity_key=f"comment-participant:{comment_id}",
            meta=_meta(note, note_type, text),
            mentioned_user_ids=(),
        )
        # Only genuinely-new rows come back, so a retry never re-pushes.
        schedule_push_for_activities(rows)
        push_activities(rows)
        return len(rows)
    except Exception:  # noqa: BLE001 - the comment is already in the note
        log.exception(
            "note comment notify failed note=%s/%s", note_type, getattr(note, "note_id", None)
        )
        return 0
