"""`manage_project_labels` write tool (MIP-882): create a project label, or put labels on a
project and take them off.

Labels group PROJECTS and belong to the whole team ("Client Work", "Q3"); they are not
the tags that go on a project's tasks (`add_project_tag`). A project points at a label by
id, so renaming or deleting one reaches every project that carries it, which is why only
creating, putting on and taking off are here.

The page's rules, kept, through `origin.services.project_catalog`: a name is 1 to 30
characters and unique in the team whatever its case, and every action is for the owner or
an editor of the project the call acts from. A new label is visible to the whole team, so
the call names a project to act from, as the page does.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.project_access import load_project, require_manager
from origin.services.project_catalog import (
    DEFAULT_LABEL_COLOR,
    CatalogRefused,
    assign_labels,
    clean_color,
    create_label,
    labels_of,
    text_color_for,
    unassign_labels,
)

_ACTIONS = ("create", "assign", "unassign")


def _label_ids(raw: Any) -> list[int]:
    if not isinstance(raw, list) or not raw:
        raise ToolError("`label_ids` must be a list of label ids (from list_project_tags).")
    ids: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not (
            isinstance(item, int) or (isinstance(item, str) and item.strip().isdigit())
        ):
            raise ToolError(f"`label_ids` must be whole numbers (got {item!r}).")
        value = int(item)
        if value not in ids:
            ids.append(value)
    return ids


def _labels_now(project) -> list[dict[str, Any]]:
    return [{"label_id": label.label_id, "label_name": label.name} for label in labels_of(project)]


def _create(args: dict[str, Any], project, ctx: ToolContext) -> dict[str, Any]:
    try:
        color = clean_color(args.get("color"), DEFAULT_LABEL_COLOR)
        label = create_label(
            team_id=project.team_id,
            user=CustomUser.objects.filter(id=ctx.user_id).first(),
            raw_name=args.get("name"),
            color=color,
            text_color=text_color_for(color),
        )
    except CatalogRefused as refused:
        raise ToolError(f"{refused.message} Nothing was created.")
    return {
        "label_id": label.label_id,
        "label_name": label.name,
        "color": label.color,
        "__summary__": f'Created the label "{label.name}"',
    }


def _change(action: str, args: dict[str, Any], project) -> dict[str, Any]:
    ids = _label_ids(args.get("label_ids"))
    done, unknown = (assign_labels if action == "assign" else unassign_labels)(project, ids)
    if unknown:
        raise ToolError(
            f"Label id(s) {unknown} are not labels of this team. Get label_ids from "
            "`list_project_tags`. Nothing was changed."
        )
    name = project.project_name
    if action == "assign":
        summary = (
            f'Put {len(done)} label(s) on "{name}"'
            if done
            else f'"{name}" already had those labels'
        )
    else:
        summary = (
            f'Took {len(done)} label(s) off "{name}"'
            if done
            else f'"{name}" had none of those labels'
        )
    return {
        "project_id": project.project_id,
        "changed_label_ids": done,
        "labels": _labels_now(project),
        "__summary__": summary,
    }


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = args.get("action")
    if action not in _ACTIONS:
        raise ToolError(f"`action` must be one of {list(_ACTIONS)} (got {action!r}).")
    project = load_project(args.get("project_id"), ctx)
    require_manager(project, ctx, "manage project labels")
    try:
        if action == "create":
            return _create(args, project, ctx)
        return _change(action, args, project)
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        failure = log_failure("manage_project_labels failed", e)
        raise ToolError(f"Failed to change the labels (ref {failure}).")


MANAGE_PROJECT_LABELS = Tool(
    name="manage_project_labels",
    description=(
        "Create a project label, or put labels on a project or take them off. REQUIRES USER "
        "APPROVAL. Labels group PROJECTS and belong to the team; tasks carry tags "
        "(`add_project_tag`). Call `list_project_tags` FIRST for the team's labels and their "
        "`label_id`. action 'create': `name` (up to 30 characters, unique in the team) and an "
        "optional `color`. 'assign' / 'unassign': `label_ids` on `project_id`. Only the "
        "project's owner or an editor. Labels can't be renamed or deleted here."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": list(_ACTIONS),
                "description": "'create' a label, or 'assign' / 'unassign' labels on a project.",
            },
            "project_id": {
                "type": "INTEGER",
                "description": "The project to act from or change (from list_projects).",
            },
            "name": {
                "type": "STRING",
                "description": "create: the label's name, up to 30 characters.",
            },
            "color": {
                "type": "STRING",
                "description": "create: optional hex colour like #7c3aed.",
            },
            "label_ids": {
                "type": "ARRAY",
                "items": {"type": "INTEGER"},
                "description": "assign / unassign: label ids (from list_project_tags).",
            },
        },
        "required": ["action", "project_id"],
    },
    run=_run,
    requires_approval=True,
)
