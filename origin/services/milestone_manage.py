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

from django.db.models import BigIntegerField, Case, Q, Value, When
from django.utils import timezone

from origin.models.task.milestone_models import MilestoneFolder, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.purge import purge_milestone, purge_task
from origin.services.milestone_service import (
    parse_iso_date,
    sync_backing_task,
    sync_milestone_assignees,
)
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.services.task_project_move import collect_descendant_task_ids
from origin.services.task_reparent import resolve_root_task_id
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
) -> list[str]:
    """Apply a milestone PATCH. The caller owns the transaction and the
    `invalidate_project_tasks_cache` call. Returns the ids of the people it ADDED as
    assignees, for the caller to tell once the transaction commits (MIP-1464).

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

    added: list[str] = []
    if assignee_ids is not UNSET:
        added = sync_milestone_assignees(milestone, assignee_ids)
        # The backing task shows the milestone's first assignee (else its
        # reporter), and step 2 ran before the assignees were written, so it
        # still showed the reporter (MIP-916). Mirror once more. Same as the
        # per-person endpoint, and a no-op for every other column.
        sync_backing_task(milestone)
    return added


# How far down the `parent_task_id` chain the re-root follows, and how far up it
# walks a stray row: the same cap `resolve_root_task_id` uses, so a cyclic or
# corrupt chain ends instead of spinning.
REROOT_DEPTH_LIMIT = 64


def reroot_detached_tasks(backing_task_id: int, milestone_id: int) -> int:
    """Give every task that hung under a milestone's backing row a root of its
    own, as `reparent_task` does for any other detach. Returns how many rows
    changed (MIP-931).

    A task in a milestone is filed under the backing row and is rooted at it
    (`bridge_milestone_to_parent`), and so is everything below it (a new
    sub-task copies its parent's root). Cutting those tasks loose from a
    deleted backing row without moving the root left every one of them pointing
    at a dead row, which the task diagram, the sub-task drawer and every new
    sub-task anchor on. After this, each task that hung DIRECTLY off the backing
    row is its own root and each of its descendants points at it. A row still
    rooted at the backing row but not under it by `parent_task_id` (legacy
    data) is rooted by its own parent chain, or by itself when it has none.

    A row filed in ANOTHER milestone is not this delete's to change, even when
    it hangs under one of these tasks (the milestone picker accepts an explicit
    parent, and keeps the milestone's own backing row as the root): it, and
    whatever hangs under it, keeps its stored root. Only a root that is the
    dead backing row itself is replaced, by the chain rule above.

    Call it BEFORE the direct children lose their `parent_task_id`: the chain is
    what tells a descendant which top it belongs to. The tree is read in a
    fixed handful of queries (one per level plus one for the rows) and written
    in one, so the cost does not grow with the number of tasks. Rows whose root
    is already right are left alone. The backing row keeps its own row.

    Not `cascade_tree_position_to_subtasks`: it walks one top task at a time
    (a loop of queries on a milestone with hundreds of tasks) and also pushes
    `milestone_id` / `sprint_id`, which the detach has just cleared in its own,
    labelled write.
    """
    below = collect_descendant_task_ids(backing_task_id, depth_limit=REROOT_DEPTH_LIMIT)
    rows = {
        task_id: (parent_id, root_id, in_milestone_id)
        for task_id, parent_id, root_id, in_milestone_id in TaskMaster.objects.filter(
            Q(task_id__in=below) | Q(root_task_id=backing_task_id)
        )
        .exclude(task_id=backing_task_id)
        .values_list("task_id", "parent_task_id", "root_task_id", "milestone_id")
    }

    def elsewhere(task_id: int) -> bool:
        _parent, root_id, in_milestone_id = rows[task_id]
        return in_milestone_id not in (None, milestone_id) and root_id != backing_task_id

    # task -> (its root once the backing row is gone, whether the chain above it
    # runs through a row filed elsewhere)
    settled_root: dict[int, tuple[int | None, bool]] = {}
    for task_id in rows:
        if task_id in settled_root:
            continue
        path: list[int] = []
        current = task_id
        while True:
            path.append(current)
            parent_id = rows[current][0]
            if elsewhere(current):
                settled = (rows[current][1], True)
            elif parent_id is None or parent_id == backing_task_id:
                settled = (current, False)  # the top of its own sub-tree
            elif parent_id in settled_root:
                settled = settled_root[parent_id]
            elif parent_id in rows and parent_id not in path and len(path) < REROOT_DEPTH_LIMIT:
                current = parent_id
                continue
            elif parent_id in rows:
                settled = (current, False)  # a cycle, or a chain too long to be real: anchor here
            else:
                # Filed under a task that is not part of this tree (legacy
                # data): it roots wherever that task's chain does. A chain that
                # somehow still ends at the backing row anchors on the row.
                root = resolve_root_task_id(current, parent_id)
                settled = (current if root == backing_task_id else root, False)
            break
        for member in path:
            settled_root[member] = settled

    changed: dict[int, int] = {}
    for task_id, (root, behind_elsewhere) in settled_root.items():
        stored = rows[task_id][1]
        if root is None or root == stored:
            continue
        if behind_elsewhere and stored != backing_task_id:
            continue
        changed[task_id] = root
    if not changed:
        return 0
    # One write for every row, whatever its new root. Stamped by hand through
    # `update_tasks_recorded`: `root_task_id` is part of the task payload and
    # a bare `.update()` would leave `ts_updated_at` alone, so a browser that
    # syncs "rows changed since my checkpoint" would never hear of it (MIP-936).
    return update_tasks_recorded(
        TaskMaster.objects.filter(task_id__in=changed),
        cascade="milestone_delete",
        root_task_id=Case(
            *[When(task_id=task_id, then=Value(root)) for task_id, root in changed.items()],
            output_field=BigIntegerField(),
        ),
    )


def delete_milestone(milestone: MilestoneMaster) -> None:
    """Soft-delete a milestone the way `MilestoneView.delete` does."""
    milestone.is_deleted = True
    milestone.save(update_fields=["is_deleted", "ts_updated_at"])
    # Soft-delete the backing task too so the milestone disappears
    # from the project's task table on the next reload. `ts_updated_at` is
    # stamped by hand: a queryset `.update()` skips `auto_now`, and a browser
    # that already holds this row only hears about a delete as a row changed
    # since its checkpoint, so without it the milestone stayed on screen
    # there for good, and the UI's own delete then found it already gone
    # (MIP-936).
    if milestone.task_id is not None:
        TaskMaster.objects.filter(task_id=milestone.task_id).update(
            is_deleted=True, status="Deleted", ts_updated_at=timezone.now()
        )
    # Re-root first: it reads the parent edges the unparenting below cuts.
    if milestone.task_id is not None:
        reroot_detached_tasks(milestone.task_id, milestone.milestone_id)
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
