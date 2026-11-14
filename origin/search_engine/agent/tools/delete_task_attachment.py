"""`delete_task_attachment` write tool (MIP-1563): take one file off a task.

The Attachments tab's delete, through the one implementation it shares
(`services/task_attachment_manage.delete_attachment`): the file leaves storage, the row goes, and the
task's history records an `attachment_removed` row. It touches no search index (attachments are not
indexed), as the page never did.

Its own tool rather than an option of `update_task`, so the MCP opt-in for destructive tools covers a
delete and nothing else (the reasoning of `delete_comment`); a delete hidden in an update is the
shape `DELETING_ARGUMENTS` exists to patch up, not one to add.

The rule is the page's: anyone who can open the task may delete any of its files, whoever uploaded
it (`can_access_task`; here `task_access.load_task`, which is the same audience and also refuses
another team's task and a deleted one). It is destructive and has no undo: the file is gone from
storage, so there is nothing to put back (`undo.UNDO_SUPPORT`).

The attachment is looked up by `(task, attachment_id)` and nothing else, so an id that exists only
on another task is "not found" on this one and that task's file is never touched.

Registered flag-gated (`register_flagged_tool`, flag `AGENT_PARITY_TOOLS`): with the flag off the
tool is not declared, not runnable and not published.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.task.task_models import TaskAttachments
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    wrap_workspace_content,
)
from origin.search_engine.agent.tools.task_access import as_int, load_task
from origin.search_engine.agent.tools.task_attachments import attachment_name, attachment_size
from origin.services.task_attachment_manage import delete_attachment


def find_attachment(task_id: int, attachment_id: int) -> TaskAttachments | None:
    """The file numbered `attachment_id` ON THIS TASK, or None. Nothing else selects the row, so
    another task's file with the same (or any other) number is never reached."""
    return TaskAttachments.objects.filter(task=task_id, attachment_id=attachment_id).first()


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    task = load_task(args.get("task_id"), ctx, action="delete a file from")
    attachment_id = as_int(args.get("attachment_id"), "attachment_id")
    attachment = find_attachment(task.task_id, attachment_id)
    if attachment is None:
        raise ToolError(
            f"File {attachment_id} not found on {task.display_id}. Get the `attachment_id` from "
            "`fetch_task` (its `attachments`)."
        )
    name = attachment_name(attachment)
    size = attachment_size(attachment)
    try:
        delete_attachment(attachment)
    except Exception as e:  # noqa: BLE001
        ref = log_failure("delete_task_attachment tool failed", e)
        raise ToolError(f"Failed to delete the file (ref {ref}).")
    left = TaskAttachments.objects.filter(task=task.task_id).count()
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "attachment_id": attachment_id,
        "name": wrap_workspace_content(name),
        "size_bytes": size,
        "deleted": True,
        "files_left": left,
        "__summary__": f"Deleted file {name} from {task.display_id}",
    }


DELETE_TASK_ATTACHMENT = Tool(
    name="delete_task_attachment",
    description=(
        "Delete one file attached to a task. Call `fetch_task` FIRST: its `attachments` list each "
        "file's `attachment_id`, name, size and type. REQUIRES USER APPROVAL — destructive: the "
        "file leaves the task and storage for good and can't be brought back from here. Use it "
        "only when the user explicitly asks to delete that file, never to show or download one. "
        "Anyone who can open the task may delete its files, as in the app. Required: task_id, "
        "attachment_id."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "Numeric task id the file is attached to.",
            },
            "attachment_id": {
                "type": "INTEGER",
                "description": "The file's id on that task, from fetch_task.",
            },
        },
        "required": ["task_id", "attachment_id"],
    },
    run=_run,
    requires_approval=True,
)
