"""Who may do what to a project, for the project tools (MIP-882).

The page's rules, kept:

* **Create.** Any member of the team (never a guest from another team) may create a
  project. It is owned by the person asking: the REST create trusts an owner sent by
  the client, and a tool must not.
* **Rename, recode, change a role.** The project's owner or an editor
  (`can_manage`). Everyone else, and anyone outside the project, is refused.
* **Add a member.** Any member of the project, as the page allows, and the person
  added must already be on the team, so a project can't be used to pull in an
  outsider.

A tool adds the run's team on top, as the others do.
"""

from __future__ import annotations

from typing import Any

from origin.models.project.prj_models import ProjectMaster
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.task_access import as_int
from origin.services.member_roles import can_manage, resolve_project_role
from origin.views.utils.scope_guards import is_project_member, is_team_member


def require_team_member(ctx: ToolContext) -> None:
    if not is_team_member(ctx.team_id, ctx.user_id):
        raise ToolForbidden("Not authorized: only a member of this team can do that.")


def load_project(raw_project_id: Any, ctx: ToolContext) -> ProjectMaster:
    project_id = as_int(raw_project_id, "project_id")
    try:
        project = ProjectMaster.objects.get(project_id=project_id, is_deleted=False)
    except ProjectMaster.DoesNotExist:
        raise ToolError(f"Project {project_id} not found. Get ids from `list_projects`.")
    if str(project.team_id) != ctx.team_id:
        raise ToolForbidden("Not authorized: project is in a different team.")
    return project


def require_manager(project: ProjectMaster, ctx: ToolContext, what: str) -> None:
    if not can_manage(resolve_project_role(project, ctx.user_id)):
        raise ToolForbidden(f"Only the project owner or an editor can {what}.")


def require_member(project: ProjectMaster, ctx: ToolContext) -> None:
    if not is_project_member(project.project_id, ctx.user_id):
        raise ToolForbidden("Not authorized: you are not a member of this project.")
