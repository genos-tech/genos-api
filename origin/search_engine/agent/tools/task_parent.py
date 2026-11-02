"""The caller's right to file a task under another task (MIP-899).

Shared by `create_task` and `update_task`. A parent's title is shown on the
task filed under it, so a task the caller can't read can't be a parent, even
in the same team and project. The structural checks (it exists, the same
team and project, no cycle) are `origin.services.task_reparent`'s.
"""

from __future__ import annotations

from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import ToolContext, ToolForbidden


def require_readable_parent(parent_task_id: int, ctx: ToolContext) -> None:
    """Raise `ToolForbidden` when `parent_task_id` is a task in the caller's
    team that the caller can't read. A task that doesn't exist, or sits in
    another team, is left to the structural checks, which answer for it."""
    parent = TaskMaster.objects.filter(task_id=parent_task_id).first()
    if parent is None or str(parent.team_id or "") != ctx.team_id:
        return
    if ctx.user_id not in task_acl_user_ids(
        parent.project_id, parent.assignee_id, parent.reporter_id
    ):
        raise ToolForbidden(f"Not authorized to file a task under task {parent_task_id}.")
