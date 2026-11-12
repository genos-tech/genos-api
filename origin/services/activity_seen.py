"""Opening a task or a note marks its unread activity read (MIP-1421).

An unread `Activity` row is cleared by the chat read cursor (`read_cursor_views`), by a click
on the row in the Activity sidebar, or by "mark all read". Opening a task never cleared one, so
a comment on a task you had opened and read stayed unread on the sidebar, the app badge and
`list_my_unread` (MIP-1375). Notes were cleared only when opened from the notes tree
(`NoteUnreadContext.markRead`), not from a chip, a search result or a task's notes tab.

Both "open" endpoints (`RecordTaskOpenView`, `RecordNoteOpenView`) are called by the client when
it SHOWS the task or note to the person, and only then (an MCP or agent read never comes here),
so they are where "seen" is known. They call these and echo the cleared ids as
`readActivityIds`; the client flips them in its activity store (`handleV3ActivitiesRead`), the
same way it lands a read-cursor clear.

## What opening a task clears

Only what the task preview shows, for the caller, unread:

  * rows on the task's own card message (the "you were assigned" mention) and on its comment
    mirrors (a comment, an @mention in a comment, a reaction on one): `message.task_id` is the
    task AND the message is the card (not a thread reply) or carries `taskCommentId`;
  * rows on the task's description: `surface_type` 5 with `meta.taskId` (an @mention in the
    body, an inline comment on it).

NOT cleared: a plain reply typed in the task's project-chat thread (it shows in chat, where the
read cursor clears it), and the task NOTE's rows (`surface_type` 7: opening the task does not
show its note). The preview opens on the Comments tab when there are comments
(`useInitialTabIndex`), which is why a comment counts as seen.

## What opening a note clears

The caller's unread rows on that note: `surface_type` 6/7/8 (personal / task / chat note) with
`meta.noteId`, the body @mentions and the inline comments. The briefing reads a READ note
mention as answered (`briefing._activity_mentions`), as it already did after opening the note
from the tree.

`meta` is JSON written by two producers, so an id may be stored as a number or as a string;
both are matched.
"""

from __future__ import annotations

import logging

from django.db.models import Q

from origin.models.chat.unified_models import Activity

logger = logging.getLogger(__name__)

SURFACE_TASK_BODY = 5
# note_type (1 personal, 2 task, 3 chat) -> the activity surface it rides
NOTE_SURFACE = {1: 6, 2: 7, 3: 8}


def _either(key: str, value: int) -> Q:
    return Q(**{f"meta__{key}": value}) | Q(**{f"meta__{key}": str(value)})


def _mark_read(user_id, scope: Q) -> list[str]:
    ids = [
        str(pk)
        for pk in Activity.objects.filter(scope, recipient_id=user_id, is_read=False).values_list(
            "id", flat=True
        )
    ]
    if ids:
        Activity.objects.filter(id__in=ids, is_read=False).update(is_read=True)
    return ids


def mark_task_activity_read(user_id, task_id: int) -> list[str]:
    """Mark the caller's unread activity on `task_id` read; returns the cleared ids."""
    on_task_messages = Q(message__task_id=task_id) & (
        Q(message__is_thread_reply=False) | Q(message__metadata__has_key="taskCommentId")
    )
    on_description = Q(surface_type=SURFACE_TASK_BODY) & _either("taskId", task_id)
    return _mark_read(user_id, on_task_messages | on_description)


def mark_note_activity_read(user_id, note_type: int, note_id: int) -> list[str]:
    """Mark the caller's unread activity on one note read; returns the cleared ids."""
    surface = NOTE_SURFACE.get(int(note_type))
    if surface is None:
        return []
    return _mark_read(user_id, Q(surface_type=surface) & _either("noteId", int(note_id)))


def safely(clear, *args) -> list[str]:
    """Run a clear for an "open" endpoint: an open must never fail because of it."""
    try:
        return clear(*args)
    except Exception as exc:  # noqa: BLE001 — opening the task is what the caller asked for
        logger.warning("[activity_seen] %s failed: %s", getattr(clear, "__name__", clear), exc)
        return []
