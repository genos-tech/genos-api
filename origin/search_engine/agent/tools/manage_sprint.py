"""`manage_sprint` write tool (MIP-882): create a sprint, or change one.

Through the one implementation the page's create and edit share
(`services/sprint_manage`), so the same rules hold: the end may not come before
the start, and the window may not overlap another live sprint of the project.

There is no "end the sprint" action, because the page has none: a sprint's status
(`upcoming`, `active`, `completed`) follows its dates, so ending one early is
setting its `end_date`.

Deleting is its own tool (`delete_sprint`) so the MCP opt-in for destructive tools
covers a delete and nothing else.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.sprint_access import load_project, load_sprint
from origin.search_engine.agent.tools.todo_args import parse_iso_date
from origin.services.sprint_manage import SprintRefused, create_sprint, update_sprint
from origin.services.sprint_status import derive_status
from origin.services.user_time import today_for_user_id

_ACTIONS = ("create", "update")
_NAME_MAX = 120


def _name(raw: Any) -> str:
    name = (raw or "").strip() if isinstance(raw, str) else ""
    if not name:
        raise ToolError("`name` is required and must be non-empty.")
    if len(name) > _NAME_MAX:
        raise ToolError(f"`name` is too long ({len(name)} characters; the limit is {_NAME_MAX}).")
    return name


def _result(sprint, ctx: ToolContext, verb: str) -> dict[str, Any]:
    status = derive_status(sprint, today_for_user_id(ctx.user_id))
    return {
        "sprint_id": sprint.sprint_id,
        "project_id": sprint.project_id,
        "name": sprint.name,
        "start_date": sprint.start_date.isoformat(),
        "end_date": sprint.end_date.isoformat(),
        "status": status,
        "__summary__": (
            f'{verb} sprint "{sprint.name}" ({sprint.start_date.isoformat()} to '
            f"{sprint.end_date.isoformat()}, {status})"
        ),
    }


def _create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = load_project(args.get("project_id"), ctx)
    name = _name(args.get("name"))
    for key in ("start_date", "end_date"):
        if not args.get(key):
            raise ToolError(f"`{key}` (YYYY-MM-DD) is required to create a sprint.")
    start = parse_iso_date(args["start_date"], "start_date")
    end = parse_iso_date(args["end_date"], "end_date")
    try:
        sprint = create_sprint(project=project, name=name, start=start, end=end)
    except SprintRefused as refused:
        raise ToolError(f"{refused.message} Nothing was created.")
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_sprint create failed", e)
        raise ToolError(f"Failed to create the sprint (ref {ref}).")
    return _result(sprint, ctx, "Created")


def _update(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    sprint = load_sprint(args.get("sprint_id"), ctx)
    given = {k: args[k] for k in ("name", "start_date", "end_date") if args.get(k) is not None}
    if not given:
        raise ToolError("Nothing to change: send at least one of name, start_date, end_date.")
    # Read every input BEFORE the guarded section: a refusal that names the bad
    # argument ("`name` is too long", "`end_date` must be YYYY-MM-DD") has to reach
    # the model as it is, not be swallowed by the catch-all below as "Failed (ref)".
    name = _name(given["name"]) if "name" in given else None
    start = parse_iso_date(given["start_date"], "start_date") if "start_date" in given else None
    end = parse_iso_date(given["end_date"], "end_date") if "end_date" in given else None
    try:
        sprint = update_sprint(sprint, name=name, start=start, end=end)
    except SprintRefused as refused:
        raise ToolError(f"{refused.message} Nothing was changed.")
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_sprint update failed", e)
        raise ToolError(f"Failed to update the sprint (ref {ref}).")
    return _result(sprint, ctx, "Updated")


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = args.get("action")
    if action not in _ACTIONS:
        raise ToolError(f"`action` must be one of {list(_ACTIONS)} (got {action!r}).")
    return _create(args, ctx) if action == "create" else _update(args, ctx)


MANAGE_SPRINT = Tool(
    name="manage_sprint",
    description=(
        "Create a sprint, or change one. REQUIRES USER APPROVAL. action 'create': needs "
        "project_id (from list_projects), name, and start_date and end_date as YYYY-MM-DD; the "
        "window may not overlap another sprint of the project. action 'update': call "
        "`list_sprints` FIRST for the sprint_id, then send sprint_id and any of name, start_date, "
        "end_date; what you leave out stays. To END a sprint early, set its end_date: its status "
        "follows its dates and there is no separate end action. To delete one use delete_sprint."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": list(_ACTIONS),
                "description": "'create' a new sprint, or 'update' an existing one.",
            },
            "project_id": {
                "type": "INTEGER",
                "description": "create: the project to add the sprint to (from list_projects).",
            },
            "sprint_id": {
                "type": "INTEGER",
                "description": "update: the sprint to change (from list_sprints).",
            },
            "name": {
                "type": "STRING",
                "description": f"The sprint's name (up to {_NAME_MAX} characters). Needed to create.",
            },
            "start_date": {
                "type": "STRING",
                "description": "First day, YYYY-MM-DD. Needed to create.",
            },
            "end_date": {
                "type": "STRING",
                "description": "Last day, YYYY-MM-DD (not before start_date). Needed to create.",
            },
        },
        "required": ["action"],
    },
    run=_run,
    requires_approval=True,
)
