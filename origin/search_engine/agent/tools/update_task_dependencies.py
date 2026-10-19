"""`update_task_dependencies` write tool — link and unlink EXISTING tasks.

`create_task_plan` can only wire `blocked_by_indexes` between the tasks
it is creating, so there was no way for the agent (or an MCP client) to
say "MIP-618 blocks MIP-619" about two tasks that already exist. This
tool adds and removes blocker→blocked edges in ONE approval: `add` and
`remove` are both lists of `{blocker_task_id, blocked_task_id}` pairs,
so flipping an edge's direction is one call (remove + add), not two.

Rules — the UI's `TaskDependencyView.post`, plus the access check that
view does not make:
  * no self-dependency;
  * both tasks exist, are not deleted, and sit in the caller's team
    (cross-project is fine, cross-team never);
  * the caller can read BOTH tasks (`task_acl_user_ids`, the same set
    `fetch_task` / `assign_task` enforce) — linking reveals one task's
    title to whoever can see the other, so access to one end is not
    enough;
  * no duplicate edge, and no added edge that closes a cycle in the
    graph as it will be after the whole batch (a reverse edge is just
    the 2-cycle case).

Removing an edge that does not exist is reported, not an error — the
caller wanted it gone and it is. Semantics are update_tasks_bulk's:
validate everything, then apply everything in one transaction; one bad
entry rejects the batch. Rows are written with `objects.create`, not
`bulk_create`, so the dependency signals re-derive the Blocked status.
"""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.db.models import Q

from apis.request_id import log_failure
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.services.task_cache import invalidate_project_tasks_cache

_MAX_EDGES = 30


