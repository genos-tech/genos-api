from django.db import IntegrityError, transaction
from django.utils import timezone

from origin.models.note.version_note_models import NoteVersionMaster
from origin.services.write_via import recorded_via

COALESCE_SECONDS = 300  # 5 minutes — one autosave burst becomes one row.


def _latest_version(note_type, note_id):
    return (
        NoteVersionMaster.objects.filter(note_type=note_type, note_id=note_id)
        .order_by("-version_no")
        .first()
    )


def keep_current_state(*, team, note_type, note_id, title, body):
    """Make sure the note's CURRENT title and body are the newest version, so an
    edit about to replace them can be walked back.

    Version rows are written after an edit, so a note nobody has edited through
    the app has none, and the first agent edit would leave no copy of what it
    replaced. When the newest row already holds exactly this state, nothing is
    written. The row has no editor: nobody just made this state, it is what was
    there.
    """
    latest = _latest_version(note_type, note_id)
    if (
        latest is not None
        and (latest.title or "") == (title or "")
        and (latest.body or []) == (body or [])
    ):
        return latest
    return snapshot_note_version(
        team=team,
        editor=None,
        note_type=note_type,
        note_id=note_id,
        title=title or "",
        body=body or [],
        force_new=True,
    )


def snapshot_note_version(
    *,
    team,
    editor,
    note_type,
    note_id,
    title,
    body,
    restored_from_version_no=None,
    force_new=False,
):
    """Persist a version snapshot for the given note.

    Behaviour:
    - If the latest existing row is by the same editor, is an ordinary
      edit (not a restore marker), and was last updated within
      `COALESCE_SECONDS`, overwrite it in place. This makes a single
      typing session collapse to one row.
    - Otherwise, insert a new row with the next `version_no`.
    - Restore actions (`restored_from_version_no` set) always insert a
      fresh row; restore markers are never overwritten by subsequent
      coalesced edits.
    - `force_new` always inserts a fresh row. An edit made by an agent or an API
      key sets it: it is not a typing session, and the row it would overwrite
      holds the text it has just replaced (MIP-883).

    `via` (MIP-1488) is how the write arrived, from the write in progress
    (`write_via.recorded_via`): an API key or a connected app (MCP), else NULL (the web app,
    the in-app AI, an Undo, a command). A row with no `editor` is a copy of what was already
    there (`keep_current_state`), not anyone's write, so it never has one. A row is only
    coalesced into by a write with the same `via`: an autosave never takes over a row the
    AI app wrote (it would show the AI app's chip on the person's typing), and the AI app
    never takes over a person's.

    Returns the resulting `NoteVersionMaster` instance.
    """

    latest = _latest_version(note_type, note_id)
    via = recorded_via() if editor is not None else None

    if (
        latest is not None
        and not force_new
        and restored_from_version_no is None
        # Never overwrite a restore marker — the next edit forces a
        # fresh row so the "restored from vN" label stays accurate.
        and latest.restored_from_version_no is None
        and editor is not None
        and latest.editor_id == editor.id
        and latest.via == via
        and (timezone.now() - latest.ts_updated_at).total_seconds() < COALESCE_SECONDS
    ):
        latest.title = title
        latest.body = body
        latest.save(update_fields=["title", "body", "ts_updated_at"])
        return latest

    # New row. `version_no` is computed from the current head; under two
    # concurrent same-user PUTs the unique constraint on
    # (note_type, note_id, version_no) is the safety net — catch
    # IntegrityError, re-read the head, and retry once.
    for attempt in range(2):
        head = _latest_version(note_type, note_id)
        next_no = (head.version_no + 1) if head else 1
        try:
            with transaction.atomic():
                return NoteVersionMaster.objects.create(
                    team=team,
                    editor=editor,
                    note_type=note_type,
                    note_id=note_id,
                    version_no=next_no,
                    title=title,
                    body=body,
                    restored_from_version_no=restored_from_version_no,
                    via=via,
                )
        except IntegrityError:
            if attempt == 1:
                raise


def delete_note_versions(note_type, note_id):
    """Remove all version rows for a note (used in DELETE handlers)."""
    NoteVersionMaster.objects.filter(note_type=note_type, note_id=note_id).delete()
