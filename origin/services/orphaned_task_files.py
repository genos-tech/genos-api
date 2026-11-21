"""Task files already left in storage with no row naming them (MIP-1824).

Before MIP-1824 a hard delete of a task, of a project or of a demo team removed the rows of the
task's attachments and body images and left their files on the media volume, where nothing could
reach them again. `task_files.stored_files_of_tasks` stops NEW ones; this finds the ones already
there and removes them, with the same care:

  * only files at exactly `task_attachments/<int>/<name>` and `tasks/<int>/<name>`, the two shapes
    the upload paths write. Any other path is counted and never deleted;
  * a file is an orphan when NO row names it, in either table. The task's own row being gone is
    the usual case (`orphan_task_gone`); a file whose task is alive but whose row is gone is the
    other (`orphan_task_live`). Only the first is removed by default: a live task's unnamed file may
    still be shown by its body (a row stored in another form, an upload from before a path change),
    so it is removed only with `include_live_tasks`, and then only when neither the task's body nor
    its comments contain the file's name (MIP-1890);
  * a file younger than `GRACE` is never matched: an upload in flight writes its file before its
    row, and the row is what makes the file "named". A file whose age cannot be read is kept;
  * the table is read once for the scan, and every candidate is looked up again just before it is
    deleted, so a row written since the scan protects its file;
  * at most `limit` files are deleted per run (`waiting` counts the rest), so a first run is small.

Dry run is the default: `scan(apply=False)` reads and writes nothing. Output is COUNTS ONLY: a file
name is the uploader's text, and nothing here prints or logs one.
"""

from __future__ import annotations

import json
import logging
import posixpath
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.core.files.storage import default_storage
from django.utils import timezone

from origin.models.task.task_models import (
    TaskAttachments,
    TaskBodyAttachmentFact,
    TaskComments,
    TaskMaster,
)

logger = logging.getLogger(__name__)

GRACE = timedelta(days=7)
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
ROOTS = ("task_attachments", "tasks")

# Why a file was not deleted. The text is printed, so it says it plainly.
KEPT_ODD_PATH = "not at a task file path"
KEPT_YOUNG = "younger than 7 days"
KEPT_NO_AGE = "age could not be read"
KEPT_REFERENCED = "a row names it"
KEPT_LIVE_TASK = "of a live task (needs --include-live-tasks)"
KEPT_IN_BODY = "a live task's body or comment contains its name"


@dataclass
class OrphanReport:
    apply: bool
    limit: int
    scanned: int = 0  # files found under the task roots
    orphan: int = 0  # no row names them
    orphan_task_gone: int = 0  # ... and their task row is gone
    orphan_task_live: int = 0  # ... and their task row is alive
    orphan_bytes: int = 0
    selected: int = 0  # taken (or, in a dry run, that would be) this run
    waiting: int = 0  # orphans past this run's limit
    removed: int = 0
    removed_bytes: int = 0
    errors: int = 0  # a storage error: kept, retried by a later run
    kept: Counter = field(default_factory=Counter)


def _walk(root: str):
    """Every file name under `root`, as storage names ('/'-separated). A missing root is empty."""
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            dirs, files = default_storage.listdir(folder)
        except (FileNotFoundError, NotADirectoryError):
            continue
        for name in files:
            yield posixpath.join(folder, name)
        stack.extend(posixpath.join(folder, sub) for sub in dirs)


def _task_id_of(name: str) -> int | None:
    """The task id of `<root>/<int>/<file>`, or None when the name is at any other path."""
    parts = name.split("/")
    if len(parts) != 3 or parts[0] not in ROOTS or not parts[1].isdigit():
        return None
    if not parts[2] or parts[2] in (".", "..") or ".." in parts:
        return None
    return int(parts[1])


def _referenced_names() -> set[str]:
    """Every stored name under the task roots that a row names, read once for the scan."""
    names = set(
        TaskAttachments.objects.filter(attached_file__startswith="task_attachments/").values_list(
            "attached_file", flat=True
        )
    )
    names.update(
        TaskBodyAttachmentFact.objects.filter(body_attachment_url__startswith="tasks/").values_list(
            "body_attachment_url", flat=True
        )
    )
    return names


def _named_now(name: str) -> bool:
    """A fresh look, right before a delete: does any row name this file now?"""
    return (
        TaskAttachments.objects.filter(attached_file=name).exists()
        or TaskBodyAttachmentFact.objects.filter(body_attachment_url=name).exists()
    )


def _named_in_task_text(task_id: int, name: str) -> bool:
    """Does the live task's body, or any comment of it, contain this file's name (or its basename)?"""
    needles = {name, posixpath.basename(name)}
    task = TaskMaster.objects.filter(task_id=task_id).values_list("content", flat=True).first()
    texts = [json.dumps(task, ensure_ascii=False)] if task else []
    texts.extend(
        json.dumps(body, ensure_ascii=False)
        for body in TaskComments.objects.filter(task_id=task_id).values_list(
            "comment_body", flat=True
        )
    )
    return any(n in t for t in texts for n in needles)


def scan(
    *,
    apply: bool,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
    include_live_tasks: bool = False,
) -> OrphanReport:
    """Find (and, with `apply`, delete) the orphaned task files, at most `limit` deletions.

    Dry run unless `apply`: then nothing is written. Safe to run twice: the second run finds only
    what was kept, and the files younger than the grace period are found once they are old."""
    report = OrphanReport(apply=apply, limit=limit)
    cutoff = (now or timezone.now()) - GRACE
    referenced = _referenced_names()
    live_task_ids = set(TaskMaster.objects.values_list("task_id", flat=True))

    for root in ROOTS:
        for name in _walk(root):
            report.scanned += 1
            task_id = _task_id_of(name)
            if task_id is None:
                report.kept[KEPT_ODD_PATH] += 1
                continue
            if name in referenced:
                report.kept[KEPT_REFERENCED] += 1
                continue
            try:
                modified = default_storage.get_modified_time(name)
            except Exception:  # noqa: BLE001 - keep what we cannot age
                report.kept[KEPT_NO_AGE] += 1
                continue
            if modified >= cutoff:
                report.kept[KEPT_YOUNG] += 1
                continue
            report.orphan += 1
            if task_id in live_task_ids:
                report.orphan_task_live += 1
            else:
                report.orphan_task_gone += 1
            try:
                size = default_storage.size(name)
            except Exception:  # noqa: BLE001 - the size is for the count only
                size = 0
            report.orphan_bytes += size

            if task_id in live_task_ids and not include_live_tasks:
                report.kept[KEPT_LIVE_TASK] += 1
                continue
            if report.selected >= limit:
                report.waiting += 1
                continue
            report.selected += 1
            if not apply:
                continue
            if task_id in live_task_ids and _named_in_task_text(task_id, name):
                report.kept[KEPT_IN_BODY] += 1
                report.selected -= 1
                continue
            if _named_now(name):  # a row written since the scan protects its file: not this run's
                report.kept[KEPT_REFERENCED] += 1
                report.selected -= 1
                continue
            try:
                default_storage.delete(name)
            except Exception:  # noqa: BLE001 - kept, the next run retries it
                report.errors += 1
                report.selected -= 1
                continue
            report.removed += 1
            report.removed_bytes += size
    return report
