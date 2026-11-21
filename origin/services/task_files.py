"""The stored files of tasks: what a task's delete takes with it, and removing them (MIP-1824).

A task's attachments (`task_attachments/<task id>/<name>`) and body images (`tasks/<task id>/<name>`)
are rows that CASCADE with the task. The files do not: until this, a hard delete of a task, of a
project or of a demo team removed the rows and left the files on the media volume, unreachable.

Every path that hard-deletes tasks calls `stored_files_of_tasks` BEFORE its delete (the rows are the
only record of the names) and `remove_after_commit` with what it got, so the files leave storage only
once the delete has committed. Callers: the task DELETE view, `project_delete.delete_project`, the
demo teardown (`demo_seeder.delete_demo_team_data`) and the `purge_deleted_tasks` purge.

Which files go is the same rule everywhere: a name is taken only when it sits under the task's own
folder, has no `..`, and no row of ANOTHER task names it. Anything else is counted and left: the row
goes either way, and a file we cannot prove is ours is never removed.

Output is never a file name: the name is the uploader's text. Logs carry counts only.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from django.core.files.storage import default_storage
from django.db import transaction

from origin.models.task.task_models import TaskAttachments, TaskBodyAttachmentFact

logger = logging.getLogger(__name__)

# Ids per query: a project delete can hold thousands of tasks.
BATCH = 500

# (model, the column that names the stored file, the folder root the upload path starts with)
_SOURCES = (
    (TaskAttachments, "attached_file", "task_attachments"),
    (TaskBodyAttachmentFact, "body_attachment_url", "tasks"),
)


def stored_files_of_tasks(task_ids: Iterable[int]) -> tuple[list[str], int]:
    """(names that may be deleted once these tasks' rows are gone, how many were left and why not).

    Reads only. Call it before the delete: afterwards nothing names the files."""
    ids = sorted({int(task_id) for task_id in task_ids})
    names: list[str] = []
    kept = 0
    for model, column, root in _SOURCES:
        for start in range(0, len(ids), BATCH):
            chunk = ids[start : start + BATCH]
            pairs = [
                (task_id, name)
                for task_id, name in model.objects.filter(task_id__in=chunk).values_list(
                    "task_id", column
                )
                if name
            ]
            if not pairs:
                continue
            elsewhere = named_by_other_rows([name for _tid, name in pairs], chunk)
            for task_id, name in pairs:
                own_folder = name.startswith(f"{root}/{task_id}/")
                if own_folder and ".." not in name.split("/") and name not in elsewhere:
                    names.append(name)
                else:
                    kept += 1
    return names, kept


def named_by_other_rows(names: list[str], task_ids: list[int]) -> set[str]:
    """The names, among `names`, that a row NOT belonging to `task_ids` still points at. A row with
    no task at all counts as "not these tasks" too: nothing else is known about the file."""
    found: set[str] = set()
    for model, column, _root in _SOURCES:
        for start in range(0, len(names), BATCH):
            found.update(
                model.objects.filter(**{f"{column}__in": names[start : start + BATCH]})
                .exclude(task_id__in=task_ids)
                .values_list(column, flat=True)
            )
    return found


def remove_after_commit(names: Iterable[str], *, family: str) -> None:
    """Delete these stored files once the current transaction has committed. Best-effort: a file
    that is already gone is fine, and any other failure is counted in the log, never named."""
    names = list(names)
    if names:
        transaction.on_commit(lambda: remove_now(names, family=family), robust=True)


def remove_now(names: Iterable[str], *, family: str) -> int:
    """Delete these stored files now. Returns how many could not be deleted (a missing file is not one)."""
    failed = 0
    for name in names:
        try:
            default_storage.delete(name)
        except Exception:  # noqa: BLE001 - a storage error must not undo a delete that committed
            failed += 1
    if failed:
        logger.warning("%d stored task file(s) could not be removed (%s)", failed, family)
    return failed
