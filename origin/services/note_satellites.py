"""The rows that hang off a note by `(note_type, note_id)` and die with it (MIP-1517).

A note is HARD-deleted: none of the three note tables has an `is_deleted` flag, and there is no
trash (`services/note_delete.py`). But five tables point at a note by number only, with no
foreign key, so nothing cascades into them when a project, a task or an account takes the note
row with it:

  * `NotePermissionMaster`: who may open it;
  * `NoteVersionMaster`: its history, every row of which holds the note's TITLE AND BODY;
  * `NoteSummary` (Spotlight): the cached LLM summary of the body, in clear text;
  * `NoteFavoriteMaster` and `NoteRecentMaster`: the lists of notes people starred or opened.

`origin/signals/note_satellite_signals.py` calls `forget_note_satellites` from a `post_delete`
receiver on the three note models, so EVERY way a note row goes (a page delete, a folder delete,
a project or task cascade, the account erasure, a future path nobody has written yet) takes
these rows with it. Rows that `forget_deleted_notes` (`services/note_delete.py`) also clears are
simply already gone by then: the delete is a no-op the second time.

One note costs five small DELETEs. A path that removes many notes at once wraps the delete in
`batched_note_cleanup()`: the receiver then only notes the ids, and the block's exit deletes the
rows of ALL of them in five statements per `BATCH` notes, inside the same transaction.

Not here: the `Activity` mention rows (`meta.noteId`, a JSON scan), which stay with
`forget_deleted_notes`.
"""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import transaction

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine.models import NoteSummary

BATCH = 500

# note_type -> note ids noticed by the receiver inside a `batched_note_cleanup()` block;
# None outside one (the receiver then deletes at once).
_pending: ContextVar[dict[int, set[int]] | None] = ContextVar(
    "note_satellites_pending", default=None
)

_SATELLITES = (
    NotePermissionMaster,
    NoteVersionMaster,
    NoteSummary,
    NoteFavoriteMaster,
    NoteRecentMaster,
)


def forget_note_satellites(code: int, note_ids) -> None:
    """Delete the satellite rows of the notes `note_ids` of family `code` (1 personal, 2 task,
    3 chat). Idempotent. Inside `batched_note_cleanup()` it only records the ids."""
    ids = {int(n) for n in note_ids}
    if not ids:
        return
    pending = _pending.get()
    if pending is not None:
        pending[code] |= ids
        return
    _delete(code, ids)


def _delete(code: int, ids: set[int]) -> None:
    ordered = sorted(ids)
    for start in range(0, len(ordered), BATCH):
        chunk = ordered[start : start + BATCH]
        for model in _SATELLITES:
            model.objects.filter(note_type=code, note_id__in=chunk).delete()


@contextmanager
def batched_note_cleanup():
    """Wrap a delete that removes many notes: the satellite rows go in one pass at the end of
    the block, atomically with the delete. Nested blocks defer to the outermost one. If the
    block raises, nothing is flushed (the transaction rolls the delete back too)."""
    if _pending.get() is not None:
        yield
        return
    pending: dict[int, set[int]] = defaultdict(set)
    with transaction.atomic():
        token = _pending.set(pending)
        try:
            yield
        finally:
            _pending.reset(token)
        for code, ids in pending.items():
            _delete(code, ids)
