"""`manage_project` write tool (MIP-882): create a project, or rename or recode one.

The page creates a project in several browser steps (a system user for the project,
the project, its creator's membership, the system user's team membership, then the
other members). A tool has no browser, so `services/project_manage.create_project`
does all of it on the server in one transaction.

Only what the page lets the owner or an editor change is here: the name and the code
(the prefix of the project's task ids). Handing ownership to someone else is not,
and neither is deleting the project (`delete_project`, in the app only).

Who is added to a project is its own tool, `manage_project_members`, because on MCP
that needs the key's opt-in and creating or renaming a project does not.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMembers
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.project_access import (
    load_project,
    require_manager,
    require_team_member,
)
from origin.services.project_manage import (
    ProjectRefused,
    create_project,
    set_project_code,
    set_project_name,
)

_ACTIONS = ("create", "update")


def _result(project, verb: str, **extra) -> dict[str, Any]:
    return {
        "project_id": project.project_id,
        "name": project.project_name,
        "code": project.code,
        "is_private": project.is_private,
        **extra,
        "__summary__": f'{verb} project "{project.project_name}" ({project.code})',
    }


def _create(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    require_team_member(ctx)
    team = TeamMaster.objects.get(team_id=ctx.team_id)
    owner = CustomUser.objects.get(id=ctx.user_id)
    member_ids = args.get("member_ids") or []
    if not isinstance(member_ids, list):
        raise ToolError("`member_ids` must be a list of user ids (from get_team_members).")
    try:
        project = create_project(
            team=team,
            owner=owner,
            name=args.get("name"),
            is_private=bool(args.get("is_private")),
            member_ids=member_ids,
        )
    except ProjectRefused as refused:
        raise ToolError(f"{refused.message} Nothing was created.")
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_project create failed", e)
        raise ToolError(f"Failed to create the project (ref {ref}).")
    return _result(
        project, "Created", members=ProjectMembers.objects.filter(project=project).count()
    )


def _update(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = load_project(args.get("project_id"), ctx)
    given = {k for k in ("name", "code") if args.get(k) is not None}
    if not given:
        raise ToolError("Nothing to change: send `name`, `code` or both.")
    require_manager(project, ctx, "rename a project or change its code")
    try:
        if "code" in given:
            set_project_code(project, args["code"])
        if "name" in given:
            set_project_name(project, args["name"])
    except ProjectRefused as refused:
        raise ToolError(refused.message)
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_project update failed", e)
        raise ToolError(f"Failed to update the project (ref {ref}).")
    return _result(project, "Updated")


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = args.get("action")
    if action not in _ACTIONS:
        raise ToolError(f"`action` must be one of {list(_ACTIONS)} (got {action!r}).")
    return _create(args, ctx) if action == "create" else _update(args, ctx)


MANAGE_PROJECT = Tool(
    name="manage_project",
    description=(
        "Create a project, or rename one or change its code. A project is the workspace that "
        "holds tasks; a milestone, sprint or task is not one (use manage_milestone, "
        "manage_sprint or create_task). REQUIRES USER APPROVAL. action "
        "'create': needs name (unique in the team); is_private (default false) and member_ids "
        "(team members to add, from get_team_members) are optional. You become its owner and "
        "the code is derived from the name. action 'update': call `list_projects` FIRST for "
        "the project_id, then send name and/or code (2-6 uppercase letters or digits, starting "
        "with a letter; it prefixes the project's task ids). Only the owner or an editor can "
        "update. To add people use manage_project_members."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": list(_ACTIONS),
                "description": "'create' a new project, or 'update' an existing one.",
            },
            "project_id": {
                "type": "INTEGER",
                "description": "update: the project to change (from list_projects).",
            },
            "name": {
                "type": "STRING",
                "description": "The project's name (up to 120 characters). Needed to create.",
            },
            "code": {
                "type": "STRING",
                "description": "update: the new task-id prefix, e.g. 'WEB'.",
            },
            "is_private": {
                "type": "BOOLEAN",
                "description": "create: true to make it private (members only). Default false.",
            },
            "member_ids": {
                "type": "ARRAY",
                "items": {"type": "STRING"},
                "description": "create: user ids of team members to add besides you.",
            },
        },
        "required": ["action"],
    },
    run=_run,
    requires_approval=True,
)
