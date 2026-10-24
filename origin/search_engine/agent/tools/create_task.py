"""`create_task` write tool — the first write tool in the registry.

Creates a new `TaskMaster` row under the given project. ACL: the
requesting user must be a member of the target project (same check
the read-side `fetch_task` uses).

This is the FIRST tool with `requires_approval=True`. The agent
controller routes any call to a `requires_approval` tool through the
pause/resume protocol introduced in Phase 7:

  1. Model invokes `create_task(...)` with proposed args.
  2. Controller emits `tool_call_pending_approval` and stops.
  3. Frontend renders an Approve / Reject card.
  4. User decides. POST `/api/v2/agent/decide/` resumes the loop.
  5. On approve: this tool's `_run` is called with the persisted args
     and the row is actually created. On reject: the tool is not
     called; the model sees a `{"error": "user_rejected"}` response
     and explains to the user.

`content_text` is markdown, parsed by the same `task_body_blocks` that
`update_task` and `create_task_plan` use, so a body renders the same
whichever tool wrote it.

`milestone_id` files the new task under an existing milestone exactly as
`create_task_plan`'s `existing_milestone_id` does: nested under the
milestone's backing task, in the milestone's sprint.

`assignee_id` assigns the task at creation, accepted on the same terms
as `assign_task`: an active member of this team, or someone on the
target project (a guest collaborator holds only the latter). Omitted
means unassigned — there is no implicit "assign to the caller" default.

`tags` takes tag names from the project's catalogue (`list_project_tags`);
an unknown name is refused, never created — see `task_tags`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.blocknote_md import task_body_blocks
from origin.search_engine.agent.tools.body_mentions import notify_task_body_mentions
from origin.search_engine.agent.tools.entity_chips import CHIP_SYNTAX_HINT, chip_resolver
from origin.search_engine.agent.tools.task_tags import resolve_tags
from origin.search_engine.quota import (
    TASK_CREATE_KEY,
    check_remaining_monthly,
    increment_usage,
)
from origin.services.milestone_service import ensure_backing_task
from origin.views.utils.scope_guards import is_team_member

_DEFAULT_STATUS = "Open"
# Canonical enum lives on the frontend in `taskMeta.ts`. Keep this in
# sync — both the priority and effort-level sets are read by chip
# colour lookups, and an out-of-set value renders without a colour.
_VALID_PRIORITIES = {"Minimal", "Low", "Normal", "High", "Critical"}
_VALID_EFFORTS = {"Minimal", "Low", "Moderate", "High", "Extensive"}


def _resolve_milestone(raw_id: Any, project_id: int, ctx: ToolContext) -> MilestoneMaster:
    """The milestone a new task is filed under; same checks as
    `create_task_plan`'s `existing_milestone_id`."""
    try:
        milestone_id = int(raw_id)
    except (TypeError, ValueError):
        raise ToolError(f"`milestone_id` must be an integer (got {raw_id!r}).")
    try:
        milestone = MilestoneMaster.objects.select_related("task").get(
            milestone_id=milestone_id, is_deleted=False
        )
    except MilestoneMaster.DoesNotExist:
        raise ToolError(f"Milestone {milestone_id} not found.")
    if str(getattr(milestone, "team_id", "") or "") != ctx.team_id:
        raise ToolForbidden("Not authorized: milestone belongs to a different team.")
    if milestone.project_id != project_id:
        raise ToolError(
            f"Milestone {milestone_id} belongs to project {milestone.project_id}, not {project_id}."
        )
    return milestone


