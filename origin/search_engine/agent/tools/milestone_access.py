"""Who may act on a milestone, a milestone folder or a project (MIP-879).

Shared by `manage_milestone` and `delete_milestone`. The REST views let any
project member do all of it (`is_project_member`: the owner or a member), and
answer a stranger with a 404 so a sequential id isn't an existence oracle. The
tools take the same rule, plus the tool-side tenant check: the object has to
be in the run's team. A refusal is worded exactly like a miss ("... not
found"), so a caller learns nothing about an object it can't see; it is still
a `ToolForbidden`, which is what the MCP refusal-burst alert counts.
"""

from __future__ import annotations

from typing import Any

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneFolder, MilestoneMaster
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.views.utils.scope_guards import is_project_member


def _as_int(raw: Any, field: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"`{field}` must be an integer (got {raw!r}).")


def _may_act_in(project: ProjectMaster | None, ctx: ToolContext) -> bool:
    return (
        project is not None
        and not project.is_deleted
        and str(project.team_id or "") == ctx.team_id
        and is_project_member(project.project_id, ctx.user_id)
    )


def load_project(raw_id: Any, ctx: ToolContext) -> ProjectMaster:
    """A live project in the caller's team that the caller belongs to."""
    project_id = _as_int(raw_id, "project_id")
    project = ProjectMaster.objects.select_related("team").filter(project_id=project_id).first()
    if project is None or project.is_deleted:
        raise ToolError(f"Project {project_id} not found.")
    if not _may_act_in(project, ctx):
        raise ToolForbidden(f"Project {project_id} not found.")
    return project


def load_milestone(
    raw_id: Any, ctx: ToolContext, *, allow_deleted: bool = False
) -> MilestoneMaster:
    """The milestone, if the caller may act on it. A deleted one is refused
    unless `allow_deleted` (the delete tool treats it as already done)."""
    milestone_id = _as_int(raw_id, "milestone_id")
    milestone = (
        MilestoneMaster.objects.select_related("project", "task", "folder", "sprint")
        .filter(milestone_id=milestone_id)
        .first()
    )
    if milestone is None:
        raise ToolError(f"Milestone {milestone_id} not found.")
    if str(milestone.team_id or "") != ctx.team_id or not _may_act_in(milestone.project, ctx):
        raise ToolForbidden(f"Milestone {milestone_id} not found.")
    if milestone.is_deleted and not allow_deleted:
        raise ToolError(f"Milestone {milestone_id} has been deleted.")
    return milestone


def load_folder(raw_id: Any, ctx: ToolContext) -> MilestoneFolder:
    """The folder, if the caller may act in its project."""
    folder_id = _as_int(raw_id, "milestone_folder_id")
    folder = MilestoneFolder.objects.select_related("project").filter(folder_id=folder_id).first()
    if folder is None:
        raise ToolError(f"Milestone folder {folder_id} not found.")
    # A folder's own `team` can be NULL (SET_NULL), so the tenant is its
    # project's.
    if not _may_act_in(folder.project, ctx):
        raise ToolForbidden(f"Milestone folder {folder_id} not found.")
    return folder
