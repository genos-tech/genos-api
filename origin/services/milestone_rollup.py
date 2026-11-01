"""Which tasks count as a milestone's work (MIP-777).

A milestone's tasks are the ones that point at it (`milestone` FK) PLUS the
children of its backing row (`parent_task_id`), which the project table nests
under it. That predicate is the single definition behind the UI progress bar
(`_serialize_milestone`) and every agent/MCP rollup, so they reconcile.

Two things used to be counted that are not work:

  * The milestone's OWN backing row. It carries its milestone's id, so the FK
    branch matched it: a milestone planned with 20 tasks reported 21.
  * Tasks whose status is "Deleted". The task tree already hides them, and
    deleting a milestone sets that status, so they read as gone, not as
    finished. Counting them as closed made progress 37/49 where the live
    figure is 33/44.

`milestone_work_q` is the rollup predicate: neither of those. Callers that need
the full set (the milestone's own dependency edges, the order of work, the
"Deleted" row of a status breakdown) ask for it explicitly through
`milestone_tasks_q`.
"""

from __future__ import annotations

from django.db.models import Q


def milestone_tasks_q(milestone, *, own_row: bool = True, deleted: bool = True) -> Q:
    """Predicate for "tasks belonging to this milestone".

    `own_row=False` leaves out the milestone's backing task; `deleted=False`
    leaves out tasks whose status is "Deleted". The defaults keep the full set,
    as before. Soft-deleted rows (`is_deleted`) and the create form's scaffold
    rows are always excluded.
    """
    # The FK branch does not exclude `is_milestone`: only the milestone's own
    # row carries its id, so excluding that row by id is the precise cut.
    q = Q(milestone=milestone, is_deleted=False, is_init_task=False)
    if milestone.task_id is not None:
        q = q | Q(
            parent_task_id=milestone.task_id,
            is_deleted=False,
            is_init_task=False,
            is_milestone=False,
        )
        if not own_row:
            q = q & ~Q(task_id=milestone.task_id)
    if not deleted:
        q = q & ~Q(status="Deleted")
    return q


def milestone_work_q(milestone) -> Q:
    """The milestone's live work: what its progress and totals are counted over."""
    return milestone_tasks_q(milestone, own_row=False, deleted=False)
