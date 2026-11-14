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
from typing import TYPE_CHECKING, Any, NamedTuple

from origin.models.task.task_models import TaskMaster

if TYPE_CHECKING:
    from origin.search_engine.agent.tools.base import ToolContext

CLOSED = "Closed"
DELETED = "Deleted"

# A branch deeper than any real tree is a bad link, not a task tree: stop short.
MAX_DEPTH = 200

# `(task_id, milestone_id, parent_task_id, is_milestone, status)`
COLUMNS = ("task_id", "milestone_id", "parent_task_id", "is_milestone", "status")


class Verdict(NamedTuple):
    """What an ancestor says about a task: `status` is Closed or Deleted, `ancestor_id` the
    nearest ancestor that said it."""

    status: str
    ancestor_id: int


def _parent_links(row, backing_of: dict[int, int]) -> list[int]:
    """The ids a row's walk goes up to, nearest first: its parent link, then (when it is not a
    milestone itself) the backing row of its milestone. `backing_of` maps a milestone id to the task
    id of its backing row."""
    _, milestone_id, parent_id, is_milestone, _status = row
    parents = [parent_id]
    if not is_milestone and milestone_id is not None:
        parents.append(backing_of.get(milestone_id))
    return [p for p in parents if p is not None]


def ancestor_verdicts(rows) -> dict[int, Verdict]:
    """`{task_id: Verdict}` for the rows under a Closed or Deleted ancestor.

    `rows` are `(task_id, milestone_id, parent_task_id, is_milestone, status)` tuples, the
    tasks AND their ancestors (a project's rows, whole). See the module docstring for the
    rule. Rows with no such ancestor have no entry.

    One plain walk up per row, with its own `seen` set (MIP-1426). The row itself starts in
    it, so its own status is never the reason, not even through a loop in the links. It is
    not a memoised recursion on purpose: a result computed while the loop guard had cut a
    walk short was remembered, so with a Closed task inside a loop of parent links the
    answer for the other members depended on which row came first (rows `1 -> 3 -> 5
    (Closed) -> 2 -> 1` gave four different answers over their 24 orders). The web client
    had the same flaw and the same fix (`closedByAncestor.ts`, genos-frontend #910).

    Order is the rule: ancestors are visited nearest first (a parent before what is above
    it, the parent link before the milestone link). The first Deleted wins outright (it
    beats Closed); otherwise the first Closed is named. A node reached twice (the parent
    link and the milestone link) is read once.
    """
    by_id = {r[0]: r for r in rows}
    backing_of = {r[1]: r[0] for r in rows if r[3] and r[1] is not None}

    def parents_of(row) -> list[int]:
        return _parent_links(row, backing_of)

    def verdict(task_id) -> Verdict | None:
        seen = {task_id}
        # (ancestor id, links from the task). The farther link is pushed first so the
        # nearer one is popped first.
        stack = [(p, 1) for p in reversed(parents_of(by_id[task_id]))]
        closed: Verdict | None = None
        while stack:
            ancestor_id, depth = stack.pop()
            if ancestor_id in seen or ancestor_id not in by_id or depth > MAX_DEPTH:
                continue
            seen.add(ancestor_id)
            ancestor = by_id[ancestor_id]
            if ancestor[4] == DELETED:
                return Verdict(DELETED, ancestor_id)
            if closed is None and ancestor[4] == CLOSED:
                closed = Verdict(CLOSED, ancestor_id)
            stack.extend((p, depth + 1) for p in reversed(parents_of(ancestor)))
        return closed

    return {task_id: v for task_id in by_id if (v := verdict(task_id)) is not None}


def descendant_ids(task_id: int, project_id: int) -> set[int]:
    """The ids of every task that has `task_id` among its ancestors, by the same two links the
    walk above goes up (a task's parent, and the backing row of its milestone): the tasks whose
    effective status can change when `task_id` is closed, deleted or reopened. One query (the
    project's narrow rows); `task_id` itself is not in the answer, and a loop in the links ends.

    Over-reporting is harmless for its callers (they re-derive from the rows); under-reporting is
    not, so a soft-deleted `task_id` still answers: its children still point at it."""
    rows = list(
        TaskMaster.objects.filter(
            project_id=project_id, is_deleted=False, is_init_task=False
        ).values_list(*COLUMNS)
    )
    backing_of = {r[1]: r[0] for r in rows if r[3] and r[1] is not None}
    children: dict[int, list[int]] = {}
    for row in rows:
        for parent in _parent_links(row, backing_of):
            children.setdefault(parent, []).append(row[0])
    found: set[int] = set()
    stack = [task_id]
    while stack:
        for child in children.get(stack.pop(), ()):
            if child != task_id and child not in found:
                found.add(child)
                stack.append(child)
    return found


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
            ).values_list(*COLUMNS)
        )
        if project_ids
        else {}
    )
    return {
        task_id: effective(own, verdicts.get(task_id) if project_id is not None else None)
        for task_id, project_id, own in asked
    }


def effective_fields(task_ids: Iterable[Any], ctx: "ToolContext") -> dict[int, dict[str, Any]]:
    """`{task_id: {effective_status, closed_by_ancestor?}}` for tasks a read tool is returning: the
    fields the MCP `get_task` / `list_tasks` carry, and the in-app tools carry behind
    `AGENT_CLOSED_ANCESTOR_RULE` (one implementation for both surfaces: MIP-1413, MIP-1478).

    `closed_by_ancestor` names the ancestor by `task_id`, and by `display_id` only when the
    caller may read that task (`LabelAccess`): a task's assignee who is not in its project can
    read the task, but not necessarily its grandparent. Ids that are not numbers are skipped."""
    # Imported here: `label_acl` imports `tools.base`, which runs the tools package's `__init__`,
    # which imports `ancestor_rule`, which imports this module.
    from origin.search_engine.agent.label_acl import LabelAccess  # noqa: PLC0415

    ids = []
    for raw in task_ids:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    found = effective_statuses(ids, ctx.team_id)
    ancestors = {e.closed_by_ancestor_id for e in found.values() if e.closed_by_ancestor_id}
    readable = LabelAccess(ctx).tasks(ancestors) if ancestors else {}
    out: dict[int, dict[str, Any]] = {}
    for task_id, e in found.items():
        fields: dict[str, Any] = {"effective_status": e.status}
        if e.closed_by_ancestor_id is not None:
            ancestor = readable.get(e.closed_by_ancestor_id)
            fields["closed_by_ancestor"] = {
                "task_id": e.closed_by_ancestor_id,
                "display_id": ancestor.display_id if ancestor else None,
            }
        out[task_id] = fields
    return out