def _parse_pairs(raw: Any, key: str, errors: list[str]) -> list[tuple[int, int]]:
    """`[{blocker_task_id, blocked_task_id}, …]` → int pairs, deduplicated."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"`{key}` must be an array.")
        return []
    pairs: list[tuple[int, int]] = []
    for i, row in enumerate(raw):
        where = f"{key}[{i}]"
        if not isinstance(row, dict):
            errors.append(f"{where}: must be an object.")
            continue
        try:
            blocker = int(row.get("blocker_task_id"))
            blocked = int(row.get("blocked_task_id"))
        except (TypeError, ValueError):
            errors.append(f"{where}: `blocker_task_id` and `blocked_task_id` must be task ids.")
            continue
        if blocker == blocked:
            errors.append(f"{where}: a task cannot block itself (task {blocker}).")
            continue
        if (blocker, blocked) in pairs:
            errors.append(f"{where}: the same edge is listed twice.")
            continue
        pairs.append((blocker, blocked))
    return pairs


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    errors: list[str] = []
    to_add = _parse_pairs(args.get("add"), "add", errors)
    to_remove = _parse_pairs(args.get("remove"), "remove", errors)
    if not errors and not to_add and not to_remove:
        raise ToolError("Pass at least one edge in `add` or `remove`.")
    if len(to_add) + len(to_remove) > _MAX_EDGES:
        raise ToolError(
            f"Too many edges: {len(to_add) + len(to_remove)} (max {_MAX_EDGES}). "
            "Split the batch and tell the user."
        )
    task_ids = {tid for pair in to_add + to_remove for tid in pair}
    tasks_by_id = {
        t.task_id: t
        for t in TaskMaster.objects.select_related("project").filter(task_id__in=task_ids)
    }
    # Set-based form of `task_acl_user_ids`, as in update_tasks_bulk: one
    # membership query for the caller rather than one per task.
    member_project_ids = set(
        ProjectMembers.objects.filter(
            attendee_id=ctx.user_id,
            project__team_id=ctx.team_id,
            project__is_deleted=False,
        ).values_list("project_id", flat=True)
    )

    def _visible(task: TaskMaster | None) -> bool:
        # Not found, other team and no access all read the same way, so
        # an error can't be used to probe which ids exist elsewhere.
        return (
            task is not None
            and not task.is_deleted
            and str(getattr(task, "team_id", "") or "") == ctx.team_id
            and (
                task.project_id in member_project_ids
                or str(task.assignee_id or "") == ctx.user_id
                or str(task.reporter_id or "") == ctx.user_id
            )
        )

    for key, pairs in (("add", to_add), ("remove", to_remove)):
        for blocker, blocked in pairs:
            for tid in (blocker, blocked):
                if not _visible(tasks_by_id.get(tid)):
                    errors.append(f"{key} {blocker} -> {blocked}: task {tid} not found.")
                    break
    if errors:
        raise ToolError(
            "Dependency update validation failed (nothing was applied):\n- "
            + "\n- ".join(errors[:25])
        )

    def _label(tid: int) -> str:
        task = tasks_by_id.get(tid)
        return (task.display_id or str(tid)) if task is not None else str(tid)

    for blocker, blocked in set(to_add) & set(to_remove):
        errors.append(f"edge {_label(blocker)} -> {_label(blocked)} is in both `add` and `remove`.")

    removing = set(to_remove)
    adding = set(to_add)
    duplicates: set[tuple[int, int]] = set()
    if to_add:
        pair_q = Q()
        for blocker, blocked in to_add:
            pair_q |= Q(blocker_task_id=blocker, blocked_task_id=blocked)
        duplicates = set(
            TaskDependency.objects.filter(pair_q).values_list("blocker_task_id", "blocked_task_id")
        )
    for blocker, blocked in to_add:
        if (blocker, blocked) in duplicates:
            errors.append(
                f"add {_label(blocker)} -> {_label(blocked)}: this dependency already exists."
            )

    # An added edge A→B (A blocks B) closes a cycle iff A is already
    # downstream of B, in the graph as it will be after the batch. Only
    # added edges are checked: the UI refuses just the 2-cycle, so a
    # longer cycle may already exist, and refusing every batch near it
    # would also refuse the removal that breaks it.
    downstream: dict[int, set[int]] = {}

    def _blocked_by(tids: set[int]) -> None:
        missing = tids - downstream.keys()
        if not missing:
            return
        for tid in missing:
            downstream[tid] = set()
        rows = TaskDependency.objects.filter(
            team_id=ctx.team_id, blocker_task_id__in=missing
        ).values_list("blocker_task_id", "blocked_task_id")
        for blocker, blocked in rows:
            if (blocker, blocked) not in removing:
                downstream[blocker].add(blocked)
        for blocker, blocked in adding:
            if blocker in missing:
                downstream[blocker].add(blocked)

    for blocker, blocked in to_add:
        # Breadth-first from `blocked`; `parent` doubles as the visited set
        # and lets the cycle be spelled out for the model.
        parent: dict[int, int | None] = {blocked: None}
        frontier = {blocked}
        while frontier and blocker not in parent:
            _blocked_by(frontier)
            nxt: set[int] = set()
            for tid in frontier:
                for child in downstream[tid]:
                    if child not in parent:
                        parent[child] = tid
                        nxt.add(child)
            frontier = nxt
        if blocker in parent:
            chain = [blocker]
            while (up := parent[chain[-1]]) is not None:
                chain.append(up)
            chain.reverse()  # blocked -> … -> blocker
            # The path can run through tasks outside the batch; name the
            # ones the caller may see, and only those.
            for task in TaskMaster.objects.select_related("project").filter(
                task_id__in=set(chain) - tasks_by_id.keys()
            ):
                if _visible(task):
                    tasks_by_id[task.task_id] = task
            names = [
                _label(t) if t in tasks_by_id else "(a task you can't see)"
                for t in [*chain, blocked]
            ]
            errors.append(
                f"add {_label(blocker)} -> {_label(blocked)}: would create a dependency "
                "cycle (" + " -> ".join(names) + ", each blocking the next)."
            )
    if errors:
        raise ToolError(
            "Dependency update validation failed (nothing was applied):\n- "
            + "\n- ".join(errors[:25])
        )

    def _edge(blocker: int, blocked: int) -> dict[str, Any]:
        a, b = tasks_by_id[blocker], tasks_by_id[blocked]
        return {
            "blocker_task_id": blocker,
            "blocker_display_id": a.display_id,
            "blocker_title": a.title,
            "blocked_task_id": blocked,
            "blocked_display_id": b.display_id,
            "blocked_title": b.title,
        }

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    not_found: list[dict[str, Any]] = []
    try:
        with transaction.atomic():
            for blocker, blocked in to_remove:
                # Both ends were checked to be in this team above, so the
                # edge is too, whatever its (SET_NULL-able) team column says.
                # A queryset delete still sends post_delete per row (there
                # is a receiver), so the blocked task leaves "Blocked".
                deleted, _ = TaskDependency.objects.filter(
                    blocker_task_id=blocker, blocked_task_id=blocked
                ).delete()
                (removed if deleted else not_found).append(_edge(blocker, blocked))
            for blocker, blocked in to_add:
                TaskDependency.objects.create(
                    blocker_task=tasks_by_id[blocker],
                    blocked_task=tasks_by_id[blocked],
                    team_id=ctx.team_id,
                    created_by_id=ctx.user_id,
                )
                added.append(_edge(blocker, blocked))
    except Exception as e:  # noqa: BLE001 — surface as ToolError for the model
        # The model and the MCP caller read this; the database error
        # goes to the log with a reference (MIP-622).
        ref = log_failure("update_task_dependencies tool failed", e)
        raise ToolError(f"Dependency update failed (nothing was applied) (ref {ref}).")

    # A Blocked/Open flip changes the project table's rows.
    for pid in {tasks_by_id[t].project_id for t in task_ids}:
        invalidate_project_tasks_cache(ctx.team_id, pid)

    parts = []
    if added:
        parts.append(f"added {len(added)}")
    if removed:
        parts.append(f"removed {len(removed)}")
    if not_found:
        parts.append(f"{len(not_found)} to remove did not exist")
    return {
        "added": added,
        "removed": removed,
        "not_found": not_found,
        "__summary__": "Dependencies updated: " + ", ".join(parts),
    }


_EDGE_ITEMS = {
    "type": "OBJECT",
    "properties": {
        "blocker_task_id": {
            "type": "INTEGER",
            "description": "The task that must finish first (numeric id, from list_tasks).",
        },
        "blocked_task_id": {
            "type": "INTEGER",
            "description": "The task that waits on it (numeric id, from list_tasks).",
        },
    },
    "required": ["blocker_task_id", "blocked_task_id"],
}

UPDATE_TASK_DEPENDENCIES = Tool(
    name="update_task_dependencies",
    description=(
        "Add or remove blocker→blocked dependencies between EXISTING tasks "
        "(or milestones, via their backing task_id) in ONE approval. "
        "REQUIRES USER APPROVAL. Use it for 'X blocks Y', 'Y depends on X', "
        "'X must be done before Y', or to undo one; create_task_plan "
        "remains correct for tasks you are creating. Read the current graph "
        "first (list_task_dependencies) so you only propose real changes. "
        "Flip an edge's direction by removing it and adding the reverse in "
        "the same call. Both tasks must be in this team; cross-project is "
        "fine. All-or-nothing: a self-dependency, a duplicate, a task you "
        "can't see, or an edge that would create a cycle rejects the whole "
        "batch. A task with an unfinished blocker turns Blocked "
        "automatically."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "add": {
                "type": "ARRAY",
                "description": f"Edges to create. Up to {_MAX_EDGES} edges in add + remove.",
                "items": _EDGE_ITEMS,
            },
            "remove": {
                "type": "ARRAY",
                "description": "Edges to delete, named the same way.",
                "items": _EDGE_ITEMS,
            },
        },
        "required": [],
    },
    run=_run,
    requires_approval=True,
)
