"""`list_project_tags` tool — ONE project's tag catalogue (MIP-707).

The tags a task in this project can carry (`ProjectTags`), so the agent
can pick a fitting one for `create_task` / `create_task_plan` /
`update_task` — those refuse a name that isn't here.

ACL contract (same shape as `list_project_members`):
  * Tenant guard: project.team_id must equal ctx.team_id.
  * Membership guard: ctx.user_id must be in ProjectMembers for the
    project — the REST tag list (`ProjectTagsView.get`) requires the
    same, since a project's tag vocabulary sketches what it works on.
"""

from __future__ import annotations

from typing import Any

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.task_tags import project_tag_catalogue


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_project_id = args.get("project_id")
    try:
        project_id = int(raw_project_id)
    except (TypeError, ValueError):
        raise ToolError(f"`project_id` must be an integer (got {raw_project_id!r}).")

    try:
        project = ProjectMaster.objects.get(project_id=project_id, is_deleted=False)
    except ProjectMaster.DoesNotExist:
        raise ToolError(f"Project {project_id} not found.")

    if str(getattr(project, "team_id", "") or "") != ctx.team_id:
        raise ToolForbidden("Not authorized: project belongs to a different team.")

    if not ProjectMembers.objects.filter(project_id=project_id, attendee_id=ctx.user_id).exists():
        raise ToolForbidden(
            f"Not authorized to access project {project_id}. You are not a member of that project."
        )

    # Names are returned bare, like task titles in `list_tasks`: they are
    # identifiers the model passes back verbatim to the write tools, and a
    # `<workspace_content>` wrapper would be echoed back as part of the
    # name. `ProjectTags.tag_name` caps them at 20 characters.
    tags = [
        {
            "name": tag["tagName"],
            "color": tag["tagColor"],
            "text_color": tag["tagTextColor"],
        }
        for tag in project_tag_catalogue(project_id)
    ]
    return {
        "project_id": project_id,
        "project_name": project.project_name,
        "tags": tags,
        "__summary__": f"{len(tags)} tag(s) on {project.project_name}",
    }


LIST_PROJECT_TAGS = Tool(
    name="list_project_tags",
    description=(
        "List the tags defined on ONE project (name and colours) — the only "
        "tags a task in that project can carry. Call it before setting "
        "`tags` on create_task / create_task_plan / update_task, and pick "
        "the tags that genuinely describe the task; if none fits, leave the "
        "task untagged. Tags can't be created here. Requires the project_id "
        "(from list_projects). You must be a member of the project."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "project_id": {
                "type": "INTEGER",
                "description": "The project's integer id (from list_projects).",
            },
        },
        "required": ["project_id"],
    },
    run=_run,
)
