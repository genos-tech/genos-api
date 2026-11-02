"""`delete_sprint` write tool (MIP-882): delete a sprint.

The page's delete, through the one implementation it shares
(`services/sprint_manage.delete_sprint`): the sprint is soft-deleted, its
milestones fall back into "No sprint" and the tasks in them leave with them, in a
way other browsers see (MIP-941). The milestones and tasks themselves are kept.

Its own tool rather than an action of `manage_sprint`, so the MCP opt-in for
destructive tools covers a delete and nothing else. It is destructive there
because nothing restores the sprint from this surface. Deleting a deleted sprint
succeeds.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.sprint_access import load_sprint
from origin.services.sprint_manage import delete_sprint


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    sprint = load_sprint(args.get("sprint_id"), ctx, allow_deleted=True)
    already = sprint.is_deleted
    detached = 0
    if not already:
        try:
            detached = delete_sprint(sprint)
        except Exception as e:  # noqa: BLE001
            ref = log_failure("delete_sprint tool failed", e)
            raise ToolError(f"Failed to delete the sprint (ref {ref}).")
    return {
        "sprint_id": sprint.sprint_id,
        "name": sprint.name,
        "deleted": True,
        "already_deleted": already,
        "milestones_detached": detached,
        "__summary__": (
            f'Sprint "{sprint.name}" was already deleted.'
            if already
            else f'Deleted sprint "{sprint.name}"'
            + (
                f"; its {detached} milestone(s) were kept and no longer belong to a sprint"
                if detached
                else ""
            )
        ),
    }


DELETE_SPRINT = Tool(
    name="delete_sprint",
    description=(
        "Delete a sprint. Call `list_sprints` FIRST for the sprint_id. REQUIRES USER APPROVAL — "
        "destructive and not undoable from here. Its milestones and their tasks are NOT deleted: "
        "they stay in the project, no longer in a sprint. Use it only when the user explicitly "
        "asks to delete that sprint; to change its dates or name use manage_sprint. Required: "
        "sprint_id. Deleting an already-deleted sprint succeeds."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "sprint_id": {
                "type": "INTEGER",
                "description": "Numeric sprint id to delete (from list_sprints).",
            },
        },
        "required": ["sprint_id"],
    },
    run=_run,
    requires_approval=True,
)
