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

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from django.db.models import Q

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.task_tags import tag_names
from origin.services.milestone_rollup import milestone_tasks_q
from origin.services.user_time import today_for_user_id

_MAX_LIMIT = 50
_VALID_STATUSES = {"Open", "WIP", "Blocked", "Pending", "Closed", "Deleted"}
# Mirror of the canonical frontend enum in `taskMeta.ts` (Minimal/Low/
# Normal/High/Critical). Invalid values are rejected with a clear error
# so the model corrects course rather than silently returning [].
_VALID_PRIORITIES = {"Minimal", "Low", "Normal", "High", "Critical"}


def _encode_cursor(updated_at: datetime, task_id: int) -> str:
    """Opaque position of the last row of a page: where the next one starts."""
    raw = json.dumps({"t": updated_at.isoformat(), "id": task_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        return datetime.fromisoformat(data["t"]), int(data["id"])
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise ToolError(
            "`cursor` is not valid. Pass back the `next_cursor` of the previous page "
            "unchanged, together with the same filters."
        )


def _milestone_task_q(milestone: MilestoneMaster) -> Q:
    """Every task belonging to this milestone, its own backing row and
    "Deleted" ones included: the full set, for callers that need all of it
    (dependency edges, order of work). Rollups and `list_tasks` use
    `milestone_work_q` instead. The definition is shared with the UI's
    `_serialize_milestone` in `origin/services/milestone_rollup.py`."""
    return milestone_tasks_q(milestone)


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
            raise ToolForbidden(
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

    # A task deleted in the UI is marked by this status (the task tree filters
    # on it too; `is_deleted` is not always set). It is gone, so it stays out
    # of every page unless the caller asks for it by name (MIP-776).
    asked_for_deleted = "Deleted" in (raw_statuses or [])
    if not asked_for_deleted:
        qs = qs.exclude(status="Deleted")

    raw_assignee_id = args.get("assignee_id")
    if raw_assignee_id:
        # The caller supplies a user_id (UUID string) they got from
        # get_team_members or get_current_user.  We don't ACL-check the
        # assignee_id itself — it's a filter value, not an auth claim.
        qs = qs.filter(assignee_id=raw_assignee_id)

    raw_priorities = args.get("priority")
    if raw_priorities is not None:
        if isinstance(raw_priorities, str):
            raw_priorities = [raw_priorities]
        invalid_pri = set(raw_priorities) - _VALID_PRIORITIES
        if invalid_pri:
            raise ToolError(
                f"Invalid priority value(s): {sorted(invalid_pri)}. "
                f"Must be one of {sorted(_VALID_PRIORITIES)}."
            )
        qs = qs.filter(priority__in=raw_priorities)

    raw_milestone_id = args.get("milestone_id")
    if raw_milestone_id is not None:
        try:
            milestone_id = int(raw_milestone_id)
        except (TypeError, ValueError):
            raise ToolError(f"`milestone_id` must be an integer (got {raw_milestone_id!r}).")
        try:
            milestone = MilestoneMaster.objects.get(milestone_id=milestone_id, is_deleted=False)
        except MilestoneMaster.DoesNotExist:
            raise ToolError(f"Milestone {milestone_id} not found.")
        if str(getattr(milestone, "team_id", "") or "") != ctx.team_id:
            raise ToolForbidden("Not authorized: milestone is in a different team.")
        if milestone.project_id not in member_project_ids:
            raise ToolForbidden(
                f"Not authorized to list tasks in milestone {milestone_id}. "
                "You are not a member of that project."
            )
        # Use the same Q-union as the milestone UI rollup so subtasks are
        # included. `.distinct()` because the union can match a row twice.
        # The milestone's work: not its own backing row, and (unless asked for)
        # not Deleted tasks, so the rows add up to the milestone's totals.
        qs = qs.filter(
            milestone_tasks_q(milestone, own_row=False, deleted=asked_for_deleted)
        ).distinct()

    if args.get("overdue_only"):
        today = today_for_user_id(ctx.user_id)
        qs = qs.exclude(status__in=["Closed", "Deleted"]).filter(
            due_date__isnull=False, due_date__lt=today
        )

    try:
        limit = int(args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, _MAX_LIMIT))

    # Newest update first, task id to break ties: a total order, so a page
    # boundary falls between two rows the same way every time. A task updated
    # between two page requests moves to the front and is not seen again.
    qs = qs.select_related("project").order_by("-ts_updated_at", "-task_id")
    raw_cursor = args.get("cursor")
    if raw_cursor:
        after_at, after_id = _decode_cursor(str(raw_cursor))
        qs = qs.filter(
            Q(ts_updated_at__lt=after_at) | Q(ts_updated_at=after_at, task_id__lt=after_id)
        )
    # One extra row says whether anything is left.
    rows = list(qs[: limit + 1])
    has_more = len(rows) > limit
    rows = rows[:limit]

    tasks = []
    for t in rows:
        tasks.append(
            {
                "task_id": t.task_id,
                # Human-readable PRJ-123 form. Used in chip labels and
                # whenever the prose references a task — never expose the
                # raw integer task_id to end users.
                "display_id": t.display_id,
                "title": t.title,
                "status": t.status,
                "priority": t.priority,
                "effort_level": t.effort_level,
                "due_date": t.due_date.isoformat() if t.due_date else None,
                "assignee_id": str(t.assignee_id) if t.assignee_id else None,
                "tags": tag_names(t.tags),
                "project_id": t.project_id,
                "project_name": t.project.project_name if t.project else None,
                # Where it sits (MIP-779), so a milestone's hierarchy can be
                # rebuilt from one listing. Ids only: a title or code from
                # another row would need its own read check.
                "parent_task_id": t.parent_task_id,
                "milestone_id": t.milestone_id,
                "is_milestone": bool(t.is_milestone),
            }
        )

    result: dict[str, Any] = {"tasks": tasks, "has_more": has_more}
    summary = f"Found {len(tasks)} task(s)"
    if has_more:
        result["next_cursor"] = _encode_cursor(rows[-1].ts_updated_at, rows[-1].task_id)
        summary += " (more available)"
    result["__summary__"] = summary
    return result


LIST_TASKS = Tool(
    name="list_tasks",
    description=(
        "Structured query for tasks: filter by project, milestone, status, "
        "priority, assignee, or overdue date. Use this instead of "
        "search_knowledge_base when the user asks a structural question like "
        "'what are my open tasks?', 'which Critical tasks are in milestone X?', "
        "'overdue tasks in project Y', or 'list all WIP tasks assigned to me'. "
        "Returns task_id, title, status, priority, due_date, assignee_id, "
        "project_id, project_name, and where it sits: parent_task_id, "
        "milestone_id and is_milestone (a task filed under a milestone by "
        "its parent alone has a null milestone_id; the milestone's own "
        "row has is_milestone true). Rows are newest-updated first. When more "
        "tasks match than were returned, `has_more` is true and `next_cursor` "
        "starts the next page: pass it back as `cursor` with the same filters "
        "(the list is partial until `has_more` is false). Prefer naming projects by their "
        "project_name in prose (e.g. 'In **Website Redesign**: ...') rather "
        "than as 'Project N'. Results are scoped to tasks the current user is "
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
            "milestone_id": {
                "type": "INTEGER",
                "description": (
                    "Restrict to one milestone. Includes both direct-FK tasks "
                    "AND subtasks (tasks whose `parent_task_id` is the "
                    "milestone's backing task), but not the milestone's own "
                    "row, so the rows add up to its task counts. Resolve a "
                    "milestone name to an id with `list_milestones` first. The milestone's "
                    "project membership is checked just like `project_id`."
                ),
            },
            "status": {
                "type": "ARRAY",
                "items": {
                    "type": "STRING",
                    "enum": ["Open", "WIP", "Blocked", "Pending", "Closed", "Deleted"],
                },
                "description": (
                    "Filter by one or more statuses. Omit to get live work: "
                    "tasks with status Deleted are gone and are left out unless "
                    "you ask for `Deleted` by name."
                ),
            },
            "priority": {
                "type": "ARRAY",
                "items": {
                    "type": "STRING",
                    "enum": ["Minimal", "Low", "Normal", "High", "Critical"],
                },
                "description": (
                    "Filter by one or more priorities. Useful for 'show me "
                    "Critical tasks under milestone X' or 'High-priority "
                    "open tasks across my projects'."
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
                "description": f"Page size (1–{_MAX_LIMIT}). Default 20.",
            },
            "cursor": {
                "type": "STRING",
                "description": (
                    "The `next_cursor` of the previous page, to get the next one. "
                    "Use the same filters as that call."
                ),
            },
        },
        "required": [],
    },
    run=_run,
)
