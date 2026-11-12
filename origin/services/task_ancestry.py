"""A task under a Closed or Deleted ancestor reads as closed or deleted (MIP-1413).

Ken's rule (MIP-1353, MIP-1389): if a task's parent, grandparent, ... up to the root task or
its milestone is Closed (or Deleted), the task is treated as Closed (or Deleted), whatever
its own status says. The status itself is never changed: users do not update every
status, and reopening the parent must bring the branch back exactly as it was.

The rule lived in two places that had to agree:

  * the web client, everywhere (`utils/closedByAncestor.ts`), from the whole project list
    it already holds;
  * the milestone progress bar (`milestone_rollup`, genos-api #706).

Nothing else on the server could ask it, so a server-side reader (MCP `list_tasks`, a
report, a slim picker) either loaded the whole tree itself or got it wrong. This module is
the one walker; `milestone_rollup` calls it too.

## The rule

A task's ancestors are its `parent_task_id` chain and, when it is not a milestone itself,
the backing row of its milestone (the two links the client follows). Deleted beats Closed,
wherever each sits. A task's own status is never the reason. A parent that is not among the
rows (soft-deleted, a scaffold row, another project) ends the walk, and a cycle in the
links is survived.

`effective_status` is the status the rule reads: the ancestor's verdict when it changes the
reading, else the task's own status. `closed_by_ancestor_id` is set exactly when they
differ: the nearest ancestor that decided it (on the parent chain before the milestone row).

## Why it is computed, not stored

A stored flag must be rewritten for a whole subtree on every close, reopen, re-parent and
milestone move, inside the same transaction; a missed path leaves rows wrong for good
(MIP-1351 showed how a carried derived value strands). A walk over the narrow rows of the
projects involved is two queries and a dictionary pass.

## Where it is NOT used, on purpose

`getProjectTasks`, the web client's task list, is kept fresh by deltas (only changed rows).
Closing a parent changes no child row, so a server-computed field on the children would go
stale on every client that already holds them. The client's own walk over its full list is
the right tool there. This module answers reads that are computed fresh: the MCP task reads
(through `augment`, so the in-app agent sees no change until the December eval), and later
the slim picker (MIP-1353) and the reports (MIP-1300).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple

from origin.models.task.task_models import TaskMaster

CLOSED = "Closed"
DELETED = "Deleted"
_ENDING = (CLOSED, DELETED)

# A branch deeper than any real tree is a bad link, not a task tree: stop short.
MAX_DEPTH = 200

# `(task_id, milestone_id, parent_task_id, is_milestone, status)`
_COLUMNS = ("task_id", "milestone_id", "parent_task_id", "is_milestone", "status")


class Verdict(NamedTuple):
    """What an ancestor says about a task: `status` is Closed or Deleted, `ancestor_id` the
    nearest ancestor that said it."""

    status: str
    ancestor_id: int


def ancestor_verdicts(rows) -> dict[int, Verdict]:
    """`{task_id: Verdict}` for the rows under a Closed or Deleted ancestor.

    `rows` are `(task_id, milestone_id, parent_task_id, is_milestone, status)` tuples, the
    tasks AND their ancestors (a project's rows, whole). See the module docstring for the
    rule. Rows with no such ancestor have no entry.
    """
    by_id = {r[0]: r for r in rows}
    backing_of = {r[1]: r[0] for r in rows if r[3] and r[1] is not None}
    memo: dict[int, Verdict | None] = {}
    visiting: set[int] = set()

    def verdict(task_id, depth=0) -> Verdict | None:
        if task_id in memo:
            return memo[task_id]
        if task_id in visiting or task_id not in by_id or depth > MAX_DEPTH:
            return None
        visiting.add(task_id)
        _, milestone_id, parent_id, is_milestone, _status = by_id[task_id]
        found: Verdict | None = None
        parents = [parent_id]
        if not is_milestone and milestone_id is not None:
            parents.append(backing_of.get(milestone_id))
        for parent in parents:
            if parent is None or parent not in by_id:
                continue
            own = by_id[parent][4]
            for candidate in (
                Verdict(own, parent) if own in _ENDING else None,
                verdict(parent, depth + 1),
            ):
                if candidate is None:
                    continue
                # Deleted beats Closed; the first one found (the nearest, on the parent
                # chain before the milestone row) names the ancestor.
                if candidate.status == DELETED and (found is None or found.status != DELETED):
                    found = candidate
                elif candidate.status == CLOSED and found is None:
                    found = candidate
        visiting.discard(task_id)
        memo[task_id] = found
        return found

    return {task_id: v for task_id in by_id if (v := verdict(task_id)) is not None}


class Effective(NamedTuple):
    """The status the rule reads, and the ancestor that decided it (None when it is the
    task's own status)."""

    status: str
    closed_by_ancestor_id: int | None


def effective(own_status: str, found: Verdict | None) -> Effective:
    """The status the rule reads for a task whose own status is `own_status`."""
    if found is None or own_status == DELETED or own_status == found.status:
        return Effective(own_status, None)
    # Closed under a Deleted ancestor reads Deleted; anything else under an ancestor reads
    # the ancestor's verdict.
    return Effective(found.status, found.ancestor_id)


def effective_statuses(task_ids: Iterable[int], team_id) -> dict[int, Effective]:
    """`{task_id: Effective}` for the given tasks of `team_id`, in two queries.

    The tasks' projects are loaded whole (five narrow columns), so every ancestor is at
    hand. Unknown ids, other teams' tasks and soft-deleted rows have no entry. A task with no
    project has only its own status (it has no tree to read).

    Access is the CALLER's business: this reads statuses, it does not decide who may see a
    task. Call it with the ids a read tool already returned.
    """
    ids = {int(i) for i in task_ids if i is not None}
    if not ids:
        return {}
    asked = list(
        TaskMaster.objects.filter(
            task_id__in=ids, team_id=team_id, is_deleted=False, is_init_task=False
        ).values_list("task_id", "project_id", "status")
    )
    project_ids = {p for _, p, _ in asked if p is not None}
    verdicts = (
        ancestor_verdicts(
            TaskMaster.objects.filter(
                project_id__in=project_ids, is_deleted=False, is_init_task=False
            ).values_list(*_COLUMNS)
        )
        if project_ids
        else {}
    )
    return {
        task_id: effective(own, verdicts.get(task_id) if project_id is not None else None)
        for task_id, project_id, own in asked
    }
