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

from collections import defaultdict

from django.db.models import Q

from origin.models.task.task_models import TaskMaster


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


_MAX_ANCESTOR_DEPTH = 200


def _ancestor_verdicts(rows) -> dict[int, str]:
    """`{task_id: "Closed" | "Deleted"}` for the rows under a Closed or Deleted ancestor.

    `rows` are `(task_id, milestone_id, parent_task_id, is_milestone, status)`. A row's
    ancestors are its `parent_task_id` chain and, when it is not a milestone itself, the
    backing row of its milestone (the same two links the table follows on the client:
    `closedByAncestor.ts`). Deleted beats Closed, a row's own status is never the reason,
    a parent that is not in `rows` ends the walk, and a cycle in the links is survived.
    """
    by_id = {r[0]: r for r in rows}
    backing_of = {r[1]: r[0] for r in rows if r[3] and r[1] is not None}
    memo: dict[int, str | None] = {}
    visiting: set[int] = set()

    def verdict(task_id, depth=0):
        if task_id in memo:
            return memo[task_id]
        # A branch deeper than any real tree is a bad link, not a task tree: stop short.
        if task_id in visiting or task_id not in by_id or depth > _MAX_ANCESTOR_DEPTH:
            return None
        visiting.add(task_id)
        _, milestone_id, parent_id, is_milestone, _status = by_id[task_id]
        found = None
        parents = [parent_id]
        if not is_milestone and milestone_id is not None:
            parents.append(backing_of.get(milestone_id))
        for parent in parents:
            if parent is None or parent not in by_id:
                continue
            for candidate in (
                by_id[parent][4] if by_id[parent][4] in ("Closed", "Deleted") else None,
                verdict(parent, depth + 1),
            ):
                if candidate == "Deleted" or (candidate == "Closed" and found is None):
                    found = candidate
        visiting.discard(task_id)
        memo[task_id] = found
        return found

    return {task_id: v for task_id in by_id if (v := verdict(task_id)) is not None}


def milestone_work_counts(milestones, *, ancestor_rule: bool = False) -> dict[int, tuple[int, int]]:
    """`{milestone_id: (total, closed)}` for many milestones, in ONE query.

    The numbers `milestone_work_q` gives one milestone at a time (two COUNTs
    each), for a list that would otherwise cost two round trips per milestone.
    Every milestone passed in has an entry, `(0, 0)` when it has no work.

    This has to be one query over the rows rather than a GROUP BY: a task can
    be the work of one milestone through its FK and of ANOTHER through its
    parent, and a GROUP BY on `milestone_id` would only ever see the first. So
    the candidate rows are loaded (five narrow columns) and each is sorted into
    the milestones it belongs to here, by the same rules as `milestone_work_q`:

      * through the FK (`milestone_id`), or through the parent (`parent_task_id`
        is the milestone's backing row, and the task is not itself a milestone);
      * counted once per milestone, even when it matches both ways;
      * never the milestone's own backing row;
      * never a soft-deleted row, a create-form scaffold row, or a task whose
        status is "Deleted".

    A milestone without a backing row yet (legacy) counts through its FK only.
    Callers that need the full set of a single milestone still use
    `milestone_tasks_q`; `test_milestone_list_queries` keeps the two in step.

    `ancestor_rule=True` is the progress bar's own reading (MIP-1382, MIP-1389), the one
    the task table, the board and the dashboard apply on the client: a task under a
    Closed ancestor counts as closed, one under a Deleted ancestor is not counted,
    whatever its own status says (users do not update every status). A Closed milestone
    reads N/N. Still ONE query: the milestones' projects are loaded whole (five narrow
    columns) so the ancestors are at hand. The agent/MCP rollups keep
    `ancestor_rule=False` until the December eval (what the model sees must not move).
    """
    by_id = {m.milestone_id: m for m in milestones}
    counts = {milestone_id: [0, 0] for milestone_id in by_id}
    if not by_id:
        return {}
    by_backing = defaultdict(list)
    for m in by_id.values():
        if m.task_id is not None:
            by_backing[m.task_id].append(m.milestone_id)

    matches = Q(milestone_id__in=list(by_id))
    if by_backing:
        matches |= Q(parent_task_id__in=list(by_backing))
    rows = TaskMaster.objects.filter(is_deleted=False, is_init_task=False)
    if ancestor_rule:
        # The candidates AND their ancestors: the milestones' projects, whole. A
        # Deleted row stays in, it may be the ancestor that takes its branch with it.
        project_ids = {m.project_id for m in by_id.values() if m.project_id is not None}
        rows = rows.filter(matches | Q(project_id__in=project_ids))
    else:
        rows = rows.filter(matches).exclude(status="Deleted")
    rows = list(
        rows.values_list("task_id", "milestone_id", "parent_task_id", "is_milestone", "status")
    )
    verdicts = _ancestor_verdicts(rows) if ancestor_rule else {}
    for task_id, milestone_id, parent_task_id, is_milestone, task_status in rows:
        verdict = verdicts.get(task_id)
        if task_status == "Deleted" or verdict == "Deleted":
            continue  # gone, not finished (only reachable with `ancestor_rule`)
        owners = set()
        if milestone_id in counts:
            owners.add(milestone_id)
        if parent_task_id is not None and not is_milestone:
            owners.update(by_backing.get(parent_task_id, ()))
        for owner in owners:
            if task_id == by_id[owner].task_id:
                continue  # the milestone's own backing row is not its work
            counts[owner][0] += 1
            if (
                task_status == "Closed"
                or verdict == "Closed"
                # a milestone without a backing row yet has no ancestor to read
                or (ancestor_rule and by_id[owner].status == "Closed")
            ):
                counts[owner][1] += 1
    return {milestone_id: (total, closed) for milestone_id, (total, closed) in counts.items()}
