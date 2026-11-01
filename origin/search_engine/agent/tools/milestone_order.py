"""Where to start a milestone (MIP-782): ready-now, blocked, critical path.

Open work falls into one list: in progress (WIP), blocked (an open blocker, or
status Blocked), pending (status Pending and nothing blocks it: it waits on
someone, so it is not offered as a place to start), or ready now.

`get_milestone_summary` used to return counts only, so a reader of a
20-task milestone with 18 dependency edges could not tell which task to take
first. This reads the dependency edges and answers it.

A task is blocked by an OPEN blocker of its own, or of any open ancestor up
to and including the milestone's backing task: a sub-task of a blocked
parent is not ready even though its own status says Open. A blocker is open
unless it is Closed, Deleted or soft-deleted (`task_blocking.CLOSED_STATUSES`,
the rule that drives the automatic Blocked status).

The critical path is the longest chain of still-open milestone tasks along
those same edges, counted in tasks. It is only reported when it is a chain
(two or more tasks): with no edges every task is a "path of one" and naming
one would mislead.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.tools.list_tasks import _milestone_task_q
from origin.search_engine.agent.tools.task_enums import blank_to_none
from origin.services.task_blocking import CLOSED_STATUSES

# Rows listed per group; the counts beside them are always complete.
_MAX_ROWS = 50
# Ancestors are walked this far, which is far deeper than the UI nests.
_MAX_DEPTH = 10


def _is_open(task: TaskMaster) -> bool:
    return task.status not in CLOSED_STATUSES and not task.is_deleted


def _ref(task: TaskMaster) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "title": task.title or "",
        "status": task.status or "",
    }


def _row(task: TaskMaster) -> dict[str, Any]:
    return {
        **_ref(task),
        "priority": blank_to_none(task.priority),
        "effort_level": blank_to_none(task.effort_level),
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
        "parent_task_id": task.parent_task_id,
    }


def _load_with_ancestors(milestone: MilestoneMaster) -> dict[int, TaskMaster]:
    """The milestone's tasks (every status) plus the ancestors they hang from."""
    tasks = {
        t.task_id: t
        for t in TaskMaster.objects.filter(_milestone_task_q(milestone))
        .select_related("project")
        .distinct()
    }
    for _ in range(_MAX_DEPTH):
        missing = {
            t.parent_task_id
            for t in tasks.values()
            if t.parent_task_id is not None and t.parent_task_id not in tasks
        }
        if not missing:
            break
        for t in TaskMaster.objects.filter(task_id__in=missing).select_related("project"):
            tasks[t.task_id] = t
    return tasks


def _ancestors(task: TaskMaster, tasks: dict[int, TaskMaster]) -> list[TaskMaster]:
    """Open ancestors, nearest first. A closed one blocks nothing."""
    out, seen, cur = [], {task.task_id}, task
    for _ in range(_MAX_DEPTH):
        parent = tasks.get(cur.parent_task_id) if cur.parent_task_id is not None else None
        if parent is None or parent.task_id in seen:
            break
        seen.add(parent.task_id)
        if _is_open(parent):
            out.append(parent)
        cur = parent
    return out


def _critical_path(
    nodes: set[int], preds: dict[int, set[int]], tasks: dict[int, TaskMaster]
) -> tuple[list[TaskMaster], bool]:
    """Longest chain through `preds` (blocked -> its blockers) among `nodes`.

    Kahn's order, so a dependency cycle leaves its members unprocessed rather
    than looping; the second value says whether that happened.
    """
    succs: dict[int, set[int]] = defaultdict(set)
    indeg = {n: 0 for n in nodes}
    for n in nodes:
        for p in preds.get(n, ()):
            if p in nodes:
                succs[p].add(n)
                indeg[n] += 1
    queue = deque(sorted(n for n, d in indeg.items() if d == 0))
    best: dict[int, int] = {}
    prev: dict[int, int | None] = {}
    done = 0
    while queue:
        n = queue.popleft()
        done += 1
        # Lowest id first among equals, so the answer does not wobble.
        ins = sorted(p for p in preds.get(n, ()) if p in best)
        top = max(ins, key=lambda p: (best[p], -p), default=None)
        best[n] = 1 + (best[top] if top is not None else 0)
        prev[n] = top
        for s in sorted(succs[n]):
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    has_cycle = done < len(nodes)
    if not best:
        return [], has_cycle
    end = max(best, key=lambda n: (best[n], -n))
    chain = []
    cur: int | None = end
    while cur is not None:
        chain.append(tasks[cur])
        cur = prev[cur]
    chain.reverse()
    return chain, has_cycle


def order_of_work(milestone: MilestoneMaster, member_project_ids: set[int]) -> dict[str, Any]:
    tasks = _load_with_ancestors(milestone)
    backing_id = milestone.task_id
    # The work: open tasks of the milestone, not the milestone's own row.
    work = {
        tid: t
        for tid, t in tasks.items()
        if tid != backing_id
        and _is_open(t)
        and not t.is_init_task
        and (t.milestone_id == milestone.milestone_id or t.parent_task_id == backing_id)
    }

    # Every edge that touches a task of the work or one of its ancestors.
    holders = set(work)
    for t in work.values():
        holders.update(a.task_id for a in _ancestors(t, tasks))
    open_blockers: dict[int, list[TaskDependency]] = defaultdict(list)
    for dep in (
        TaskDependency.objects.filter(blocked_task_id__in=holders)
        .select_related("blocker_task", "blocker_task__project")
        .order_by("id")
    ):
        if _is_open(dep.blocker_task):
            open_blockers[dep.blocked_task_id].append(dep)

    ready, blocked, in_progress, pending = [], [], [], []
    preds: dict[int, set[int]] = defaultdict(set)
    for tid, t in sorted(work.items()):
        sources = [(t, "self"), *((a, a) for a in _ancestors(t, tasks))]
        blockers, redacted = [], 0
        for holder, via in sources:
            for dep in open_blockers.get(holder.task_id, ()):
                b = dep.blocker_task
                preds[tid].add(b.task_id)
                if b.project_id not in member_project_ids:
                    # Blocks, but the caller may not see what it is.
                    redacted += 1
                    continue
                entry = _ref(b)
                if via != "self":
                    entry["inherited_from"] = _ref(via)
                blockers.append(entry)
        if t.status == "WIP":
            in_progress.append(_row(t))
        elif blockers or redacted or t.status == "Blocked":
            row = _row(t)
            row["open_blockers"] = blockers
            if redacted:
                row["redacted_blockers"] = redacted
            if not blockers and not redacted:
                row["note"] = "Status is Blocked but no open task blocks it (set by hand)."
            blocked.append(row)
        elif t.status == "Pending":
            # Pending means waiting on someone, not startable (MIP-851).
            pending.append(_row(t))
        else:
            ready.append(_row(t))

    chain, has_cycle = _critical_path(set(work), preds, tasks)
    out: dict[str, Any] = {
        "ready_now": ready[:_MAX_ROWS],
        "ready_now_count": len(ready),
        "in_progress": in_progress[:_MAX_ROWS],
        "in_progress_count": len(in_progress),
        "pending": pending[:_MAX_ROWS],
        "pending_count": len(pending),
        "blocked": blocked[:_MAX_ROWS],
        "blocked_count": len(blocked),
        "critical_path": [_ref(t) for t in chain] if len(chain) >= 2 else [],
    }
    if has_cycle:
        out["dependency_cycle"] = True
    return out
