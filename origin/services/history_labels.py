"""Which names of a task's history a reader may see (MIP-2523, MIP-2532).

A `project_changed` / `milestone_changed` / `sprint_changed` / `parent_changed` row snapshots the
NAME of what it pointed at (`metadata.oldLabel` / `newLabel`, plus a parent's display id). A task
moves, and everyone who can read it reads its history, so the snapshot must be cut for a reader
who may not see the thing it names:

* a project: a Hidden project (MIP-1699) the reader is neither on nor owns;
* a milestone, sprint or parent task: one in a project the reader is neither on nor owns.

The task's own current project always passes: whoever reads the task already sees what it holds
there (an assignee outside the member list included). A thing that no longer exists cannot be
placed in a project, so its name is cut too. The ids stay (the app shows `#12`).

A project's own custom fields and statuses (MIP-2532) are named only to a reader on that project
(or its current one, as above):

* a custom field row (`custom_field_changed`) belongs to the project it was written in
  (`TaskActivity.project`: only that project's fields are recorded). The field name goes, and so
  do both values: option ids, a member's id and typed text are that project's data as much as
  their labels are. A value that was set reads `HIDDEN_VALUE`, an unset one stays unset;
* a custom status name (a `custom_status_changed` row, or `metadata.customStatus` on a status
  row) belongs to the project holding the status. NOT to the row's project: the row a move writes
  when it drops the old project's status is stamped with the new one. A deleted status is no
  longer held anywhere, so the reader has to be on the row's project and on every project the
  task has left.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from origin.models import MilestoneMaster, ProjectMaster, ProjectMembers, TaskMaster
from origin.models.project.prj_models import ProjectStatus
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.services.hidden_scope import visible_projects_q

# field_name -> (model, pk field) of the thing whose project decides.
_HELD_BY = {
    "milestone_id": (MilestoneMaster, "milestone_id"),
    "sprint_id": (Sprint, "sprint_id"),
    "parent_task_id": (TaskMaster, "task_id"),
}
_SIDES = ("old", "new")
# What a set value of a cut custom field reads as. Not None: that says "it was empty".
HIDDEN_VALUE = "—"


def _status_change(row) -> dict | None:
    """The custom status snapshot a row carries, if any."""
    meta = row.metadata or {}
    if row.action_type == TaskActivityActionType.CUSTOM_STATUS:
        return meta
    change = meta.get("customStatus")
    return change if isinstance(change, dict) else None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _ids(rows: list, field: str) -> set[int]:
    return {
        i
        for r in rows
        if r.field_name == field
        for v in (r.old_value, r.new_value)
        if (i := _as_int(v)) is not None
    }


class HistoryLabels:
    """The rule above for one reader and one task's rows, resolved in a few queries up front."""

    def __init__(self, rows: Iterable, *, viewer_id: Any, task_project_id: int | None):
        rows = list(rows)
        project_ids = _ids(rows, "project_id")
        held: dict[str, dict[int, int]] = {}
        for field, (model, pk) in _HELD_BY.items():
            ids = _ids(rows, field)
            held[field] = (
                dict(model.objects.filter(**{f"{pk}__in": ids}).values_list(pk, "project_id"))
                if ids
                else {}
            )
        status_ids = {
            i
            for r in rows
            if (change := _status_change(r))
            for side in _SIDES
            if (i := _as_int(change.get(f"{side}Id"))) is not None
        }
        statuses = (
            dict(
                ProjectStatus.objects.filter(status_id__in=status_ids).values_list(
                    "status_id", "project_id"
                )
            )
            if status_ids
            else {}
        )
        # A deleted status: where the task has been is all that is left to go by.
        left: set[int | None] = set()
        if status_ids - statuses.keys():
            left = {
                _as_int(old)
                for old in TaskActivity.objects.filter(
                    task_id__in={r.task_id for r in rows},
                    action_type=TaskActivityActionType.PROJECT,
                    old_value__isnull=False,
                ).values_list("old_value", flat=True)
            }
        holders = {pid for by_id in held.values() for pid in by_id.values() if pid is not None}
        holders |= set(statuses.values()) | {pid for pid in left if pid is not None}
        holders |= {
            r.project_id
            for r in rows
            if r.project_id is not None
            and (r.action_type == TaskActivityActionType.CUSTOM_FIELD or _status_change(r))
        }
        on: set[int] = set()
        if holders and viewer_id is not None:
            on = set(
                ProjectMembers.objects.filter(
                    attendee_id=viewer_id, project_id__in=holders
                ).values_list("project_id", flat=True)
            ) | set(
                ProjectMaster.objects.filter(
                    owner_id=viewer_id, project_id__in=holders
                ).values_list("project_id", flat=True)
            )
        named: set[int] = set()
        if project_ids:
            named = set(
                ProjectMaster.objects.filter(project_id__in=project_ids)
                .filter(visible_projects_q(viewer_id))
                .values_list("project_id", flat=True)
            )
        if task_project_id is not None:
            on.add(task_project_id)
            named.add(task_project_id)
        self._held = held
        self._statuses = statuses
        self._left = left
        self._on = on
        self._named = named

    def _may_name(self, field: str, value: Any) -> bool:
        pk = _as_int(value)
        if pk is None:
            return False
        if field == "project_id":
            return pk in self._named
        return self._held[field].get(pk) in self._on

    def _may_name_status(self, row, status_id: Any) -> bool:
        pk = _as_int(status_id)
        if pk in self._statuses:
            return self._statuses[pk] in self._on
        return row.project_id in self._on and self._left <= self._on

    def _cut_status(self, row, change: dict) -> dict:
        cut = {
            f"{side}Label"
            for side in _SIDES
            if change.get(f"{side}Label") is not None
            and not self._may_name_status(row, change.get(f"{side}Id"))
        }
        return {k: v for k, v in change.items() if k not in cut} if cut else change

    def _cut_relation(self, row, metadata: dict) -> dict:
        field = row.field_name
        cut = [
            key
            for side, value in zip(_SIDES, (row.old_value, row.new_value))
            if value not in (None, "") and not self._may_name(field, value)
            for key in (f"{side}Label", f"{side}DisplayId")
            if key in metadata
        ]
        return {k: v for k, v in metadata.items() if k not in cut} if cut else metadata

    def cut(self, row) -> None:
        """Take out of `row`, in memory only (never saved), what this reader may not see."""
        if row.action_type == TaskActivityActionType.CUSTOM_FIELD:
            if row.project_id not in self._on:
                row.metadata = {
                    k: v
                    for k, v in (row.metadata or {}).items()
                    if k not in ("fieldId", "fieldName", "oldLabel", "newLabel")
                }
                row.old_value = None if row.old_value is None else HIDDEN_VALUE
                row.new_value = None if row.new_value is None else HIDDEN_VALUE
            return
        if not row.metadata:
            return
        if row.field_name == "project_id" or row.field_name in _HELD_BY:
            row.metadata = self._cut_relation(row, row.metadata)
        if row.action_type == TaskActivityActionType.CUSTOM_STATUS:
            row.metadata = self._cut_status(row, row.metadata)
        elif change := _status_change(row):
            row.metadata = {**row.metadata, "customStatus": self._cut_status(row, change)}
