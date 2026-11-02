"""`delete_milestone` write tool (MIP-879).

Deletes a milestone exactly as `MilestoneView.delete` does, through the one
implementation they share (`services/milestone_manage.delete_milestone`): the
milestone and its backing task row are soft-deleted, its tasks (and anything
filed under the backing row) are detached, the project's task cache is
dropped and the search index forgets both. The tasks themselves are kept.

Its own tool rather than an action of `manage_milestone`, so the MCP opt-in for
destructive tools covers the delete and nothing else: a key without it can
still create and edit milestones.

`update_task` still refuses `status: 'Deleted'` on a milestone's backing row
(MIP-876): that is only half of a delete.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.list_milestones import _backing_display_id
from origin.search_engine.agent.tools.milestone_access import load_milestone
from origin.services.milestone_manage import delete_milestone
from origin.services.milestone_rollup import milestone_work_q


def milestone_task_count(milestone) -> int:
    """How many live tasks are in the milestone (the count `list_milestones`
    shows as `tasks_total`). They are what a delete detaches."""
    return TaskMaster.objects.filter(milestone_work_q(milestone)).distinct().count()


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    milestone = load_milestone(args.get("milestone_id"), ctx, allow_deleted=True)
    label = _backing_display_id(milestone) or f"milestone {milestone.milestone_id}"
    payload = {
        "milestone_id": milestone.milestone_id,
        "task_id": milestone.task_id,
        "display_id": _backing_display_id(milestone),
        "project_id": milestone.project_id,
        "project_name": milestone.project.project_name,
        "title": milestone.title,
    }
    if milestone.is_deleted:
        return {
            **payload,
            "deleted": True,
            "already_deleted": True,
            "tasks_detached": 0,
            "__summary__": f"{label} was already deleted.",
        }

    detached = milestone_task_count(milestone)
    try:
        delete_milestone(milestone)
    except Exception as e:  # noqa: BLE001
        ref = log_failure("delete_milestone tool failed", e)
        raise ToolError(f"Failed to delete the milestone (ref {ref}).")
    return {
        **payload,
        "deleted": True,
        "already_deleted": False,
        "tasks_detached": detached,
        "__summary__": (
            f'Deleted milestone {label} "{milestone.title}"'
            + (
                f"; its {detached} task(s) were kept and no longer belong to a milestone"
                if detached
                else ""
            )
        ),
    }


DELETE_MILESTONE = Tool(
    name="delete_milestone",
    description=(
        "Delete a milestone. REQUIRES USER APPROVAL — destructive and not undoable from here: "
        "the milestone disappears from its project and from search. Its tasks are NOT "
        "deleted: they stay in the project, no longer in the milestone. Use it only when the "
        "user explicitly asks to delete that milestone; to finish one, set its status to "
        "Closed with manage_milestone instead. Required: milestone_id (from list_milestones). "
        "Deleting an already-deleted milestone succeeds."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "milestone_id": {
                "type": "INTEGER",
                "description": "Numeric milestone id to delete (from list_milestones).",
            },
        },
        "required": ["milestone_id"],
    },
    run=_run,
    requires_approval=True,
)
