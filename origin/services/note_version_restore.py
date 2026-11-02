"""Restoring an earlier version of a note: the page's rule and the agent's (MIP-883).

Restoring copies a past version's title and body onto the live note and writes a new
version marking the restore, so the restore can itself be walked back.

One thing differs from what the page did before this module existed: the text a
restore replaces is now kept first (`keep_current_state`), so a restore can always be
walked back even when no version row holds what the note says right now (a note nobody
has edited through the app has none; neither has one whose last change was made by a
tool before this existed). The restore's own row never coalesces, so the version it
came from stays intact.

The live editor is not touched here. The page's browser applies the body to its own
editor; a tool has no browser and pushes the body to the collaboration server itself
(`push_note_body_now`).
"""

from __future__ import annotations

from django.db import transaction

from origin.models.note.version_note_models import NoteVersionMaster
from origin.services.note_delete import FAMILIES
from origin.views.utils.note_version import keep_current_state, snapshot_note_version


class RestoreRefused(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def restore_note_version(
    *, team, user, family: str, note_id: int, version_no: int
) -> tuple[NoteVersionMaster, NoteVersionMaster]:
    """Put `version_no` back as the note's live title and body.

    Returns `(new_version, source)`: the row that records the restore, and the version
    it was restored from (whose body the caller pushes to an open editor).
    """
    model, code = FAMILIES[family]
    try:
        source = NoteVersionMaster.objects.get(
            note_type=code, note_id=note_id, version_no=version_no
        )
    except NoteVersionMaster.DoesNotExist:
        raise RestoreRefused("Version not found.", 404)
    try:
        note = model.objects.get(note_id=note_id)
    except model.DoesNotExist:
        raise RestoreRefused("Note not found.", 404)

    with transaction.atomic():
        keep_current_state(
            team=team, note_type=code, note_id=note_id, title=note.title, body=note.body
        )
        note.title = source.title
        note.body = source.body
        note.save(update_fields=["title", "body", "ts_updated_at"])
        new_version = snapshot_note_version(
            team=team,
            editor=user,
            note_type=code,
            note_id=note_id,
            title=source.title,
            body=source.body,
            restored_from_version_no=version_no,
        )
    return new_version, source
