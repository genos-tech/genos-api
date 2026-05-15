"""`list_tasks` tool — structured task query.

Complements `search_knowledge_base` (semantic) with a precise ORM-backed
filter for structural questions: "what are my overdue tasks?", "list all
WIP tasks in project X", "which tasks are assigned to me?".

ACL contract (defence-in-depth):
  * Base scope: `team_id = ctx.team_id` — never crosses tenant boundary.
  * Visibility scope: the query is further restricted to tasks that the
    requesting user legitimately sees:
      - Tasks in projects where ctx.user_id is a ProjectMember, OR
      - Tasks where ctx.user_id is the assignee, OR
      - Tasks where ctx.user_id is the reporter.
    This mirrors the `task_acl_user_ids` derivation used by fetch_task but
    applied as a Django Q-filter so the whole result set is scoped in one
    query rather than per-row.
  * If the caller additionally filters by `project_id`, we verify the user
    is a member of that specific project before narrowing the queryset.
    This catches the case where a user is assignee on a task in a project
    they aren't a member of — they shouldn't be able to enumerate all
    tasks in that project just because they appear on one.

All user ids used for ACL come from `ctx` (server-trusted), never from the
LLM's function-call arguments.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Q
from django.utils import timezone

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError

_MAX_LIMIT = 50
_VALID_STATUSES = {"Open", "WIP", "Pending", "Closed"}


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # --- Derive the set of project ids the user belongs to. ---
    # Used both for the base ACL filter and for validating explicit
    # project_id requests.
    member_project_ids = set(
        ProjectMembers.objects.filter(
            attendee_id=ctx.user_id,
            project__team_id=ctx.team_id,
            project__is_deleted=False,
        ).values_list("project_id", flat=True)
    )

    # --- Base queryset: tenant-scoped, soft-delete excluded. ---
    qs = TaskMaster.objects.filter(
        team_id=ctx.team_id,
        is_deleted=False,
        is_init_task=False,
    ).filter(
        # ACL row-filter: mirrors task_acl_user_ids logic as a set-based
        # predicate so we get a single query rather than per-row checks.
        Q(project_id__in=member_project_ids)
        | Q(assignee_id=ctx.user_id)
        | Q(reporter_id=ctx.user_id)
    )

    # --- Optional caller filters ---

    raw_project_id = args.get("project_id")
    if raw_project_id is not None:
        try:
            project_id = int(raw_project_id)
        except (TypeError, ValueError):
            raise ToolError(f"`project_id` must be an integer (got {raw_project_id!r}).")
        # Extra membership check: enumerating all tasks in a project the
        # user isn't a member of is not allowed even if they're the
        # assignee on some tasks within it.
        if project_id not in member_project_ids:
            raise ToolError(
                f"Not authorized to list all tasks in project {project_id}. "
                "You are not a member of that project."
            )
        qs = qs.filter(project_id=project_id)

    raw_statuses = args.get("status")
    if raw_statuses is not None:
        if isinstance(raw_statuses, str):
            raw_statuses = [raw_statuses]
        invalid = set(raw_statuses) - _VALID_STATUSES
        if invalid:
            raise ToolError(
                f"Invalid status value(s): {sorted(invalid)}. "
                f"Must be one of {sorted(_VALID_STATUSES)}."
            )
        qs = qs.filter(status__in=raw_statuses)

    raw_assignee_id = args.get("assignee_id")
    if raw_assignee_id:
        # The caller supplies a user_id (UUID string) they got from
        # get_team_members or get_current_user.  We don't ACL-check the
        # assignee_id itself — it's a filter value, not an auth claim.
        qs = qs.filter(assignee_id=raw_assignee_id)

    if args.get("overdue_only"):
        today = timezone.now().date()
        qs = qs.exclude(status__in=["Closed", "Deleted"]).filter(
            due_date__isnull=False, due_date__lt=today
        )

    try:
        limit = int(args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, _MAX_LIMIT))

    qs = qs.order_by("-ts_updated_at")[:limit]

    tasks = []
    for t in qs:
        tasks.append(
            {
                "task_id": t.task_id,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "assignee_id": str(t.assignee_id) if t.assignee_id else None,
                "project_id": t.project_id,
            }
        )

    return {
        "tasks": tasks,
        "__summary__": f"Found {len(tasks)} task(s)",
    }


LIST_TASKS = Tool(
    name="list_tasks",
    description=(
        "Structured query for tasks: filter by project, status, assignee, "
        "or overdue date. Use this instead of search_knowledge_base when "
        "the user asks a structural question like 'what are my open tasks?', "
        "'which tasks are overdue in project X?', or 'list all WIP tasks'. "
        "Returns task_id, title, status, priority, due_date, assignee_id, "
        "and project_id. Results are scoped to tasks the current user is "
        "authorised to see (project member, assignee, or reporter)."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "project_id": {
                "type": "INTEGER",
                "description": (
                    "Restrict to one project. Resolve the name to an id with "
                    "`list_projects` first if needed. Omit to search across "
                    "all accessible projects."
                ),
            },
            "status": {
                "type": "ARRAY",
                "items": {
                    "type": "STRING",
                    "enum": ["Open", "WIP", "Pending", "Closed"],
                },
                "description": (
                    "Filter by one or more statuses. Omit to include all " "non-deleted tasks."
                ),
            },
            "assignee_id": {
                "type": "STRING",
                "description": (
                    "Filter by assignee UUID. Use get_current_user to get "
                    "the caller's own id, or get_team_members to resolve "
                    "a name to a UUID."
                ),
            },
            "overdue_only": {
                "type": "BOOLEAN",
                "description": (
                    "If true, only return tasks whose due_date is in the past "
                    "and status is not Closed or Deleted."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": f"Max results to return (1–{_MAX_LIMIT}). Default 20.",
            },
        },
        "required": [],
    },
    run=_run,
)
