"""`list_project_tags` tool — ONE project's tag catalogue (MIP-707), and the
rest of what a task in it can carry: its custom fields and its saved filters
(MIP-877).

The tags a task in this project can carry (`ProjectTags`), so the agent
can pick a fitting one for `create_task` / `create_task_plan` /
`update_task` — those refuse a name that isn't here.

The same read also returns the project's custom-field definitions
(`ProjectCustomField`: name, type, and the options of a tag field) and its
saved task filters (`ProjectSavedTaskFilter`). They are the project's
vocabulary in the same sense the tags are, they are gated the same way (the
REST lists of all three require project membership), and a caller that wants
one nearly always wants the others, so they ride here rather than in a tool of
their own, which would cost every in-app turn a declaration.

ACL contract (same shape as `list_project_members`):
  * Tenant guard: project.team_id must equal ctx.team_id.
  * Membership guard: ctx.user_id must be in ProjectMembers for the
    project — the REST tag list (`ProjectTagsView.get`) requires the
    same, since a project's tag vocabulary sketches what it works on.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.db.models.functions import Lower

from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectLabelAssignment,
    ProjectMaster,
    ProjectMembers,
    ProjectSavedTaskFilter,
)
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolForbidden,
    wrap_workspace_content,
)
from origin.search_engine.agent.tools.task_custom_fields import field_definitions, project_fields
from origin.search_engine.agent.tools.task_enums import VALID_EFFORTS, VALID_PRIORITIES
from origin.search_engine.agent.tools.task_tags import project_tag_catalogue

# What a saved filter stores for "no restriction on this dimension", and for
# "tasks with none" (no milestone, no sprint, nobody assigned). The app has
# spelled both two ways over time.
_ANY = {"all", "__all__"}
_NONE = {"none", "__none__"}
_STATUSES = {"Open", "WIP", "Blocked", "Pending", "Closed", "Deleted"}


def _labels(raw: Any, known: set[str]) -> list[str]:
    """The labels a saved filter lists for a dimension that has a fixed set of
    values. A value outside the set is not one the product wrote (any member
    can store anything here), so it is fenced rather than passed as a value."""
    if not isinstance(raw, list):
        return []
    items = [
        str(item) for item in raw if isinstance(item, (str, int)) and not isinstance(item, bool)
    ]
    if any(item.casefold() in _ANY for item in items):
        return []
    return [item if item in known else wrap_workspace_content(item) for item in items]


def _keys(raw: Any, *, none_label: str, ids_are_uuids: bool = False) -> list[Any]:
    """Milestone, sprint or member selections: ids, with the "none" sentinel
    spelled `none_label`. Anything else is fenced."""
    if not isinstance(raw, list):
        return []
    items = [item for item in raw if isinstance(item, (str, int)) and not isinstance(item, bool)]
    if any(str(item).casefold() in _ANY for item in items):
        return []
    out: list[Any] = []
    for item in items:
        text = str(item)
        if text.casefold() in _NONE:
            out.append(none_label)
        elif ids_are_uuids:
            try:
                out.append(str(uuid.UUID(text)))
            except ValueError:
                out.append(wrap_workspace_content(text))
        elif text.isdigit():
            out.append(int(text))
        else:
            out.append(wrap_workspace_content(text))
    return out


def saved_filters(project_id: int, team_id: str, tag_names: set[str]) -> list[dict[str, Any]]:
    """The project's saved task filters, by name like the app's menu, with the
    dimensions each one restricts. A dimension a filter does not mention, or sets
    to "All", is left out: it asserts nothing about it."""
    rows = ProjectSavedTaskFilter.objects.filter(team_id=team_id, project_id=project_id).order_by(
        Lower("filter_name")
    )
    out = []
    for row in rows:
        blob = row.filters if isinstance(row.filters, dict) else {}
        entry: dict[str, Any] = {
            "filter_id": row.filter_id,
            "filter_name": wrap_workspace_content(row.filter_name),
        }
        for key, value in (
            ("status", _labels(blob.get("status"), _STATUSES)),
            ("priority", _labels(blob.get("priorities"), VALID_PRIORITIES)),
            ("effort_level", _labels(blob.get("effortLevels"), VALID_EFFORTS)),
            ("tags", _labels(blob.get("tags"), tag_names)),
            ("milestone_ids", _keys(blob.get("milestoneKeys"), none_label="none")),
            ("sprint_ids", _keys(blob.get("sprintKeys"), none_label="none")),
            (
                "assignee_ids",
                _keys(blob.get("memberKeys"), none_label="unassigned", ids_are_uuids=True),
            ),
        ):
            if value:
                entry[key] = value
        out.append(entry)
    return out


def _team_labels(project: ProjectMaster) -> list[dict[str, Any]]:
    """The team's project labels, and whether this project carries each. Labels group
    projects and belong to the team, so every member of the team can read the catalogue
    (`ProjectLabelsView.get`); it rides here because `manage_project_labels` needs the
    ids. The name is user text the model reads and never passes back (it passes the id),
    so it is under its own key, `label_name`, which the result fence wraps."""
    on_project = set(
        ProjectLabelAssignment.objects.filter(project=project.project_id).values_list(
            "label_id", flat=True
        )
    )
    return [
        {
            "label_id": label.label_id,
            "label_name": label.name,
            "color": label.color,
            "on_this_project": label.label_id in on_project,
        }
        for label in ProjectLabel.objects.filter(team=project.team_id).order_by("name")
    ]


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
        # The words of a missing one, so a guessed id does not say which projects exist in
        # other teams (MIP-1519). Only the class differs: it counts toward the MCP refusal
        # alarm. Same pattern as `milestone_access.load_project`.
        raise ToolForbidden(f"Project {project_id} not found.")

    if not ProjectMembers.objects.filter(project_id=project_id, attendee_id=ctx.user_id).exists():
        raise ToolForbidden(
            f"Not authorized to access project {project_id}. You are not a member of that project."
        )

    # Names are returned bare, like task titles in `list_tasks`: they are
    # identifiers the model passes back verbatim to the write tools, and a
    # `<workspace_content>` wrapper would be echoed back as part of the
    # name. `ProjectTags.tag_name` caps them at 20 characters.
    catalogue = project_tag_catalogue(project_id)
    tags = [
        {
            "name": tag["tagName"],
            "color": tag["tagColor"],
            "text_color": tag["tagTextColor"],
        }
        for tag in catalogue
    ]
    custom_fields = field_definitions(project_fields(project_id))
    filters = saved_filters(project_id, ctx.team_id, {tag["tagName"] for tag in catalogue})
    labels = _team_labels(project)
    return {
        "project_id": project_id,
        "project_name": project.project_name,
        "tags": tags,
        "labels": labels,
        "custom_fields": custom_fields,
        "saved_filters": filters,
        "__summary__": (
            f"{len(tags)} tag(s), {len(labels)} label(s), {len(custom_fields)} custom field(s) "
            f"and {len(filters)} saved filter(s) on {project.project_name}"
        ),
    }


LIST_PROJECT_TAGS = Tool(
    name="list_project_tags",
    description=(
        "What a task in ONE project can carry: its tags (name and colours), "
        "its custom fields, and its saved task filters. `tags` are the only "
        "tags a task there can have: call it before setting `tags` on "
        "create_task / create_task_plan / update_task, and pick the tags that "
        "genuinely describe the task; if none fits, leave the task untagged. "
        "To add a tag use `add_project_tag`. `labels` are the team's project "
        "labels, with their `label_id` (`on_this_project` says whether this one "
        "has it). `custom_fields` lists each field the "
        "project defines: `field_name`, `type` (`tag`, `text`, `date` or "
        "`member`) and, for a tag field, the `options` a value is chosen from. "
        "`saved_filters` are the named filter selections the project's members "
        "use in the task table, each listing the dimensions it restricts "
        "(status, priority, effort_level, tags, milestone_ids, sprint_ids, "
        "assignee_ids; 'none' / 'unassigned' mean tasks with none); read them to "
        "learn how the team slices its work. Requires the project_id "
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
