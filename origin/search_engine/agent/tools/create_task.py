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

`start_date`, `collaborators` and `custom_fields` (by field name) are the
other fields the UI's create form sets (MIP-878). They are checked before the
row exists, so a refused one creates nothing; see `task_custom_fields` and
`task_people` for the rules.

MIP-1561 (flag `AGENT_PARITY_TOOLS`, default OFF; with it off none of this exists, see
`parity.ignore_unless_parity`): the rest of what the create form sets. `status` (Open, WIP,
Blocked, Pending or Closed; Deleted is refused: a task is deleted with `update_task`),
`reporter_id` (default the caller; the same rule as `assignee_id`), `links` (`task_links`) and
`template`. A task sent with no `content_text` gets a body from a template, as one made in the app
does: the one `template` names (a built-in or one of the project's own, by name), else the
project's default (`plan_task_bodies.new_task_body`, the same text a plan's task gets). Both a
body and a `template` is refused: the template is the body.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.chip_syntax import CHIP_SYNTAX_HINT
from origin.search_engine.agent.parity import ignore_unless_parity, parity_on
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.blocknote_md import task_body_blocks
from origin.search_engine.agent.tools.body_limits import check_body_size
from origin.search_engine.agent.tools.body_mentions import notify_task_body_mentions
from origin.search_engine.agent.tools.entity_chips import chip_resolver
from origin.search_engine.agent.tools.plan_task_bodies import new_task_body
from origin.search_engine.agent.tools.task_custom_fields import (
    CUSTOM_FIELDS_SCHEMA,
    parse_iso_date,
    plan_custom_fields,
    project_fields,
)
from origin.search_engine.agent.tools.task_enums import VALID_STATUSES
from origin.search_engine.agent.tools.task_links import (
    LINKS_SCHEMA,
    plan_links,
    register_pr_webhooks,
)
from origin.search_engine.agent.tools.task_parent import require_readable_parent
from origin.search_engine.agent.tools.task_people import canonical_user_id, plan_collaborators
from origin.search_engine.agent.tools.task_tags import resolve_tags
from origin.search_engine.agent.undo import UNDO_KEY, created_task_capture
from origin.search_engine.quota import (
    TASK_CREATE_KEY,
    check_remaining_monthly,
    increment_usage,
)
from origin.services.milestone_service import ensure_backing_task
from origin.services.task_card_post import post_task_created_card
from origin.services.task_collaborators import sync_task_collaborators
from origin.services.task_reparent import find_parent, milestone_for_parent
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
        # The words of a missing one, so a guessed id does not say which milestones exist in
        # other teams (MIP-1519); only the class differs, for the MCP refusal alarm.
        raise ToolForbidden(f"Milestone {milestone_id} not found.")
    if milestone.project_id != project_id:
        raise ToolError(
            f"Milestone {milestone_id} belongs to project {milestone.project_id}, not {project_id}."
        )
    return milestone


def _resolve_assignee(raw_id: Any, project_acl: set[str], ctx: ToolContext) -> CustomUser:
    """The new task's assignee, or (`reporter_id`, MIP-1561) its reporter; same guard as
    `assign_task`. Both sets are scoped to this team, so a UUID from another tenant is refused."""
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


def _resolve_parent(raw_id: Any, project_id: int, ctx: ToolContext) -> TaskMaster:
    """The task a new task is filed under (MIP-899). It must be one the
    caller can read, in the caller's team and in the new task's project."""
    try:
        parent_id = int(raw_id)
    except (TypeError, ValueError):
        raise ToolError(f"`parent_task_id` must be a task id (got {raw_id!r}).")
    require_readable_parent(parent_id, ctx)
    parent, error = find_parent(parent_id, ctx.team_id)
    if error:
        raise ToolError(error)
    if parent.project_id != project_id:
        raise ToolError(
            f"Task {parent.display_id} is in project {parent.project_id}, not {project_id}."
        )
    return parent


