"""`add_project_tag` write tool (MIP-882): add a tag to a project's tag list.

Until now the task tools refused any tag the project did not already have, and nothing
could add one, so "tag it `urgent`" failed whenever the tag was new. A tag is put on
tasks by name and a task keeps a copy of it, which is why only adding is here: renaming
or deleting a tag rewrites every task that carries it, and stays in the project's tag
manager.

The page's rule is kept: any member of the project may add one. A tool is stricter in
one way, through `origin.services.project_catalog`: a name that differs from an existing
tag only in case is refused, since the task tools match tags by name.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.project_access import load_project, require_member
from origin.services.project_catalog import CatalogRefused, add_project_tag


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    project = load_project(args.get("project_id"), ctx)
    require_member(project, ctx)
    try:
        tag = add_project_tag(project, args.get("tag_name"), args.get("color"))
    except CatalogRefused as refused:
        raise ToolError(f"{refused.message} Nothing was added.")
    except Exception as e:  # noqa: BLE001
        failure = log_failure("add_project_tag failed", e)
        raise ToolError(f"Failed to add the tag (ref {failure}).")
    return {
        "project_id": project.project_id,
        "tag_name": tag.tag_name,
        "color": tag.tag_color,
        "__summary__": f'Added the tag "{tag.tag_name}" to "{project.project_name}"',
    }


ADD_PROJECT_TAG = Tool(
    name="add_project_tag",
    description=(
        "Add a tag to a project's tag list, so its tasks can carry it. REQUIRES USER "
        "APPROVAL. Call `list_project_tags` FIRST: it shows the tags that exist, and a "
        "name that differs only in case is refused. Any member of the project may add "
        "one. Tags can't be renamed or deleted here."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "project_id": {
                "type": "INTEGER",
                "description": "The project (from list_projects).",
            },
            "tag_name": {
                "type": "STRING",
                "description": "The tag's name, up to 20 characters.",
            },
            "color": {
                "type": "STRING",
                "description": "Optional hex colour like #3b82f6. Grey if left out.",
            },
        },
        "required": ["project_id", "tag_name"],
    },
    run=_run,
    requires_approval=True,
)
