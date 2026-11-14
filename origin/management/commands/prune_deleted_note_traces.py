"""Remove what people still hold about notes that no longer exist (MIP-1514).

A note delete used to leave three kinds of rows behind, each pointing at the vanished note:

  * `NoteRecentMaster`: the "recently opened" list of everyone who opened it;
  * `NoteFavoriteMaster`: the stars;
  * `Activity` rows of surface 6 / 7 / 8 (a personal / task / chat note's @mention and comment
    rows, `meta.noteId`), which a full resync of the feed brings back.

MIP-1489 and MIP-1514 stop new ones on every delete path (`note_delete.forget_deleted_notes`).
This command clears the ones that were left BEFORE that, for notes deleted by the paths that
did not call it.

    python manage.py prune_deleted_note_traces            # DRY RUN: counts only, writes nothing
    python manage.py prune_deleted_note_traces --apply    # delete the orphan rows

A row is an orphan when its note id is in no `PersonalNoteMaster` / `TaskNoteMaster` /
`ChatNoteMaster` row of its family (a note is hard-deleted: none of the three has an
`is_deleted` flag). Rows whose family is unknown, or an `Activity` row with no readable
`meta.noteId`, are left alone. Output is COUNTS ONLY: no note id, title, text, name or e-mail
is printed. Safe to re-run; the second `--apply` finds nothing.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from origin.models.chat.unified_models import Activity
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.services.v3_activity import (
    SURFACE_CHAT_NOTE,
    SURFACE_PERSONAL_NOTE,
    SURFACE_TASK_NOTE,
)

# the note family's `NOTE_TYPE_*`: its table, and the Activity surface its notes write rows on
FAMILIES = {
    1: ("personal", PersonalNoteMaster, SURFACE_PERSONAL_NOTE),
    2: ("task", TaskNoteMaster, SURFACE_TASK_NOTE),
    3: ("chat", ChatNoteMaster, SURFACE_CHAT_NOTE),
}
_BATCH = 2000


def orphan_rows(model, code: int):
    """The rows of a (note_type, note_id) table whose note of that family is gone."""
    _label, note_model, _surface = FAMILIES[code]
    return model.objects.filter(note_type=code).exclude(
        note_id__in=note_model.objects.values("note_id")
    )


def _noteid(meta) -> int | None:
    value = meta.get("noteId") if isinstance(meta, dict) else None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def orphan_activity_pks(code: int) -> list:
    """Primary keys of the surface-`code` Activity rows whose `meta.noteId` names no note."""
    _label, note_model, surface = FAMILIES[code]
    out: list = []
    batch: list[tuple] = []

    def flush():
        ids = {nid for _pk, nid in batch}
        alive = set(note_model.objects.filter(note_id__in=ids).values_list("note_id", flat=True))
        out.extend(pk for pk, nid in batch if nid not in alive)
        batch.clear()

    rows = Activity.objects.filter(surface_type=surface).values_list("pk", "meta")
    for pk, meta in rows.iterator(chunk_size=_BATCH):
        nid = _noteid(meta)
        if nid is None:
            continue
        batch.append((pk, nid))
        if len(batch) >= _BATCH:
            flush()
    if batch:
        flush()
    return out


class Command(BaseCommand):
    help = (
        "Find (dry run, the default) or remove (--apply) NoteRecentMaster, NoteFavoriteMaster and "
        "note-surface Activity rows whose note no longer exists. Prints counts only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Delete the orphan rows. Without it nothing is written.",
        )

    def handle(self, *args, **options):
        apply = bool(options["apply"])
        self.stdout.write("prune_deleted_note_traces: " + ("APPLY" if apply else "DRY RUN"))
        totals = {"recent": 0, "favorite": 0, "activity": 0}
        with transaction.atomic():
            for code, (label, _model, _surface) in FAMILIES.items():
                recents = orphan_rows(NoteRecentMaster, code)
                favorites = orphan_rows(NoteFavoriteMaster, code)
                activity = orphan_activity_pks(code)
                counts = {
                    "recent": recents.count(),
                    "favorite": favorites.count(),
                    "activity": len(activity),
                }
                for kind, n in counts.items():
                    totals[kind] += n
                self.stdout.write(
                    f"  {label} notes: {counts['recent']} recent, {counts['favorite']} favorite, "
                    f"{counts['activity']} activity orphan rows"
                )
                if apply:
                    recents.delete()
                    favorites.delete()
                    for start in range(0, len(activity), _BATCH):
                        Activity.objects.filter(pk__in=activity[start : start + _BATCH]).delete()
        verb = "deleted" if apply else "would delete"
        self.stdout.write(
            f"{verb}: {totals['recent']} recent, {totals['favorite']} favorite, "
            f"{totals['activity']} activity rows"
        )
        if not apply and sum(totals.values()):
            self.stdout.write("re-run with --apply to delete them")