def _parity_status(raw: Any) -> str:
    """The status a new task starts in (MIP-1561): Open unless one is named. Deleted is refused,
    in words that point at the tool that deletes; so is anything else outside the app's list."""
    if raw in (None, ""):
        return _DEFAULT_STATUS
    if raw == "Deleted":
        raise ToolError(
            "A task is not created as Deleted. Create it, then use `update_task` with "
            "status Deleted if it should go. Nothing was created."
        )
    if raw not in VALID_STATUSES:
        raise ToolError(
            f"`status` must be one of {sorted(VALID_STATUSES)} (got {raw!r}). Nothing was created."
        )
    return raw


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args = ignore_unless_parity(args, PARITY_PROPERTIES)
    parity = parity_on()
    check_body_size(args.get("content_text"), "content_text")
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
        # The words of a missing one (MIP-1519), as `milestone_access.load_project` does.
        raise ToolForbidden(f"Project {project_id} not found.")

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

    parent = None
    if args.get("parent_task_id") not in (None, ""):
        if milestone is not None:
            raise ToolError(
                "`milestone_id` and `parent_task_id` each decide where the new task "
                "sits; send one of them. Nothing was created."
            )
        parent = _resolve_parent(args["parent_task_id"], project_id, ctx)

    assignee = None
    raw_assignee_id = args.get("assignee_id")
    if raw_assignee_id and raw_assignee_id != "null":
        assignee = _resolve_assignee(raw_assignee_id, allowed, ctx)

    tags = resolve_tags(args["tags"], project_id) if args.get("tags") is not None else []

    content_text = args.get("content_text") or ""
    status = _DEFAULT_STATUS
    reporter_id: str = ctx.user_id
    reporter = None
    links: list[dict[str, Any]] | None = None
    template = None
    if parity:
        status = _parity_status(args.get("status"))
        if args.get("reporter_id") not in (None, "", "null"):
            reporter = _resolve_assignee(
                canonical_user_id(args["reporter_id"], "`reporter_id`"), allowed, ctx
            )
            reporter_id = str(reporter.id)
        if args.get("links") is not None:
            links = plan_links(args["links"], None)
        template = args.get("template")
        if template == "":
            template = None
        has_body = bool(str(content_text).strip())
        if template is not None and has_body:
            raise ToolError(
                "Send `content_text` or `template`, not both: the template IS the body. "
                "Nothing was created."
            )
        if not has_body:
            # The body a task made in the app starts with: the template named, else the
            # project's default (see `plan_task_bodies`).
            content_text = new_task_body(project, title, template)
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
    start_date = None
    if args.get("start_date") not in (None, ""):
        start_date = parse_iso_date(args["start_date"], "`start_date`")
    collaborator_ids: list[str] = []
    if args.get("collaborators") is not None:
        # The task's assignee (a project member, checked above) and its reporter (the caller)
        # can open it; so can anyone else on the project, and nobody else.
        collaborator_ids = plan_collaborators(
            args["collaborators"],
            project,
            set(),
            readers=(assignee.id if assignee is not None else None, ctx.user_id),
        )
    custom_values: dict[str, Any] = {}
    if args.get("custom_fields") is not None:
        custom_values = plan_custom_fields(
            args["custom_fields"], project_fields(project_id), None, project
        )
    due_date = args.get("due_date")
    # Let the model pass an ISO date string; pass it straight through to
    # Django which will parse it. Garbage gets a ToolError below.
    create_kwargs: dict[str, Any] = {
        "team_id": ctx.team_id,
        "project_id": project_id,
        "reporter_id": reporter_id,
        "title": title,
        "status": status,
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
    if start_date is not None:
        create_kwargs["start_date"] = start_date
    if custom_values:
        create_kwargs["custom_field_values"] = custom_values
    if links is not None:
        create_kwargs["links"] = links

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
        if parent is not None:
            # Hung under a task, it joins the milestone and sprint that task
            # lives in (or backs): the UI's POST derives the same. The root
            # signal then derives the chain from the parent.
            create_kwargs["parent_task_id"] = parent.task_id
            parent_milestone_id = milestone_for_parent(parent.task_id)
            if parent_milestone_id is not None:
                parent_milestone = MilestoneMaster.objects.get(milestone_id=parent_milestone_id)
                create_kwargs["milestone"] = parent_milestone
                create_kwargs["sprint_id"] = parent_milestone.sprint_id
        task = TaskMaster.objects.create(**create_kwargs)
    except Exception as e:  # noqa: BLE001 — surface as ToolError for the model
        # The model and the MCP caller read this; the database error
        # goes to the log with a reference (MIP-622).
        ref = log_failure("create_task tool failed", e)
        raise ToolError(f"Failed to create task (ref {ref}).")

    # The M2M is set once the row exists, as `TaskMasterView.post` does.
    sync_task_collaborators(task, collaborator_ids)
    increment_usage(ctx.user_id, TASK_CREATE_KEY)
    # An `@` chip in the body pings the person, as a typed one does.
    notify_task_body_mentions(task, actor_id=ctx.user_id)
    if links:
        # As the app's save does: a GitHub PR link gets the repo's webhook, best effort (it
        # needs the writer's own GitHub connection, and most writers have none).
        register_pr_webhooks(ctx.user_id, links)
    # No browser is here to post the "task created" card, so its assignee is told and the
    # task reaches the project chat from the server (MIP-1450).
    post_task_created_card(task, actor_id=ctx.user_id)

    summary = f"Created task {task.display_id}: {task.title}"
    if milestone is not None:
        summary += f' in milestone "{milestone.title}"'
    if parent is not None:
        summary += f" under {parent.display_id}"
    assignee_username = None
    if assignee is not None:
        assignee_username = assignee.username or str(assignee.id)
        summary += f", assigned to {assignee_username}"
    if parity and status != _DEFAULT_STATUS:
        summary += f", status {status}"
    # `due_date` is still the raw input string on the instance `create()`
    # returned (`2026-6-30`, a datetime), so read what was stored back: the
    # response shows what the row holds, not an echo of what was sent (MIP-828).
    # The parent and the milestone are read back the same way (MIP-924): which
    # one a task hangs under is decided above from `milestone_id` or
    # `parent_task_id`, and from either the row ends up with both.
    task.refresh_from_db(
        fields=[
            "priority",
            "effort_level",
            "due_date",
            "start_date",
            "parent_task_id",
            "milestone_id",
        ]
    )
    result = {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "project_id": project_id,
        "milestone_id": task.milestone_id,
        "parent_task_id": task.parent_task_id,
        "assignee_id": str(assignee.id) if assignee is not None else None,
        "assignee_username": assignee_username,
        "tags": [tag["tagName"] for tag in tags],
        "title": task.title,
        "status": task.status,
        "priority": task.priority or None,
        "effort_level": task.effort_level or None,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "start_date": task.start_date.isoformat() if task.start_date else None,
        "collaborators": collaborator_ids,
        "__summary__": summary,
    }
    if parity:
        if reporter is not None:
            result["reporter_id"] = str(reporter.id)
        if links is not None:
            result["links"] = [{"title": link["title"], "url": link["url"]} for link in links]
    # Undo (`agent/undo.py`): a stamp of the task as made, taken last, once every
    # post-save write (collaborators, root, mentions) has landed.
    if ctx.capture_undo and (image := created_task_capture(task)):
        result[UNDO_KEY] = image
    return result


#: What `AGENT_PARITY_TOOLS` adds to this tool's schema (`tool_flag_params`); the tool takes
#: these only while the flag is on (`parity.ignore_unless_parity`). Kept terse: every character is
#: billed on every call.
PARITY_PROPERTIES: dict[str, dict[str, Any]] = {
    "status": {
        "type": "STRING",
        "enum": ["Open", "WIP", "Blocked", "Pending", "Closed"],
        "description": "Optional starting status. Default Open.",
    },
    "reporter_id": {
        "type": "STRING",
        "description": "Optional reporter user id. Default: you. Same rule as assignee_id.",
    },
    "links": LINKS_SCHEMA,
    "template": {
        "type": "STRING",
        "description": (
            "Optional body template, used when you send no content_text: default, bug, "
            "spike, milestone, or a project template's name."
        ),
    },
}

#: The sentence the flag adds to the description (`tool_flag_text`).
DESCRIPTION_ADDITION = (
    " Also `status` (not Deleted), `reporter_id`, `links` ([{url, title}], http(s) only) and "
    "`template`. With no content_text the body is the project's default template (or "
    "`template`), as in the app; not both."
)

#: The base sentence this one contradicts, and what it says while the flag is on
#: (`tool_flag_text.REPLACEMENTS`).
STATUS_SENTENCE = "Status is always created as 'Open' — users change it via the UI. "
STATUS_SENTENCE_ON = "Status starts as 'Open' unless you pass `status`. "

CREATE_TASK = Tool(
    name="create_task",
    description=(
        "Create a new task in a project. REQUIRES USER APPROVAL — the "
        "user will see your proposed arguments and decide whether to "
        "execute. Use this when the user explicitly asks you to create / "
        "add / file a task. Required: title, project_id. Optional: "
        "content_text (markdown body), milestone_id (file it under an "
        "existing milestone), parent_task_id (file it under an existing "
        "task in the same project; not together with milestone_id), assignee_id, tags, priority, effort_level, due_date "
        "(ISO 8601, e.g. 2026-06-30), start_date (same), collaborators (user "
        "ids) and custom_fields (the project's own fields by name, from "
        "list_project_tags). Status is always created as 'Open' "
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
            "parent_task_id": {
                "type": "STRING",
                "description": (
                    "Optional: file the new task under this existing task, as a "
                    "sub-task. It must be in the same project, and the new task "
                    "joins its milestone. Not together with milestone_id."
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
                    "catalogue). An unknown name is refused — add a missing "
                    "one with add_project_tag first. Omit when no tag fits."
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
            "start_date": {
                "type": "STRING",
                "description": "Optional planned start, ISO 8601 date, e.g. 2026-06-15.",
            },
            "collaborators": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": (
                    "Optional collaborator user ids (list_project_members); each must "
                    "be a member of the project, as a collaborator is told about the "
                    "task and has to be able to open it."
                ),
            },
            "custom_fields": CUSTOM_FIELDS_SCHEMA,
        },
        "required": ["title", "project_id"],
    },
    run=_run,
    requires_approval=True,
)
