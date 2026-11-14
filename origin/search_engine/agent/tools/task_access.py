"""Load a task the caller may write to, for the comment and body tools (MIP-880).

The same checks `add_comment` and `update_task` make in line: the task exists
and is not deleted, it is in the caller's team, and the caller is a project
member, the assignee or the reporter (the read audience of `fetch_task`).
A user who can't see the task can't change anything on it.
"""

from __future__ import annotations

from typing import Any

from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden


def as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"`{name}` must be an integer (got {value!r}).")


def load_task(raw_task_id: Any, ctx: ToolContext, *, action: str) -> TaskMaster:
    task_id = as_int(raw_task_id, "task_id")
    try:
        task = TaskMaster.objects.get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        raise ToolError(f"Task {task_id} not found.")
    if str(getattr(task, "team_id", "") or "") != ctx.team_id:
        # The words of a missing one, so a guessed id does not say which tasks exist in other
        # teams (MIP-1531); the class still counts toward the MCP refusal alarm.
        raise ToolForbidden(f"Task {task_id} not found.")
    if task.is_deleted:
        raise ToolError(f"Task {task_id} has been deleted.")
    allowed = task_acl_user_ids(
        getattr(task, "project_id", None),
        getattr(task, "assignee_id", None),
        getattr(task, "reporter_id", None),
    )
    if ctx.user_id not in allowed:
        raise ToolForbidden(f"Not authorized to {action} task {task_id}.")
    return task


def load_own_comment(
    task: TaskMaster, raw_comment_id: Any, ctx: ToolContext, *, allow_deleted: bool = False
) -> TaskComments:
    """The caller's own comment on `task`.

    Someone else's comment is answered exactly like a missing one, as the page
    does: it only offers edit and delete on your own, and saying "that is not
    yours" would say it exists.
    """
    comment_id = as_int(raw_comment_id, "comment_id")
    comment = TaskComments.objects.filter(task=task.task_id, comment_id=comment_id).first()
    if (
        comment is None
        or str(comment.sender_id) != ctx.user_id
        or (comment.is_deleted and not allow_deleted)
    ):
        raise ToolError(
            f"Comment {comment_id} not found on {task.display_id}. Only your own comments can "
            "be changed; get the `comment_id` from `fetch_task`."
        )
    return comment


def load_comment(task: TaskMaster, raw_comment_id: Any, ctx: ToolContext) -> TaskComments:
    """Any live comment on `task`, whoever wrote it: for what the page lets a person do to a
    colleague's comment (react to it, flag it, add it to the to-do list). `task` came from
    `load_task`, so the caller may open it. A deleted comment, or an id the task has no comment
    for, reads as a missing one."""
    comment_id = as_int(raw_comment_id, "comment_id")
    comment = TaskComments.objects.filter(
        task=task.task_id, comment_id=comment_id, is_deleted=False
    ).first()
    if comment is None:
        raise ToolError(
            f"Comment {comment_id} not found on {task.display_id}. Get the `comment_id` from "
            "`fetch_task`."
        )
    return comment
