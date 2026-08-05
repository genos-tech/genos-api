"""Moving tasks — and whole milestones — between projects.

More of a task's identity is project-scoped than the `project_id` column
itself. Its `<CODE>-<n>` display id is minted per project, its notes keep
their own copy of the owning project, and the project task list is read
with `team_id` **and** `project_id`. A move that rewrites only
`project_id` therefore produces a row that belongs to no list anyone can
open — which is exactly what moving a task into an externally shared
project did: the destination project is owned by the HOST team, the row
kept the guest team, and `GetProjectTasksView` (filtering on both)
matched it under neither the source nor the destination project. The task
appeared to simply vanish.

So a move is defined here, once, over a SET of tasks, and both callers —
the task PUT and the milestone PATCH — hand over the whole sub-tree they
are moving.
"""

import logging

from django.db.models import Q
from django.utils import timezone

from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.task_models import TaskDependency, TaskMaster, claim_project_task_number

logger = logging.getLogger(__name__)

# Real task hierarchies are shallow; the cap defangs any cyclic or
# corrupt `parent_task_id` chain that might exist in the wild.
DEPTH_LIMIT = 10


def collect_descendant_task_ids(root_task_id, depth_limit=DEPTH_LIMIT):
    """Every task below `root_task_id` in the `parent_task_id` chain
    (the root itself excluded)."""
    collected = set()
    frontier = {root_task_id}
    for _ in range(depth_limit):
        if not frontier:
            break
        children = set(
            TaskMaster.objects.filter(parent_task_id__in=frontier)
            .exclude(task_id__in=collected | {root_task_id})
            .values_list("task_id", flat=True)
        )
        if not children:
            break
        collected |= children
        frontier = children
    return collected


def milestone_subtree_task_ids(milestone) -> set:
    """Every task that has to move when `milestone` moves.

    A task belongs to a milestone through the explicit FK *or* by hanging
    under its backing task — `_serialize_milestone`'s rollups count both,
    so a move has to carry both. Their descendants come along for the same
    reason a moved task's sub-tasks do, and the backing task itself is in
    the set because it IS the milestone's row in the project task table.
    """
    ids = set()
    if milestone.task_id is not None:
        ids.add(milestone.task_id)
    membership = Q(milestone_id=milestone.milestone_id)
    if milestone.task_id is not None:
        membership |= Q(parent_task_id=milestone.task_id)
    ids |= set(TaskMaster.objects.filter(membership).values_list("task_id", flat=True))
    for task_id in list(ids):
        ids |= collect_descendant_task_ids(task_id)
    return {int(task_id) for task_id in ids if task_id is not None}


def relocate_tasks(
    task_ids,
    *,
    project_id,
    team_id,
    clear_sprint=False,
    clear_tags=True,
) -> None:
    """Move a set of task rows into `project_id` / `team_id`.

    For the rows that ride ALONG with a move (sub-tasks, a milestone's
    member tasks) — the ones no user re-picks anything for. The row the
    user actually edited is written by its own endpoint; call
    `relocate_task_satellites` for that one.

    `clear_tags` is on by default because tags are project-scoped rows and,
    across a team boundary, another tenant's labels: the frontend already
    clears them on the task the user moved, and nothing would ever clear
    them on the rows dragged along behind it. `custom_field_values` are
    deliberately left alone — readers resolve them against the live field
    definitions and drop unknowns, so they cost nothing where they are and
    carrying free text back is impossible once discarded.
    """
    task_ids = {int(task_id) for task_id in task_ids if task_id is not None}
    if not task_ids:
        return

    fields = {
        "project_id": project_id,
        "team_id": team_id,
        # Numbers are unique per project, so a rider's number can already
        # be taken in the destination. NULL is exempt from the constraint;
        # a free number is re-claimed below.
        "project_task_number": None,
        # A queryset `.update()` skips `auto_now` (and `post_save`).
        # Without this, clients that sync incrementally off `ts_updated_at`
        # never learn these rows moved.
        "ts_updated_at": timezone.now(),
    }
    if clear_sprint:
        fields["sprint_id"] = None
    if clear_tags:
        fields["tags"] = None
    TaskMaster.objects.filter(task_id__in=task_ids).update(**fields)

    # Re-fetch AFTER the bulk update so each instance carries the new
    # project — `claim_project_task_number` counts within `project_id`.
    for task in TaskMaster.objects.filter(task_id__in=task_ids):
        claim_project_task_number(task)

    relocate_task_satellites(task_ids, project_id=project_id, team_id=team_id)


def relocate_task_satellites(task_ids, *, project_id, team_id) -> None:
    """Carry the rows keyed to a task across the project boundary with it.

    Split from `relocate_tasks` for the endpoint that writes the moved
    task's own columns through a serializer and only needs this half.
    """
    task_ids = {int(task_id) for task_id in task_ids if task_id is not None}
    if not task_ids:
        return
    # Task notes denormalize the owning team and project, and the notes
    # tree is read with `team=<id>, project__in=<ids>` — so a note left
    # behind disappears from both projects' trees while still being
    # reachable from the task it hangs off.
    TaskNoteMaster.objects.filter(task_id__in=task_ids).update(
        project_id=project_id, team_id=team_id
    )
    _settle_dependencies(task_ids, team_id=team_id)


def _settle_dependencies(task_ids, *, team_id) -> None:
    """Keep dependency edges within one team after a move.

    `TaskDependency` allows cross-project edges but not cross-team ones
    (see the model), and denormalizes the team so listings can scope
    cheaply. A cross-TEAM move therefore invalidates every edge touching
    the moved rows: the surviving ones get the new team, and the ones that
    would now straddle two teams are dropped rather than left pointing at
    a task the other side can never load. Same-team moves — the common
    case — lose nothing.
    """
    touching = Q(blocker_task_id__in=task_ids) | Q(blocked_task_id__in=task_ids)
    edges = list(
        TaskDependency.objects.filter(touching).values("id", "blocker_task_id", "blocked_task_id")
    )
    if not edges:
        return
    endpoints = {edge["blocker_task_id"] for edge in edges} | {
        edge["blocked_task_id"] for edge in edges
    }
    teams = dict(
        TaskMaster.objects.filter(task_id__in=endpoints).values_list("task_id", "team_id")
    )
    straddling = [
        edge["id"]
        for edge in edges
        if str(teams.get(edge["blocker_task_id"])) != str(teams.get(edge["blocked_task_id"]))
    ]
    if straddling:
        logger.info(
            "task move dropped %d cross-team dependency edge(s) for tasks=%s",
            len(straddling),
            sorted(task_ids),
        )
        TaskDependency.objects.filter(id__in=straddling).delete()
    TaskDependency.objects.filter(touching).exclude(id__in=straddling).update(team_id=team_id)
