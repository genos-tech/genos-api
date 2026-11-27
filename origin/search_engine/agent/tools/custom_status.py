"""A project's own statuses in the agent and MCP tools (MIP-2494), behind
`SEARCH_ENGINE["AGENT_CUSTOM_STATUS"]` (default off).

`status` stays the system value every tool and automation reads; `custom_status` is the NAME the
project gives it ("In Review", "Ready to start"). With the flag on:

  * reads add `custom_status` to a task or milestone that has one;
  * writes take an optional `custom_status` name, which also sets `status` to its category;
  * `list_project_tags` lists the project's `statuses`.

A name that does not fit is never a refusal (Ken, 2026-11-22: Genos guides with suggestions): the
rest of the call is applied and the reply says what was left out, with the names that would fit.

With the flag off nothing here is declared, read or written.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.db.models import F

from origin.models.project.prj_models import ProjectMaster, ProjectStatus
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.parity import flag_on
from origin.services.project_statuses import (
    BUILTIN_STATUSES,
    ensure_project_statuses,
    list_statuses,
)

FLAG = "AGENT_CUSTOM_STATUS"
ARG = "custom_status"

_WRITE_DESCRIPTION = (
    "Optional status NAME from the project's list (list_project_tags `statuses`), e.g. 'In "
    "Review'. It also sets `status` to that name's category, so omit `status` or pass the "
    "category. '' removes the name."
)
#: What the flag adds to a write tool's schema (`tool_flag_params`).
PROPERTIES: dict[str, dict[str, Any]] = {
    ARG: {"type": "STRING", "description": _WRITE_DESCRIPTION},
}
#: The same, on each row of a batch (`create_task_plan.tasks`, `update_tasks_bulk.updates`).
ROW_PROPERTIES: dict[str, dict[str, Any]] = {
    ARG: {"type": "STRING", "description": "Status name, as in update_task."},
}

#: Sentences the flag adds to a description (`tool_flag_text`).
LIST_DESCRIPTION_ADDITION = (
    " `statuses` lists the project's status names in order, each with its `category` (the "
    "`status` value it stands for): pass a name as `custom_status` when you create or update."
)
READ_DESCRIPTION_ADDITION = (
    " `custom_status`, when present, is the project's own name for `status`: use it when you "
    "talk about the task."
)


def enabled() -> bool:
    return flag_on(FLAG)


def requested(args: dict[str, Any]) -> Any:
    """The `custom_status` a call (or a batch row) asks for, or None: unsent, or the flag is off."""
    return args.get(ARG) if enabled() else None


def _clean(raw: str) -> str:
    return " ".join(raw.split()).casefold()


def project_status_choices(project: ProjectMaster) -> list[dict[str, str]]:
    """The statuses a task of `project` can be given by name: its rows that are turned on, or the
    built-in list while nobody has set the project's up. Reads only."""
    rows = list_statuses(project)
    if not rows:
        return [{"name": name, "category": category} for name, category in BUILTIN_STATUSES]
    return [{"name": r.name, "category": r.category} for r in rows if r.archived_at is None]


@dataclass(frozen=True)
class Choice:
    """What a requested name means for one write. Building it writes nothing."""

    #: The `status` the name implies; None when the name was not usable or only clears.
    status: str | None = None
    #: The row to point at. None with `builtin_key` set means "the built-in row, to be written".
    row: ProjectStatus | None = None
    builtin_key: str | None = None
    project: ProjectMaster | None = None
    #: `''`: remove the task's custom status.
    clear: bool = False
    suggestion: dict[str, Any] | None = None

    @property
    def writes_row(self) -> bool:
        return self.row is not None or self.builtin_key is not None

    def resolve_row(self) -> ProjectStatus | None:
        """The row to store, writing the project's built-in rows first when the name needs one.
        Call inside the write's transaction."""
        if self.row is not None or self.builtin_key is None:
            return self.row
        ensure_project_statuses(self.project)
        return ProjectStatus.objects.filter(
            project=self.project, builtin_key=self.builtin_key, archived_at__isnull=True
        ).first()


def _left_out(project: ProjectMaster | None, why: str) -> Choice:
    return Choice(
        suggestion={
            "kind": ARG,
            "message": f"`{ARG}` was not applied: {why} The rest of the call was.",
            "candidates": project_status_choices(project) if project is not None else [],
        }
    )


def choose(
    project: ProjectMaster | None, raw: Any, *, requested_status: str | None = None
) -> Choice:
    """What `custom_status=raw` means in `project`, given the `status` sent with it (if any)."""
    if raw is None:
        return Choice()
    if project is None:
        return _left_out(None, "the task has no project.")
    if not isinstance(raw, str):
        return _left_out(project, "it must be a status name.")
    wanted = _clean(raw)
    if not wanted:
        return Choice(clear=True)
    rows = list_statuses(project)
    builtin_key = None
    row = None
    if rows:
        row = next((r for r in rows if _clean(r.name) == wanted), None) or next(
            (r for r in rows if r.builtin_key and _clean(r.builtin_key) == wanted), None
        )
        if row is None:
            return _left_out(project, "this project has no status with that name.")
        if row.archived_at is not None:
            return _left_out(project, "that status is turned off in this project.")
        category = row.category
    else:
        found = next(((n, c) for n, c in BUILTIN_STATUSES if _clean(n) == wanted), None)
        if found is None:
            return _left_out(project, "this project has no status with that name.")
        name, category = found
        # A built-in that is its category's own name needs no row: `status` says it already.
        builtin_key = name if name != category else None
    if requested_status not in (None, "") and requested_status != category:
        return _left_out(
            project, f"it belongs to `status` {category}, not {requested_status} as sent."
        )
    return Choice(status=category, row=row, builtin_key=builtin_key, project=project)


def apply_to(obj: TaskMaster | MilestoneMaster, choice: Choice) -> bool:
    """Set `obj.custom_status` as `choice` says, in memory; True when it changed. Call after
    `obj.status` is final, and save the column."""
    if choice.clear:
        if obj.custom_status_id is None:
            return False
        obj.custom_status = None
        return True
    if not choice.writes_row:
        return False
    row = choice.resolve_row()
    if row is None or row.category != obj.status or row.status_id == obj.custom_status_id:
        return False
    obj.custom_status = row
    return True


def _names(model, key: str, ids: Iterable[Any]) -> dict[int, str]:
    """`{id: name}` for the rows whose custom status still matches their status and project, the
    rule of `project_statuses.custom_status_ref`, in one query."""
    return dict(
        model.objects.filter(
            **{f"{key}__in": [i for i in ids if i is not None]},
            custom_status__category=F("status"),
            custom_status__project_id=F("project_id"),
        ).values_list(key, "custom_status__name")
    )


def _add(rows: list[dict[str, Any]], key: str, model) -> None:
    if not (enabled() and rows):
        return
    names = _names(model, key, [r.get(key) for r in rows])
    if not names:
        return
    for row in rows:
        raw = row.get(key)
        name = names.get(int(raw)) if raw is not None else None
        if name is not None:
            row[ARG] = name


def add_to_tasks(rows: list[dict[str, Any]]) -> None:
    """Add `custom_status` to result rows carrying `task_id` that have one (flag on)."""
    _add(rows, "task_id", TaskMaster)


def add_to_milestones(rows: list[dict[str, Any]]) -> None:
    """The same for rows carrying `milestone_id`."""
    _add(rows, "milestone_id", MilestoneMaster)
