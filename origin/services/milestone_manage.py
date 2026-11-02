"""Milestone changes shared by the REST views and the agent tools (MIP-879).

Moved out of `origin/views/task/milestone_views.py` so the agent's
`manage_milestone` / `delete_milestone` leave exactly what the UI's
PATCH / DELETE and the folder endpoints leave, without importing a DRF
view and without a second copy of the rules. The view keeps thin aliases
and calls these, so its behavior is unchanged.

Three things live here:

* `update_milestone`: the ordered core of `MilestoneView.patch`. The order
  matters (the backing task is mirrored from the milestone BEFORE the
  tasks are pushed to the new sprint, and the assignees are written last),
  which is why it is one function and not four helpers each caller chains
  in its own order.
* `delete_milestone`: `MilestoneView.delete` after its lookup and access
  check.
* The folder rules (`MilestoneFolderListView` / `MilestoneFolderView`).

Callers own their transaction, their access checks and the lookups; every
function here takes objects that are already resolved and validated.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from origin.models.task.milestone_models import MilestoneFolder, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.purge import purge_milestone, purge_task
from origin.services.milestone_service import (
    parse_iso_date,
    sync_backing_task,
    sync_milestone_assignees,
)
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.signals.task_signals import update_tasks_recorded

# "The request did not say": distinct from None, which clears a sprint / folder
# and, for a date, clears the date.
UNSET: Any = object()

# The columns a PATCH copies straight onto the milestone when the request
# names them. Dates are parsed (`update_milestone`'s `start_date` / `due_date`);
# the sprint, folder and assignees go through their own parameters.
PATCHABLE_FIELDS = (
    "title",
    "description",
    "status",
    "status_code",
    "priority",
    "priority_code",
    "effort_level",
    "effort_level_code",
    "reporter_id",
    "tags",
    "links",
)


def update_milestone(
    milestone: MilestoneMaster,
    *,
    fields: dict[str, Any],
    start_date: Any = UNSET,
    due_date: Any = UNSET,
    sprint: Any = UNSET,
    folder: Any = UNSET,
    assignee_ids: Any = UNSET,
    after_sync: Callable[[], None] | None = None,
) -> None:
    """Apply a milestone PATCH. The caller owns the transaction and the
    `invalidate_project_tasks_cache` call.

    `fields` holds the plain columns to set (`PATCHABLE_FIELDS`); `sprint` is a
    `Sprint` or None, `folder` a `MilestoneFolder` or None, both already checked
    to belong to the milestone's project; any of them left `UNSET` is not touched.
    `assignee_ids` replaces the whole assignee set (`[]` clears it).

    Order, as the UI's PATCH has always run it:
      1. set the columns, then `save()`;
      2. mirror onto the backing task (`sync_backing_task`);
      3. `after_sync`: writes that belong on the backing row only (custom
         fields, collaborators). They have to follow step 2, which rewrites
         that row wholesale;
      4. a sprint change is pushed to the milestone's tasks, so the sprint
         board and analytics stay in step;
      5. the assignee join is reconciled, and the backing task mirrored once
         more so it shows the new first assignee.
    """
    sprint_changed = sprint is not UNSET
    if sprint_changed:
        milestone.sprint = sprint
    if folder is not UNSET:
        milestone.folder = folder

    for field, value in fields.items():
        setattr(milestone, field, value)

    if start_date is not UNSET:
        milestone.start_date = parse_iso_date(start_date)
    if due_date is not UNSET:
        milestone.due_date = parse_iso_date(due_date)

    milestone.save()
    sync_backing_task(milestone)

    if after_sync is not None:
        after_sync()

    # Tasks linked to this milestone inherit the milestone's sprint by
    # convention (the frontend doesn't expose a direct sprint picker on tasks).
    # When the milestone moves between sprints, push the new sprint to every
    # task that cites it. `sync_backing_task` already handled the backing row;
    # this catches the rest. Only on a sprint change, so a no-op patch (a status
    # flip) doesn't churn rows.
    if sprint_changed:
        update_tasks_recorded(
            TaskMaster.objects.filter(milestone=milestone).exclude(sprint_id=milestone.sprint_id),
            cascade="milestone_sprint",
            sprint_id=milestone.sprint_id,
        )

    if assignee_ids is not UNSET:
        sync_milestone_assignees(milestone, assignee_ids)
        # The backing task shows the milestone's first assignee (else its
        # reporter), and step 2 ran before the assignees were written, so it
        # still showed the reporter (MIP-916). Mirror once more. Same as the
        # per-person endpoint, and a no-op for every other column.
        sync_backing_task(milestone)


def delete_milestone(milestone: MilestoneMaster) -> None:
    """Soft-delete a milestone the way `MilestoneView.delete` does."""
    milestone.is_deleted = True
    milestone.save(update_fields=["is_deleted", "ts_updated_at"])
    # Soft-delete the backing task too so the milestone disappears
    # from the project's task table on the next reload.
    if milestone.task_id is not None:
        TaskMaster.objects.filter(task_id=milestone.task_id).update(
            is_deleted=True, status="Deleted"
        )
    # Detach child tasks so they remain visible in the project view
    # rather than disappearing alongside the milestone. Children
    # tracked via parent_task_id are also unparented.
    # A task has no sprint of its own: it is in its milestone's sprint
    # (`sprint_id` mirrors the milestone's, see `reparent_task`). So the sprint
    # leaves with the milestone, in the same write so the history records it
    # (MIP-923); left behind, the task sat in a sprint it had no way to leave.
    update_tasks_recorded(
        TaskMaster.objects.filter(milestone=milestone),
        cascade="milestone_delete",
        milestone=None,
        sprint=None,
    )
    if milestone.task_id is not None:
        update_tasks_recorded(
            TaskMaster.objects.filter(parent_task_id=milestone.task_id),
            cascade="milestone_delete",
            parent_task_id=None,
        )
    # Backing task got soft-deleted + every child detached → next
    # project-tasks read needs a fresh DB pull.
    invalidate_project_tasks_cache(milestone.team_id, milestone.project_id)
    # Best-effort: drop the milestone's chunks (and its soft-deleted
    # backing task's) from OpenSearch — chunkers skip `is_deleted`
    # rows, so nothing else cleans these up until the orphan sweep.
    purge_milestone(milestone.milestone_id)
    if milestone.task_id is not None:
        purge_task(milestone.task_id)


# --------------------------------------------------------------------------
# folders (MIP-847)
# --------------------------------------------------------------------------

FOLDER_NAME_MAX = MilestoneFolder._meta.get_field("name").max_length


def clean_folder_name(raw: Any) -> str | None:
    """The folder name, trimmed; None when it is empty, not text, or too long."""
    name = raw.strip() if isinstance(raw, str) else ""
    if not name or len(name) > FOLDER_NAME_MAX:
        return None
    return name


def folder_name_taken(project_id: Any, name: str, *, exclude_folder_id: Any = None) -> bool:
    """True when the project already has a folder with exactly this name (the
    unique constraint is per project); `exclude_folder_id` is the folder being
    renamed."""
    qs = MilestoneFolder.objects.filter(project_id=project_id, name=name)
    if exclude_folder_id is not None:
        qs = qs.exclude(folder_id=exclude_folder_id)
    return qs.exists()


def create_folder(project, name: str) -> MilestoneFolder:
    """Add a folder at the end of the project's folder order."""
    last = (
        MilestoneFolder.objects.filter(project=project)
        .order_by("-sort_order")
        .values_list("sort_order", flat=True)
        .first()
    )
    return MilestoneFolder.objects.create(
        team=project.team,
        project=project,
        name=name,
        sort_order=(last + 1) if last is not None else 0,
    )
