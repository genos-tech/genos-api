"""Load the project or sprint a sprint tool acts on (MIP-882).

The page's rule, kept: anyone who is a member of the project may create, edit or
delete its sprints (viewers and guests included), and a stranger gets no hint
the sprint exists. A tool adds the run's team on top, as the others do.
"""

from __future__ import annotations

from typing import Any

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.sprint_models import Sprint
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.task_access import as_int
from origin.views.utils.scope_guards import is_project_member


def _require_member(project_id: int, ctx: ToolContext, what: str) -> None:
    if not is_project_member(project_id, ctx.user_id):
        raise ToolForbidden(f"Not authorized: you are not a member of the project of {what}.")


def load_project(raw_project_id: Any, ctx: ToolContext) -> ProjectMaster:
    project_id = as_int(raw_project_id, "project_id")
    try:
        project = ProjectMaster.objects.get(project_id=project_id, is_deleted=False)
    except ProjectMaster.DoesNotExist:
        raise ToolError(f"Project {project_id} not found.")
    if str(project.team_id) != ctx.team_id:
        # The words of a missing one (MIP-1519); the class still counts toward the refusal alarm.
        raise ToolForbidden(f"Project {project_id} not found.")
    _require_member(project.project_id, ctx, f"project {project_id}")
    return project


def load_sprint(raw_sprint_id: Any, ctx: ToolContext, *, allow_deleted: bool = False) -> Sprint:
    sprint_id = as_int(raw_sprint_id, "sprint_id")
    try:
        sprint = Sprint.objects.get(sprint_id=sprint_id)
    except Sprint.DoesNotExist:
        raise ToolError(f"Sprint {sprint_id} not found. Get ids from `list_sprints`.")
    if sprint.is_deleted and not allow_deleted:
        raise ToolError(f"Sprint {sprint_id} has been deleted.")
    if str(sprint.team_id) != ctx.team_id:
        raise ToolForbidden(f"Sprint {sprint_id} not found. Get ids from `list_sprints`.")
    _require_member(sprint.project_id, ctx, f"sprint {sprint_id}")
    return sprint
