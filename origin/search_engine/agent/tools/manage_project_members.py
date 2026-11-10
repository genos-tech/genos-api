"""`manage_project_members` write tool (MIP-882): add a member, change a role, or remove one (MIP-1371).

Who can see and edit a project is access, and MCP has no approval card, so this is
its own tool: on MCP it needs the key's opt-in (as a delete does), which creating or
renaming a project (`manage_project`) does not.

The page's rules, kept. Any member of the project may add someone, and the person
added must already be on the team. Adding a member also gives them the project's chat
channel (a signal mirrors the membership). Changing a role is for the owner or an
editor; the role is `editor` or `viewer`, never `owner`, and the owner's own role is
never changed (that is a transfer). Removing someone else (`remove`) is the owner's, or an
editor's for a viewer or guest only; yourself is Leave, and the owner can't be removed.
`unassign_open_tasks` also takes them off their open tasks in this project.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMembers
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.project_access import (
    load_project,
    require_manager,
    require_member,
)
from origin.services.project_manage import (
    ProjectRefused,
    change_member_role,
    record_member_added,
    remove_member,
)
from origin.views.utils.scope_guards import is_team_member

_ACTIONS = ("add", "set_role", "remove")


def _user_id(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ToolError("`user_id` is required (from get_team_members or list_project_members).")
    return value


def _add(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = load_project(args.get("project_id"), ctx)
    require_member(project, ctx)
    user_id = _user_id(args.get("user_id"))
    if not is_team_member(ctx.team_id, user_id):
        raise ToolError(
            f"User {user_id} is not a member of this team, so they can't be added to a project."
        )
    if ProjectMembers.objects.filter(project=project, attendee_id=user_id).exists():
        return {
            "project_id": project.project_id,
            "user_id": user_id,
            "already_member": True,
            "__summary__": f'That person is already a member of "{project.project_name}".',
        }
    try:
        # Plain `.create()`: the signal that gives the member the project's chat
        # channel only fires on a save.
        ProjectMembers.objects.create(team=project.team, project=project, attendee_id=user_id)
        record_member_added(
            project, user_id, via="direct", actor=CustomUser.objects.filter(id=ctx.user_id).first()
        )
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_project_members add failed", e)
        raise ToolError(f"Failed to add the member (ref {ref}).")
    return {
        "project_id": project.project_id,
        "user_id": user_id,
        "already_member": False,
        "__summary__": f'Added a member to "{project.project_name}"',
    }


def _set_role(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = load_project(args.get("project_id"), ctx)
    require_manager(project, ctx, "change member roles")
    user_id = _user_id(args.get("user_id"))
    try:
        previous = change_member_role(
            project,
            user_id,
            args.get("role"),
            actor=CustomUser.objects.filter(id=ctx.user_id).first(),
        )
    except ProjectRefused as refused:
        raise ToolError(f"{refused.message} Nothing was changed.")
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_project_members set_role failed", e)
        raise ToolError(f"Failed to change the role (ref {ref}).")
    return {
        "project_id": project.project_id,
        "user_id": user_id,
        "role": args.get("role"),
        "previous_role": previous,
        "__summary__": f'Changed a member of "{project.project_name}" from {previous} to '
        f"{args.get('role')}",
    }


def _remove(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = load_project(args.get("project_id"), ctx)
    require_member(project, ctx)
    user_id = _user_id(args.get("user_id"))
    actor = CustomUser.objects.filter(id=ctx.user_id).first()
    try:
        unassigned = remove_member(
            project,
            user_id,
            actor_id=ctx.user_id,
            unassign=args.get("unassign_open_tasks") is True,
            actor=actor,
        )
    except ProjectRefused as refused:
        raise ToolError(f"{refused.message} Nothing was changed.")
    except Exception as e:  # noqa: BLE001
        ref = log_failure("manage_project_members remove failed", e)
        raise ToolError(f"Failed to remove the member (ref {ref}).")
    return {
        "project_id": project.project_id,
        "user_id": user_id,
        "unassigned": unassigned,
        "__summary__": f'Removed a member from "{project.project_name}"',
    }


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = args.get("action")
    if action not in _ACTIONS:
        raise ToolError(f"`action` must be one of {list(_ACTIONS)} (got {action!r}).")
    return {"add": _add, "set_role": _set_role, "remove": _remove}[action](args, ctx)


MANAGE_PROJECT_MEMBERS = Tool(
    name="manage_project_members",
    description=(
        "Add a person to a project, change a member's role, or remove one. REQUIRES USER "
        "APPROVAL — it changes who can see and edit the project. Call `list_projects` FIRST "
        "for the project_id and `get_team_members` for the user_id. action 'add': any member "
        "of the project may add a teammate; they also get the project's chat. action "
        "'set_role': the owner or an editor sets role to 'editor' or 'viewer'; the owner's "
        "role can't be changed. action 'remove': takes someone ELSE out of the project and "
        "its chat. The owner can remove anyone but themselves; an editor only viewers and "
        "guests; the owner can't be removed. It can't be undone from here (adding them back "
        "is a new 'add'). Pass unassign_open_tasks=true to also take them off their open "
        "tasks and milestones in this project."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": list(_ACTIONS),
                "description": "'add' a member, 'set_role' to change one's role, or 'remove' one.",
            },
            "project_id": {
                "type": "INTEGER",
                "description": "The project (from list_projects).",
            },
            "user_id": {
                "type": "STRING",
                "description": "The person (from get_team_members or list_project_members).",
            },
            "role": {
                "type": "STRING",
                "enum": ["editor", "viewer"],
                "description": "set_role: the new role.",
            },
            "unassign_open_tasks": {
                "type": "BOOLEAN",
                "description": (
                    "remove: also unassign the person's open tasks and milestones in this "
                    "project. Closed work keeps its assignee."
                ),
            },
        },
        "required": ["action", "project_id", "user_id"],
    },
    run=_run,
    requires_approval=True,
)