def _resolve_assignee(raw_id: Any, project_acl: set[str], ctx: ToolContext) -> CustomUser:
    """The new task's assignee; same guard as `assign_task`. Both sets are
    scoped to this team, so a UUID from another tenant is refused."""
    if not (str(raw_id) in project_acl or is_team_member(ctx.team_id, raw_id)):
        raise ToolError(
            f"User {raw_id!r} is not an active member of this team, and is not "
            "on this project. Use get_team_members or list_project_members to "
            "find valid user ids, or get_current_user for yourself."
        )
    try:
        assignee = CustomUser.objects.get(id=raw_id)
    except (CustomUser.DoesNotExist, Exception):
        raise ToolError(f"User {raw_id!r} not found.")
    if assignee.is_deleted:
        raise ToolError(f"User {raw_id!r} account has been deleted.")
    return assignee


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    title = (args.get("title") or "").strip()
    if not title:
        raise ToolError("`title` is required.")

    raw_project_id = args.get("project_id")
    try:
        project_id = int(raw_project_id)
    except (TypeError, ValueError):
        raise ToolError(f"`project_id` must be an integer (got {raw_project_id!r}).")

    try:
        project = ProjectMaster.objects.get(project_id=project_id, is_deleted=False)
    except ProjectMaster.DoesNotExist:
        raise ToolError(f"Project {project_id} not found.")

    # Tenant guard: project must be in the requesting team.
    if str(getattr(project, "team_id", "") or "") != ctx.team_id:
        raise ToolForbidden("Not authorized: project belongs to a different team.")

    # ACL: requesting user must be a project member. Reuses the same
    # set the read-side `fetch_task` enforces, so a user who can't see
    # the project's tasks can't create one in it either.
    allowed = task_acl_user_ids(
        project_id=project_id,
        assignee_id=None,
        reporter_id=None,
    )
    if ctx.user_id not in allowed:
        raise ToolForbidden(f"Not authorized to create tasks in project {project_id}.")

    milestone = None
    if args.get("milestone_id") is not None:
        milestone = _resolve_milestone(args["milestone_id"], project_id, ctx)

    assignee = None
    raw_assignee_id = args.get("assignee_id")
    if raw_assignee_id and raw_assignee_id != "null":
        assignee = _resolve_assignee(raw_assignee_id, allowed, ctx)

    tags = resolve_tags(args["tags"], project_id) if args.get("tags") is not None else []

    content_text = args.get("content_text") or ""
    priority = args.get("priority")
    if priority is not None and priority not in _VALID_PRIORITIES:
        raise ToolError(
            f"`priority` must be one of {sorted(_VALID_PRIORITIES)} (got {priority!r})."
        )
    effort_level = args.get("effort_level")
    if effort_level is not None and effort_level not in _VALID_EFFORTS:
        raise ToolError(
            f"`effort_level` must be one of {sorted(_VALID_EFFORTS)} (got {effort_level!r})."
        )
    due_date = args.get("due_date")
    # Let the model pass an ISO date string; pass it straight through to
    # Django which will parse it. Garbage gets a ToolError below.
    create_kwargs: dict[str, Any] = {
        "team_id": ctx.team_id,
        "project_id": project_id,
        "reporter_id": ctx.user_id,
        "title": title,
        "status": _DEFAULT_STATUS,
        "content": task_body_blocks(
            content_text,
            entity_chip_resolver=chip_resolver(team_id=ctx.team_id, user_id=ctx.user_id),
        ),
    }
    if assignee is not None:
        create_kwargs["assignee_id"] = assignee.id
    if tags:
        create_kwargs["tags"] = tags
    if priority:
        create_kwargs["priority"] = priority
    if effort_level:
        create_kwargs["effort_level"] = effort_level
    if due_date:
        create_kwargs["due_date"] = due_date

    # Tier quota: monthly task-creation cap. Pre-flight (fail-open on
    # infra errors); charged after the row exists so a failed create
    # doesn't burn quota — same pattern as search_web's daily gate.
    allowed, _used, task_limit = check_remaining_monthly(ctx.user_id, TASK_CREATE_KEY)
    if not allowed:
        raise ToolError(
            f"You've created all {task_limit} tasks for this month. "
            "Upgrade your plan to keep going."
        )

    try:
        if milestone is not None:
            # A task "in" a milestone hangs off its backing task row (the
            # table nests it there) and takes the milestone's sprint. The
            # root_task_id signal then derives the root from that parent.
            # Same linkage `create_task_plan` and the UI's POST produce.
            backing = ensure_backing_task(milestone)
            create_kwargs["milestone"] = milestone
            create_kwargs["parent_task_id"] = backing.task_id
            create_kwargs["sprint_id"] = milestone.sprint_id
        task = TaskMaster.objects.create(**create_kwargs)
    except Exception as e:  # noqa: BLE001 — surface as ToolError for the model
        # The model and the MCP caller read this; the database error
        # goes to the log with a reference (MIP-622).
        ref = log_failure("create_task tool failed", e)
        raise ToolError(f"Failed to create task (ref {ref}).")

    increment_usage(ctx.user_id, TASK_CREATE_KEY)
    # An `@` chip in the body pings the person, as a typed one does.
    notify_task_body_mentions(task, actor_id=ctx.user_id)

    summary = f"Created task {task.display_id}: {task.title}"
    if milestone is not None:
        summary += f' in milestone "{milestone.title}"'
    assignee_username = None
    if assignee is not None:
        assignee_username = assignee.username or str(assignee.id)
        summary += f", assigned to {assignee_username}"
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "project_id": project_id,
        "milestone_id": milestone.milestone_id if milestone is not None else None,
        "assignee_id": str(assignee.id) if assignee is not None else None,
        "assignee_username": assignee_username,
        "tags": [tag["tagName"] for tag in tags],
        "title": task.title,
        "status": task.status,
        "__summary__": summary,
    }


