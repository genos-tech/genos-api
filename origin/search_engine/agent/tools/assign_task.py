"""`assign_task` write tool — set or clear a task's assignee.

A dedicated tool (rather than reusing `update_task`) so:
  (a) the approval card shows "assign_task" with a clear
      assignee_username, not a raw UUID buried in update_task args;
  (b) the model has an unambiguous primitive for "assign this to me /
      to John / unassign" without having to compose a partial update.

ACL contract (three layers):
  1. Tenant guard: task.team_id must equal ctx.team_id.
  2. Editor guard: ctx.user_id must be in task_acl_user_ids(…) — the
     same set that `fetch_task` and `update_task` enforce.  A user who
     cannot read a task cannot assign it.
  3. Assignee guard: when assigning to another user, that user must be
     an active member of ctx.team_id OR already able to see the task
     (in the same `task_acl_user_ids` set as layer 2). Either way the
     candidate set is bounded by this tenant, so the LLM cannot assign
     to an arbitrary UUID that happens to be valid in another one.

     The second half of that "or" is what a shared project needs: an
     external collaborator holds a `ProjectMembers` row and no
     `TeamMembers` row — that absence IS the guest model — so a
     membership-only rule refused the very people the project was shared
     with, while still listing them in `list_project_members`. The first
     half stays because a task need not belong to a project at all, and
     its ACL is then just {assignee, reporter}.
"""

from __future__ import annotations

from typing import Any

from origin.models.common.user_models import CustomUser
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.views.utils.scope_guards import is_team_member


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # --- Resolve and validate task_id ---
    raw_task_id = args.get("task_id")
    try:
        task_id = int(raw_task_id)
    except (TypeError, ValueError):
        raise ToolError(f"`task_id` must be an integer (got {raw_task_id!r}).")

    try:
        task = TaskMaster.objects.get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        raise ToolError(f"Task {task_id} not found.")

    if task.is_deleted:
        raise ToolError(f"Task {task_id} has been deleted.")

    # Layer 1 — tenant guard.
    if str(getattr(task, "team_id", "") or "") != ctx.team_id:
        raise ToolError("Not authorized: task is in a different team.")

    # Layer 2 — editor guard (same set as fetch_task / update_task).
    allowed = task_acl_user_ids(
        getattr(task, "project_id", None),
        getattr(task, "assignee_id", None),
        getattr(task, "reporter_id", None),
    )
    if ctx.user_id not in allowed:
        raise ToolError(f"Not authorized to update task {task_id}.")

    # --- Resolve the new assignee (or None for unassign). ---
    raw_assignee_id = args.get("assignee_id")
    new_assignee_id: str | None = None
    new_assignee_username: str | None = None

    if raw_assignee_id and raw_assignee_id != "null":
        # Layer 3 — a teammate, or someone the task's own ACL already
        # covers. Both sets are scoped to this team, so neither can reach
        # a UUID from a different tenant.
        assignable = str(raw_assignee_id) in allowed or is_team_member(ctx.team_id, raw_assignee_id)
        if not assignable:
            raise ToolError(
                f"User {raw_assignee_id!r} is not an active member of this team, "
                "and is not on this task's project. Use get_team_members or "
                "list_project_members to find valid user ids."
            )

        try:
            assignee = CustomUser.objects.get(id=raw_assignee_id)
        except (CustomUser.DoesNotExist, Exception):
            raise ToolError(f"User {raw_assignee_id!r} not found.")

        if assignee.is_deleted:
            raise ToolError(f"User {raw_assignee_id!r} account has been deleted.")

        new_assignee_id = str(assignee.id)
        new_assignee_username = assignee.username or str(assignee.id)

    # --- Apply the change. ---
    task.assignee_id = new_assignee_id
    try:
        # "ts_updated_at" is required alongside the changed column —
        # auto_now only fires for fields named in update_fields, and the
        # incremental reindexer keys off it. See update_task.
        task.save(update_fields=["assignee_id", "ts_updated_at"])
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"Failed to assign task: {e}")

    if new_assignee_username:
        summary = f"Assigned task #{task_id} to {new_assignee_username}"
    else:
        summary = f"Unassigned task #{task_id}"

    return {
        "task_id": task_id,
        "assignee_id": new_assignee_id,
        "assignee_username": new_assignee_username,
        "__summary__": summary,
    }


ASSIGN_TASK = Tool(
    name="assign_task",
    description=(
        "Assign or unassign a task. REQUIRES USER APPROVAL — the user "
        "sees the proposed assignment before it is saved. "
        "To assign: pass task_id + assignee_id (a UUID from "
        "get_current_user or get_team_members). "
        "To unassign: omit assignee_id or pass null. "
        "The new assignee must be a member of the current team, or "
        "someone already on the task's project. "
        "Use get_current_user first when the user says 'assign to me'."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "Numeric task id to assign.",
            },
            "assignee_id": {
                "type": "STRING",
                "description": (
                    "UUID of the user to assign. Omit or pass null to "
                    "unassign. Resolve names to UUIDs with "
                    "get_team_members."
                ),
            },
        },
        "required": ["task_id"],
    },
    run=_run,
    requires_approval=True,
)
