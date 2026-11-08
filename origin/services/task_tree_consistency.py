"""Find, and repair, tasks whose placement disagrees with where they hang.

A task's place in the hierarchy is denormalized over four columns:
`parent_task_id` (the edge), and the three consequences of it, `milestone_id`,
`sprint_id` and `root_task_id`. The writers keep them together (the PUT, the
milestone and parent tools, the cascades), but a payload built from a stale
copy of a task once wrote a milestone its parent chain does not have and
carried the sub-tree there (MIP-1168). The rows that came out of that stay
wrong until something rewrites them, and the project table, which draws a task
under its parent, and the milestone filter, which reads `milestone_id`,
disagree about where they are, so the rows show up in neither.

This module is the one statement of the rule, as a read over a whole project:

  * a task directly under a milestone's backing row is in that milestone;
  * a task under an ordinary task is in whatever milestone that task is in;
  * its sprint is its milestone's sprint, or none;
  * its root is the top of its parent chain.

Parents are judged first, so a wrong milestone is fixed once, at the top of the
sub-tree, and its descendants follow from the corrected value. Only the edge is
trusted: it is the thing a person draws and drags. Milestone backing rows,
ordinary root tasks and rows whose parent no longer exists are left alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster

# Same cap as every other tree walk: a cycle in `parent_task_id` is corrupt
# data, and a walk that stops is better than one that spins.
_MAX_DEPTH = 64

CASCADE_NAME = "tree_repair"


@dataclass(frozen=True)
class Fix:
    task_id: int
    display_id: str | None
    changes: dict  # column -> (old, new)

    def describe(self) -> str:
        parts = ", ".join(f"{col} {old} -> {new}" for col, (old, new) in self.changes.items())
        return f"{self.display_id or self.task_id}: {parts}"


def find_inconsistent_tasks(*, team_id=None, project_id=None) -> list[Fix]:
    """Every task whose milestone, sprint or root disagrees with its parent.

    Reads each project once, in memory. Deleted tasks and the create form's
    scaffold rows are not judged.
    """
    tasks = TaskMaster.objects.filter(is_deleted=False, is_init_task=False)
    if team_id is not None:
        tasks = tasks.filter(team_id=team_id)
    if project_id is not None:
        tasks = tasks.filter(project_id=project_id)
    rows = {
        r["task_id"]: r
        for r in tasks.values(
            "task_id",
            "parent_task_id",
            "milestone_id",
            "sprint_id",
            "root_task_id",
            "is_milestone",
        )
    }
    if not rows:
        return []

    milestones = {
        m["milestone_id"]: m
        for m in MilestoneMaster.objects.filter(is_deleted=False).values(
            "milestone_id", "task_id", "sprint_id"
        )
    }
    milestone_of_backing = {
        m["task_id"]: m["milestone_id"] for m in milestones.values() if m["task_id"]
    }

    # `expected[task_id]` = (milestone, sprint, root) once the row is judged,
    # which is also what its children inherit.
    expected: dict[int, tuple] = {}

    def top_of_chain(task_id):
        current, seen = task_id, set()
        for _ in range(_MAX_DEPTH):
            row = rows.get(current)
            if row is None or row["parent_task_id"] is None or current in seen:
                return current
            seen.add(current)
            parent = rows.get(row["parent_task_id"])
            if parent is None:
                return current
            current = row["parent_task_id"]
        return current

    def judge(task_id, depth=0):
        if task_id in expected:
            return expected[task_id]
        row = rows[task_id]
        stored = (row["milestone_id"], row["sprint_id"], row["root_task_id"])
        parent_id = row["parent_task_id"]
        parent = rows.get(parent_id) if parent_id is not None else None
        # Backing rows, root tasks, rows with a vanished parent, and anything
        # past the depth cap stand as they are.
        if row["is_milestone"] or parent is None or depth > _MAX_DEPTH:
            expected[task_id] = stored
            return stored

        if parent_id in milestone_of_backing:
            milestone_id = milestone_of_backing[parent_id]
        else:
            # Guard a cycle before recursing into the parent.
            expected[task_id] = stored
            milestone_id = judge(parent_id, depth + 1)[0]
        if milestone_id is None:
            sprint_id = None
        elif milestone_id in milestones:
            sprint_id = milestones[milestone_id]["sprint_id"]
        else:
            # A milestone this module does not know (deleted) leaves the sprint be.
            sprint_id = row["sprint_id"]
        expected[task_id] = (milestone_id, sprint_id, top_of_chain(task_id))
        return expected[task_id]

    fixes: list[Fix] = []
    for task_id, row in rows.items():
        want = judge(task_id)
        have = (row["milestone_id"], row["sprint_id"], row["root_task_id"])
        if want == have:
            continue
        changes = {
            col: (old, new)
            for col, old, new in zip(("milestone_id", "sprint_id", "root_task_id"), have, want)
            if old != new
        }
        fixes.append(Fix(task_id=task_id, display_id=None, changes=changes))

    if fixes:
        labels = {
            t.task_id: t.display_id
            for t in TaskMaster.objects.filter(
                task_id__in=[f.task_id for f in fixes]
            ).select_related("project")
        }
        fixes = [
            Fix(task_id=f.task_id, display_id=labels.get(f.task_id), changes=f.changes)
            for f in sorted(fixes, key=lambda f: f.task_id)
        ]
    return fixes


def repair_inconsistent_tasks(fixes: list[Fix]) -> int:
    """Write `fixes`, one bulk update per distinct target, through the
    recorded-update helper: each row keeps a history entry
    (`cascade: tree_repair`), is stamped for incremental sync, and its
    project's cached task list is dropped."""
    from origin.signals.task_signals import update_tasks_recorded

    groups: dict[tuple, list[int]] = {}
    for fix in fixes:
        key = tuple(sorted((col, new) for col, (_old, new) in fix.changes.items()))
        groups.setdefault(key, []).append(fix.task_id)
    written = 0
    for key, task_ids in groups.items():
        written += update_tasks_recorded(
            TaskMaster.objects.filter(task_id__in=task_ids), cascade=CASCADE_NAME, **dict(key)
        )
    return written
