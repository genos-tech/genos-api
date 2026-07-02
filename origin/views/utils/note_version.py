from django.db import IntegrityError, transaction
from django.utils import timezone

from origin.models.note.version_note_models import NoteVersionMaster

COALESCE_SECONDS = 300  # 5 minutes — one autosave burst becomes one row.


def _latest_version(note_type, note_id):
    return (
        NoteVersionMaster.objects.filter(note_type=note_type, note_id=note_id)
        .order_by("-version_no")
        .first()
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

    Returns the resulting `NoteVersionMaster` instance.
    """

    latest = _latest_version(note_type, note_id)

    if (
        latest is not None
        and restored_from_version_no is None
        # Never overwrite a restore marker — the next edit forces a
        # fresh row so the "restored from vN" label stays accurate.
        and latest.restored_from_version_no is None
        and editor is not None
        and latest.editor_id == editor.id
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
                )
        except IntegrityError:
            if attempt == 1:
                raise


def delete_note_versions(note_type, note_id):
    """Remove all version rows for a note (used in DELETE handlers)."""
    NoteVersionMaster.objects.filter(note_type=note_type, note_id=note_id).delete()
