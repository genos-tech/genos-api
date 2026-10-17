"""`get_my_focus_tasks` tool — prioritised "what to look at next".

Returns the caller's most-important active tasks, sorted with the same
comparator the dashboard's "Up Next" card uses
(`genos-frontend/src/features/tasks/components/dashboard/TaskHomeContent.tsx`
lines 523-558). Sorting tiers, in order:

  1. Overdue float to top — tasks past due_date come first; among the
     overdue group, more-overdue first (earliest due_date wins).
  2. Priority rank — Critical(0) → High(1) → Normal(2) → Low(3) →
     Minimal(4) → none(5).
  3. Soonest due_date — no due_date sorts last (Infinity).
  4. Most recently updated — newest `ts_updated_at` wins the final tie.

The Python comparator below mirrors that comparator chain verbatim;
matching it exactly is the contract — diverging would make the agent
say "look at A first" while the dashboard says "look at B first".

ACL contract:
  * Tenant guard: ctx.team_id.
  * Scope: `assignee_id == ctx.user_id` AND the task's project is in
    ctx.user_id's `ProjectMembers` set.
  * `status NOT IN ("Closed", "Deleted")` — focus is on actionable work.

`delivery` filter (MIP-687), read from `TaskDelivery`:
  * `open_pr` — a PR is open for the task: "what is in flight".
  * `merged_pr` — a PR merged and the task is still not Closed (Closed is
    already excluded above): the "merged but nobody closed it" check.
  * `no_pr` — no PR recorded at all, open or merged.
A row whose PR state is `unknown` (backfilled, or linked without GitHub)
counts as neither open nor merged — it is excluded from both rather than
guessed into one — but it does count as a PR for `no_pr`.
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import Any

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.services.user_time import today_for_user_id

_MAX_LIMIT = 20
_DEFAULT_LIMIT = 5
_PRIORITY_RANK = {
    "Critical": 0,
    "High": 1,
    "Normal": 2,
    "Low": 3,
    "Minimal": 4,
}
_INACTIVE_STATUSES = ["Closed", "Deleted"]
_DELIVERY_FILTERS = ("open_pr", "merged_pr", "no_pr")


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        limit = int(args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        raise ToolError(f"`limit` must be an integer (got {args.get('limit')!r}).")
    limit = max(1, min(limit, _MAX_LIMIT))
    delivery = args.get("delivery") or None
    if delivery is not None and delivery not in _DELIVERY_FILTERS:
        raise ToolError(f"`delivery` must be one of {list(_DELIVERY_FILTERS)} (got {delivery!r}).")

    member_project_ids = set(
        ProjectMembers.objects.filter(
            attendee_id=ctx.user_id,
            project__team_id=ctx.team_id,
            project__is_deleted=False,
        ).values_list("project_id", flat=True)
    )

    today = today_for_user_id(ctx.user_id)

    # Fetch a bounded pool, then sort in Python with the exact comparator
    # chain the dashboard uses. We over-fetch (limit * 5, capped at 100)
    # so a project with hundreds of tasks doesn't push the lowest-priority
    # rows out of view before the sort runs.
    pool_cap = min(max(limit * 5, 50), 100)
    qs = (
        TaskMaster.objects.filter(
            team_id=ctx.team_id,
            assignee_id=ctx.user_id,
            project_id__in=member_project_ids,
            is_deleted=False,
            is_init_task=False,
        )
        .exclude(status__in=_INACTIVE_STATUSES)
        .select_related("project")
    )
    # Filtered in SQL, before the pool is cut, so a filter never comes
    # back short because its tasks fell outside the newest-100.
    if delivery == "open_pr":
        qs = qs.filter(deliveries__state=TaskDeliveryState.OPEN).distinct()
    elif delivery == "merged_pr":
        qs = qs.filter(deliveries__state=TaskDeliveryState.MERGED).distinct()
    elif delivery == "no_pr":
        qs = qs.exclude(deliveries__pr_url__isnull=False)
    rows = list(qs.order_by("-ts_updated_at")[:pool_cap])

    none_rank = max(_PRIORITY_RANK.values()) + 1

    def _compare(a: TaskMaster, b: TaskMaster) -> int:
        # Stage 1 — overdue first; among overdue rows, earliest due wins.
        a_overdue = a.due_date is not None and a.due_date < today
        b_overdue = b.due_date is not None and b.due_date < today
        if a_overdue != b_overdue:
            return -1 if a_overdue else 1
        if a_overdue and b_overdue:
            # both overdue — earlier due_date comes first
            if a.due_date != b.due_date:
                return -1 if a.due_date < b.due_date else 1
        # Stage 2 — priority rank (Critical=0 → Minimal=4 → none=5)
        pa = _PRIORITY_RANK.get(a.priority or "", none_rank)
        pb = _PRIORITY_RANK.get(b.priority or "", none_rank)
        if pa != pb:
            return -1 if pa < pb else 1
        # Stage 3 — soonest due_date, with None sorting last.
        if a.due_date is None and b.due_date is None:
            pass
        elif a.due_date is None:
            return 1
        elif b.due_date is None:
            return -1
        elif a.due_date != b.due_date:
            return -1 if a.due_date < b.due_date else 1
        # Stage 4 — most recently updated wins the final tie.
        au = a.ts_updated_at
        bu = b.ts_updated_at
        if au != bu:
            return -1 if au > bu else 1
        return 0

    rows.sort(key=cmp_to_key(_compare))
    rows = rows[:limit]

    prs_by_task: dict[int, list[dict[str, Any]]] = {}
    if delivery in ("open_pr", "merged_pr"):
        # The PRs that put each task in the list, so "what is in flight"
        # answers with the PR too and needs no second call per task.
        wanted = TaskDeliveryState.OPEN if delivery == "open_pr" else TaskDeliveryState.MERGED
        for d in TaskDelivery.objects.filter(
            task_id__in=[t.task_id for t in rows], state=wanted
        ).order_by("delivery_id"):
            prs_by_task.setdefault(d.task_id, []).append(
                {
                    "pr_url": d.pr_url,
                    "branch": d.branch or None,
                    "merged_at": d.merged_at.isoformat() if d.merged_at else None,
                }
            )

    tasks: list[dict[str, Any]] = []
    for t in rows:
        is_overdue = t.due_date is not None and t.due_date < today
        days_until_due: int | None = None
        if t.due_date is not None:
            days_until_due = (t.due_date - today).days
        row = {
            "task_id": t.task_id,
            "display_id": t.display_id,
            "title": t.title,
            "status": t.status,
            "priority": t.priority,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "is_overdue": is_overdue,
            "days_until_due": days_until_due,
            "project_id": t.project_id,
            "project_name": t.project.project_name if t.project else None,
        }
        if delivery in ("open_pr", "merged_pr"):
            row["prs"] = prs_by_task.get(t.task_id, [])
        tasks.append(row)

    if tasks:
        head = ", ".join(f"{t['display_id']} {t['title']}" for t in tasks[:3])
        summary = f"Top {len(tasks)} to focus on: {head}" + (" …" if len(tasks) > 3 else "")
    elif delivery == "open_pr":
        summary = "None of your active tasks has an open PR."
    elif delivery == "merged_pr":
        summary = "None of your active tasks has a merged PR — nothing waiting to be closed."
    elif delivery == "no_pr":
        summary = "You have no active task without a PR."
    else:
        summary = "No active tasks assigned to you."

    return {
        "tasks": tasks,
        "__summary__": summary,
    }


GET_MY_FOCUS_TASKS = Tool(
    name="get_my_focus_tasks",
    description=(
        "Prioritised list of the current user's active tasks, ranked by "
        "the same comparator the dashboard's 'Up Next' card uses: "
        "overdue first → priority (Critical → High → Normal → Low → "
        "Minimal → none) → soonest due_date → most recently updated. "
        "Use for 'what should I work on first?', 'today's priorities', "
        "'what's most important right now?'. Returns up to `limit` "
        "tasks (default 5), each with is_overdue, days_until_due, and "
        "project info. For aggregate counts instead, use "
        "`get_my_task_summary`. `delivery` narrows to tasks by their "
        "GitHub PRs: 'open_pr' (in flight), 'merged_pr' (merged but the "
        "task is still not Closed — usually one to close), 'no_pr'."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "limit": {
                "type": "INTEGER",
                "description": (
                    f"Max tasks to return (1–{_MAX_LIMIT}). Default "
                    f"{_DEFAULT_LIMIT} — matches the dashboard's top-5 "
                    "view."
                ),
            },
            "delivery": {
                "type": "STRING",
                "enum": list(_DELIVERY_FILTERS),
                "description": (
                    "Optional: only tasks with an open PR ('open_pr'), a merged PR "
                    "while the task is still open ('merged_pr'), or no PR ('no_pr')."
                ),
            },
        },
        "required": [],
    },
    run=_run,
)
