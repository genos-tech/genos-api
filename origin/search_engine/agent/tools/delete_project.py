"""`delete_project` write tool (MIP-882): delete a project and everything in it.

In the APP ONLY. It is never published over MCP (Ken's decision 3): it destroys a project's
tasks, milestones, sprints, members and tags with no undo, and a connected agent has no
approval card to put in front of that. The in-app agent has one, and so does this tool; it
asks for more than the page does:

* **The name, as well as the id.** `project_name` must match the project's name exactly, so
  a wrong id or a project looked up by an earlier, different name is caught before it is
  deleted, and the person sees on the card which project is meant.
* **The owner only**, as on the page. An editor or a plain member is refused.

What goes is what the page's dialog lists. The project's chat disappears for everyone but its
messages are kept (see `origin.services.project_delete`).

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolForbidden,
    wrap_workspace_content,
)
from origin.search_engine.agent.tools.project_access import load_project, require_member
from origin.services.project_delete import delete_project, project_contents


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = load_project(args.get("project_id"), ctx)
    require_member(project, ctx)
    if str(project.owner_id or "") != ctx.user_id:
        raise ToolForbidden("Only the project owner can delete the project.")
    given = args.get("project_name")
    if not isinstance(given, str) or given != project.project_name:
        # The real name is user text, so it comes back fenced.
        raise ToolError(
            f"`project_name` must be the project's name exactly: project {project.project_id} is "
            f"named {wrap_workspace_content(project.project_name)}. Check the id with "
            "`list_projects` and ask the user if it is the right project. Nothing was deleted."
        )
    name, project_id = project.project_name, project.project_id
    contents = project_contents(project)
    try:
        delete_project(project)
    except Exception as e:  # noqa: BLE001
        failure = log_failure("delete_project failed", e)
        raise ToolError(f"Failed to delete the project (ref {failure}).")
    return {
        "project_id": project_id,
        "project_name": name,
        "deleted": True,
        "contents": contents,
        "__summary__": (
            f'Deleted the project "{name}" with {contents["tasks"]} task(s), '
            f"{contents['milestones']} milestone(s) and {contents['sprints']} sprint(s). "
            "This cannot be undone."
        ),
    }


DELETE_PROJECT = Tool(
    name="delete_project",
    description=(
        "Delete a project for good: its tasks (with their comments, attachments and notes), "
        "milestones, sprints, members and tags, with no undo. REQUIRES USER APPROVAL. Only when "
        "the user clearly asked for that project to go. Call `list_projects` FIRST for the "
        "project_id and its exact name. Only the project's owner. Its chat disappears for "
        "everyone; the messages are kept but nobody can open them."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "project_id": {
                "type": "INTEGER",
                "description": "The project to delete (from list_projects).",
            },
            "project_name": {
                "type": "STRING",
                "description": "The project's name, exactly as list_projects gives it.",
            },
        },
        "required": ["project_id", "project_name"],
    },
    run=_run,
    requires_approval=True,
)
