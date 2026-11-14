"""Where a to-do came from: `create_todo_item`'s `task_id` and `comment_id` (MIP-1562, flag
`AGENT_PARITY_TOOLS`).

The app's "Add to To-Do" on a task comment (or on a task's card in the project chat) makes a
to-do whose notes open with one italic line, "From task comment: <link>", and nothing else
(`appendTodoFromMessage.ts`, MIP-1434 / 1436). There is no column behind it: the link in the
notes is the whole "link back". This builds that same line for the tool, from a comment or a task
the caller can open, so a to-do the agent made reads and opens like one the person made.

The label is English: the app words it in the viewer's locale at the moment it makes the to-do,
and this is text stored in the notes. A to-do made here says "From task comment" to everyone.

Declared on the registry tool only through `PARITY_PROPERTIES`, laid over the schema while the
flag is on (`tool_flag_params`); with the flag off nothing here runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.task_access import load_comment, load_task

LABEL_COMMENT = "From task comment"
LABEL_TASK = "From task"

#: What `AGENT_PARITY_TOOLS` adds to `create_todo_item`'s schema (`tool_flag_params`). Kept terse:
#: every character is paid on every call. `task_id` is declared as the reference form the task
#: tools take (a display id, a URL or a number); the tool resolves it itself, so a flag-off call
#: that sends one anyway never reaches the resolver.
PARITY_PROPERTIES: dict[str, dict[str, Any]] = {
    "task_id": {
        "type": "STRING",
        "description": "Optional: the task this to-do comes from (PRJ-123, URL or id); links back to it.",
    },
    "comment_id": {
        "type": "INTEGER",
        "description": "Optional, with task_id: the task comment it comes from; links to it.",
    },
}

#: The sentence the flag adds to the description (`tool_flag_text`).
DESCRIPTION_ADDITION = (
    " `task_id` / `comment_id` link the to-do to a task or comment, as the app's Add to To-Do does."
)


@dataclass(frozen=True)
class TodoSource:
    task: TaskMaster
    comment: TaskComments | None
    href: str
    label: str


def source_from(args: dict[str, Any], ctx: ToolContext) -> TodoSource | None:
    """The task or comment the call names, checked the way the page checks it, or None when the
    call names neither. A task the caller cannot open, a comment that is not there (or is
    deleted) and a task with no project to link read the same as a missing one."""
    # Imported here: the MCP package pulls in the view layer, which imports the tool registry.
    from origin.views.public.mcp.resolve import resolve_task_ref  # noqa: PLC0415

    raw_task, raw_comment = args.get("task_id"), args.get("comment_id")
    if raw_task in (None, "") and raw_comment in (None, ""):
        return None
    if raw_task in (None, ""):
        raise ToolError("`comment_id` needs `task_id`: the task the comment is on.")
    task = load_task(resolve_task_ref(raw_task, ctx.team_id), ctx, action="link a to-do to")
    comment = None
    if raw_comment not in (None, ""):
        comment = load_comment(task, raw_comment, ctx)
    if task.project_id is None:
        raise ToolError(
            f"{task.display_id} has no project, so there is no page to link the to-do to."
        )
    path = f"/workspace/tasks/project/{task.project_id}/task/{task.task_id}"
    if comment is not None:
        path += f"/comment/{comment.comment_id}"
    return TodoSource(
        task=task,
        comment=comment,
        href=f"{settings.FRONTEND_BASE_URL.rstrip('/')}{path}",
        label=LABEL_COMMENT if comment is not None else LABEL_TASK,
    )


def source_block(source: TodoSource) -> dict[str, Any]:
    """The notes' first block: `From task comment: <link>`, shaped as the app writes it."""
    return {
        "id": str(uuid.uuid4()),
        "type": "paragraph",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": [
            {"type": "text", "text": f"{source.label}: ", "styles": {"italic": True}},
            {
                "type": "link",
                "href": source.href,
                "content": [{"type": "text", "text": source.href, "styles": {}}],
            },
        ],
        "children": [],
    }