CREATE_TASK = Tool(
    name="create_task",
    description=(
        "Create a new task in a project. REQUIRES USER APPROVAL — the "
        "user will see your proposed arguments and decide whether to "
        "execute. Use this when the user explicitly asks you to create / "
        "add / file a task. Required: title, project_id. Optional: "
        "content_text (markdown body), milestone_id (file it under an "
        "existing milestone), assignee_id, tags, priority, effort_level, due_date "
        "(ISO 8601, e.g. 2026-06-30). Status is always created as 'Open' "
        "— users change it via the UI. Leave assignee_id out unless the "
        "user names an assignee; use get_current_user when they say "
        "'assign it to me'. For tags, call list_project_tags and pick only "
        "tags that genuinely describe the task — leave tags out if none fits."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "title": {
                "type": "STRING",
                "description": "Short task title (1 line).",
            },
            "project_id": {
                "type": "INTEGER",
                "description": (
                    "Numeric project id the task belongs to. Resolve this "
                    "with `search_knowledge_base` first if the user "
                    "doesn't name it explicitly."
                ),
            },
            "content_text": {
                "type": "STRING",
                "description": (
                    "Optional task body / description as markdown: headings, "
                    "lists, check lists, code fences, tables and inline "
                    "formatting render natively." + CHIP_SYNTAX_HINT
                ),
            },
            "milestone_id": {
                "type": "INTEGER",
                "description": (
                    "Optional: file the task under this existing milestone "
                    "(numeric milestone id from `list_milestones`). It must be "
                    "in the same project."
                ),
            },
            "assignee_id": {
                "type": "STRING",
                "description": (
                    "Optional assignee user UUID: get_current_user for yourself, "
                    "get_team_members / list_project_members for someone else. "
                    "Omit for unassigned."
                ),
            },
            "tags": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": (
                    "Optional tag names from list_project_tags (this project's "
                    "catalogue). An unknown name is refused — tags can't be "
                    "created here. Omit when no tag fits."
                ),
            },
            "priority": {
                "type": "STRING",
                "enum": ["Minimal", "Low", "Normal", "High", "Critical"],
                "description": "Optional priority.",
            },
            "effort_level": {
                "type": "STRING",
                "enum": ["Minimal", "Low", "Moderate", "High", "Extensive"],
                "description": "Optional effort estimate.",
            },
            "due_date": {
                "type": "STRING",
                "description": "Optional ISO 8601 date, e.g. 2026-06-30.",
            },
        },
        "required": ["title", "project_id"],
    },
    run=_run,
    requires_approval=True,
)
