"""Which relation names of a task's history a reader may see (MIP-2523).

A `project_changed` / `milestone_changed` / `sprint_changed` / `parent_changed` row snapshots the
NAME of what it pointed at (`metadata.oldLabel` / `newLabel`, plus a parent's display id). A task
moves, and everyone who can read it reads its history, so the snapshot must be cut for a reader
who may not see the thing it names:

* a project: a Hidden project (MIP-1699) the reader is neither on nor owns;
* a milestone, sprint or parent task: one in a project the reader is neither on nor owns.

The task's own current project always passes: whoever reads the task already sees what it holds
there (an assignee outside the member list included). A thing that no longer exists cannot be
placed in a project, so its name is cut too. The ids stay (the app shows `#12`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from origin.models import MilestoneMaster, ProjectMaster, ProjectMembers, TaskMaster
from origin.models.task.sprint_models import Sprint
from origin.services.hidden_scope import visible_projects_q

# field_name -> (model, pk field) of the thing whose project decides.
_HELD_BY = {
    "milestone_id": (MilestoneMaster, "milestone_id"),
    "sprint_id": (Sprint, "sprint_id"),
    "parent_task_id": (TaskMaster, "task_id"),
}
_SIDES = ("old", "new")


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
        holders = {pid for by_id in held.values() for pid in by_id.values() if pid is not None}
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
        self._on = on
        self._named = named

    def _may_name(self, field: str, value: Any) -> bool:
        pk = _as_int(value)
        if pk is None:
            return False
        if field == "project_id":
            return pk in self._named
        return self._held[field].get(pk) in self._on

    def metadata(self, row, metadata: dict) -> dict:
        """`metadata` without the names this reader may not see (a copy only when it cuts)."""
        field = row.field_name
        if field != "project_id" and field not in _HELD_BY:
            return metadata
        cut = [
            key
            for side, value in zip(_SIDES, (row.old_value, row.new_value))
            if value not in (None, "") and not self._may_name(field, value)
            for key in (f"{side}Label", f"{side}DisplayId")
            if key in metadata
        ]
        if not cut:
            return metadata
        return {k: v for k, v in metadata.items() if k not in cut}
