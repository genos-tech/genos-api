"""Uploaded note files whose note is already gone (MIP-1720).

An attachment row (`PersonalNoteAttachmentFact`, `TaskNoteAttachmentFact`,
`ChatNoteAttachmentFact`) points at its note with a `SET_NULL` foreign key. Until MIP-1720 no
note delete removed the rows, so a deleted note left `note = NULL` rows and their files in storage.
`note_delete.forget_note_attachments` stops new ones; this finds and removes the ones already there.

Why "note IS NULL" is enough to call a ROW unreachable, and what else is still checked before a
FILE is deleted:

  * Nothing reads the rows. The three upload routes (`api/v2/note/<family>/attachment/`) only
    POST; no route, serializer, search chunker or agent tool lists or fetches an attachment by id
    or by its note. Those routes refuse a request with no note (`validate_request_data`), so a row
    never STARTS with `note = NULL`: the only thing that makes it NULL is the note's deletion.
  * The file is served by PATH, not through the row: `/media/notes/<family>/<note id>/<name>` is
    authorized by the role on that note id (`media_views._may_read_media`), and a deleted note has
    no role on it for anyone. So the file is unreachable too.

A detached row has no owner and no team, so the only evidence of what it was is the path the model
built for it (`<family>_note_attachment_path`: `notes/<family>/<note id>/<file name>`). A row is
removed only when ALL of these hold, and is otherwise KEPT and counted:

  1. its note is NULL;
  2. its file name is exactly that shape, in ITS OWN family's directory (a name anywhere else is
     not a file this code made, and is never deleted: `kept_odd_path`);
  3. the note id in the path is in no note table of that family (`kept_note_alive`: cannot
     happen, the FK would still be set; checked because deleting a live note's file is the one
     mistake that cannot be undone);
  4. no attachment row that still HAS a note names the same file (`kept_file_shared`).

The file goes BEFORE its row, so a storage failure leaves the row for the next run instead of a
file nobody points at (the same order as `orphaned_team_rows`).

PR #959 (MIP-1713) already removes the attachments of the notes IT removes, before it removes the
notes, so it never creates a detached row; this does not overlap with it.
"""

from __future__ import annotations

import logging
import posixpath
import re
from dataclasses import dataclass

from django.core.files.storage import default_storage

from origin.models.note.chat_note_models import ChatNoteAttachmentFact, ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteAttachmentFact, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteAttachmentFact, TaskNoteMaster

logger = logging.getLogger(__name__)

# label -> (attachment table, note table): the label is the directory the upload path uses
FAMILIES = {
    "personal": (PersonalNoteAttachmentFact, PersonalNoteMaster),
    "task": (TaskNoteAttachmentFact, TaskNoteMaster),
    "chat": (ChatNoteAttachmentFact, ChatNoteMaster),
}
_ALL_ATTACHMENT_MODELS = tuple(a for a, _ in FAMILIES.values())


@dataclass
class DetachedReport:
    """Per family label: rows found (dry run) or removed (apply), and rows kept, with why."""

    removable: dict[str, int]
    kept_odd_path: int = 0
    kept_note_alive: int = 0
    kept_file_shared: int = 0
    files_removed: int = 0
    # Rows KEPT because their file could not be deleted; the next run retries them.
    file_errors: int = 0

    @property
    def total(self) -> int:
        return sum(self.removable.values())


def _path_regex(label: str) -> re.Pattern:
    # `os.path.join("notes", label, str(note_id), filename)`; a file name has no "/" (Django keeps
    # the base name of an upload), and a storage name that collided gets a suffix, still in it.
    return re.compile(rf"^notes/{label}/(\d+)/[^/]+$")


def _classify(label: str, row_name: str, note_model, report: DetachedReport) -> bool:
    """True when this detached row of family `label` may be deleted; otherwise counts why not."""
    match = _path_regex(label).match(row_name or "")
    if match is None:
        report.kept_odd_path += 1
        return False
    if note_model._base_manager.filter(note_id=int(match.group(1))).exists():
        report.kept_note_alive += 1
        return False
    if any(
        m._base_manager.filter(note_attachment_url=row_name, note__isnull=False).exists()
        for m in _ALL_ATTACHMENT_MODELS
    ):
        report.kept_file_shared += 1
        return False
    return True


def purge_detached_note_attachments(*, apply: bool) -> DetachedReport:
    """Find (and, with `apply`, delete) the attachment rows and files of notes that are gone.

    Dry run by default: nothing is written; `removable` is what `apply` would remove. Safe to run
    twice; the second run finds nothing."""
    report = DetachedReport(removable={label: 0 for label in FAMILIES})
    for label, (model, note_model) in FAMILIES.items():
        # The pks once, up front: a row whose file failed must not be fetched again this run.
        rows = list(
            model._base_manager.filter(note__isnull=True)
            .order_by("pk")
            .values_list("pk", "note_attachment_url")
        )
        for pk, name in rows:
            if not _classify(label, name, note_model, report):
                continue
            report.removable[label] += 1
            if not apply:
                continue
            try:
                default_storage.delete(name)
                report.files_removed += 1
            except Exception:  # noqa: BLE001 - storage errors vary; keep the row, retry next run
                # The directory only: a file name is the uploader's text.
                logger.exception(
                    "could not delete a detached note attachment file in %s",
                    posixpath.dirname(name),
                )
                report.file_errors += 1
                report.removable[label] -= 1
                continue
            # Only still-detached: a row somebody re-attached since the read is not ours.
            model._base_manager.filter(pk=pk, note__isnull=True).delete()
    return report
