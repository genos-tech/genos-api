"""A project's task field rules, as the agent and MCP tools meet them (MIP-1589, flag `AGENT_PARITY_FOLLOWUPS`).

A project's owner or an editor can make some metadata REQUIRED on the tasks created under it
(`ProjectMaster.task_field_rules`, `ProjectTaskFieldRulesView`: due date, effort level, priority, tags,
reporter, assignee). The app's own forms enforce that in the browser (`taskFieldRules.ts`:
`getMissingRequiredFields`), and the REST create endpoint never looks at the rules, so before this flag a
task made by `create_task`, `create_task_plan` or an MCP client could be one the app's form would have
refused. With the flag on the tools hold their callers to the same rules:

  * `create_task`, and each task (and a new milestone) of `create_task_plan`: a task that lacks a field
    the project requires is refused, in a sentence that names the fields and says nothing was created,
    so the model asks the member for them (or sets them when the member already said). A plan is refused
    as it is today for any bad row: whole, with the row named.
  * `update_task`: only a call that CLEARS a required field is refused (`due_date: ""`, `tags: []`). The app
    enforces the rules at creation only and does not guard an edit, so an unrelated edit of an older task that
    never had the field is left alone: the rule is not applied to a task retroactively.
  * In the app the refusal reaches the model BEFORE the approval card (`proposal_refusal`, called by the loop
    where it turns an unknown reference away), so a member is never asked to approve a create that would fail;
    the tool runs the same check again when it executes (an MCP call, an approval that waited, a rule
    changed in the meantime).

What this copies from the browser is the small, server-natural part: which fields are required AND active
(`tags` is inactive while the project has no tags at all: "a field with no options cannot be required"), and
that an empty string or null is unset. The reporter is never missing: a task made here is reported by its
caller, as the form seeds the creator. DEFAULTS are not applied (the app's form fills them; the tools do not
change what the model sent): a required field that has a default says so in the refusal, so the model can
offer it.

Read side: `list_project_tags` returns `task_field_rules` (what is required and the defaults) and
`task_templates` (the names of the project's own templates, never their bodies) while the flag is on. Same
ACL as the app's own reads: `ProjectTaskFieldRulesView.get` and `ProjectTaskTemplateView.get` both require
membership of the project, and so does `list_project_tags`. Everything returned is validated and short: a
priority or effort label from the fixed lists, a number of days, tag names that exist, a user id, or a
template name fenced as the workspace's own text.
"""

from __future__ import annotations

import uuid
from typing import Any

from origin.models.project.prj_models import ProjectMaster, ProjectTaskTemplate
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import ToolContext, wrap_workspace_content
from origin.search_engine.agent.tools.task_enums import VALID_EFFORTS, VALID_PRIORITIES
from origin.search_engine.agent.tools.task_tags import project_tag_catalogue

#: The configurable fields in the order the app lists them (`getMissingRequiredFields`), with the name
#: the tools use for each.
FIELDS: tuple[tuple[str, str], ...] = (
    ("assignee", "assignee_id"),
    ("reporter", "reporter_id"),
    ("tags", "tags"),
    ("priority", "priority"),
    ("effortLevel", "effort_level"),
    ("dueDate", "due_date"),
)
#: A new task made by a tool is always reported by its caller, so this one can never be missing.
_ALWAYS_SET = "reporter_id"
#: Fields an update can CLEAR (`update_task`: `due_date: ""`, `tags: []`; priority and effort level
#: cannot be cleared by it, and the assignee has its own tool).
CLEARABLE = ("tags", "due_date")
MAX_TEMPLATE_NAMES = 50


def project_rules(project: ProjectMaster) -> dict[str, dict[str, Any]]:
    """The project's rules blob as a `{field: config}` map of the six known fields; anything else
    (an unknown key, a config that is not an object) is no rule."""
    raw = project.task_field_rules
    if not isinstance(raw, dict):
        return {}
    known = {key for key, _ in FIELDS}
    return {key: cfg for key, cfg in raw.items() if key in known and isinstance(cfg, dict)}


def _is_set(value: Any) -> bool:
    """The app's `isSet`: neither null nor the empty string. A model's "null" for a person is
    also no person."""
    return value is not None and value != "" and value != "null"


def _has_tags(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(n, str) and n.strip() for n in value)


def required_params(
    project: ProjectMaster, *, tags_exist: bool | None = None
) -> list[tuple[str, str]]:
    """`(rule key, tool parameter)` of every field the project requires AND the app would enforce, in
    the app's order. `tags` is inactive while the project has no tags (`tags_exist` False; None looks).
    The reporter is left out (see `_ALWAYS_SET`)."""
    rules = project_rules(project)
    out = []
    for key, param in FIELDS:
        if param == _ALWAYS_SET or not rules.get(key, {}).get("required"):
            continue
        if key == "tags":
            if tags_exist is None:
                tags_exist = bool(project_tag_catalogue(project.project_id))
            if not tags_exist:
                continue
        out.append((key, param))
    return out


def missing_fields(
    project: ProjectMaster, values: dict[str, Any], *, tags_exist: bool | None = None
) -> list[str]:
    """The tool parameters the project requires and `values` leaves unset. `values` is keyed by
    parameter name (`assignee_id`, `tags` (the list the caller sent), `priority`, `effort_level`,
    `due_date`)."""
    out = []
    for _key, param in required_params(project, tags_exist=tags_exist):
        value = values.get(param)
        if (not _has_tags(value)) if param == "tags" else (not _is_set(value)):
            out.append(param)
    return out


def defaults(project: ProjectMaster) -> dict[str, Any]:
    """What the project defaults each configured field to, in the model's words, and only values that
    are safe to show: a fixed label, a count of days, the names of tags that still exist, a user id.
    The app drops a stale default silently, so does this."""
    rules = project_rules(project)
    out: dict[str, Any] = {}
    assignee = rules.get("assignee", {}).get("default")
    if assignee == "creator":
        out["assignee_id"] = "the member asking"
    elif isinstance(assignee, str) and _is_uuid(assignee):
        out["assignee_id"] = assignee
    cfg = rules.get("priority", {})
    if cfg.get("default") in VALID_PRIORITIES:
        out["priority"] = cfg["default"]
    cfg = rules.get("effortLevel", {})
    if cfg.get("default") in VALID_EFFORTS:
        out["effort_level"] = cfg["default"]
    offset = rules.get("dueDate", {}).get("defaultOffsetDays")
    if isinstance(offset, int) and not isinstance(offset, bool) and 0 <= offset <= 3650:
        out["due_date"] = f"today + {offset} days"
    names = rules.get("tags", {}).get("defaultTagNames")
    if isinstance(names, list) and names:
        live = {tag["tagName"] for tag in project_tag_catalogue(project.project_id)}
        kept = [n for n in names if isinstance(n, str) and n in live]
        if kept:
            out["tags"] = kept
    return out


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def rules_block(project: ProjectMaster) -> dict[str, Any]:
    """`task_field_rules` of the `list_project_tags` result."""
    required = [param for _key, param in required_params(project)]
    return {"required": required, "defaults": defaults(project)}


def templates_block(project: ProjectMaster) -> dict[str, Any]:
    """`task_templates` of the `list_project_tags` result: the NAMES of the project's own task
    templates (a task names one with `template`), fenced because a member wrote them, and which one
    is the default."""
    rows = list(
        ProjectTaskTemplate.objects.filter(project_id=project.project_id)
        .order_by("template_name", "pk")
        .values_list("pk", "template_name")[: MAX_TEMPLATE_NAMES + 1]
    )
    shown = rows[:MAX_TEMPLATE_NAMES]
    out: dict[str, Any] = {"names": [wrap_workspace_content(name) for _pk, name in shown]}
    if len(rows) > MAX_TEMPLATE_NAMES:
        out["more"] = True
    raw_default = (project.default_task_template or "").strip()
    if raw_default.startswith("custom:"):
        by_pk = {f"custom:{pk}": name for pk, name in shown}
        if raw_default in by_pk:
            out["default"] = wrap_workspace_content(by_pk[raw_default])
    elif raw_default:
        out["default"] = raw_default
    return out


def _defaults_sentence(project: ProjectMaster, params: list[str]) -> str:
    known = defaults(project)
    parts = [f"{p} {known[p]}" for p in params if p in known]
    return f" The project's own defaults: {'; '.join(parts)}." if parts else ""


def _fields(params: list[str]) -> str:
    return ", ".join(params)


def create_refusal(project: ProjectMaster, values: dict[str, Any]) -> str | None:
    """The refusal for a new task, or None."""
    missing = missing_fields(project, values)
    if not missing:
        return None
    return (
        f"Project {project.project_id} requires these fields on a new task: {_fields(missing)}."
        f"{_defaults_sentence(project, missing)} Ask the member for them, or set them if they "
        "already told you, then try again. Nothing was created."
    )


def row_refusal(project: ProjectMaster, values: dict[str, Any], where: str) -> str | None:
    """One line of a plan's validation errors for a task (or the new milestone) that lacks a field."""
    missing = missing_fields(project, values)
    if not missing:
        return None
    return (
        f"{where}: project {project.project_id} requires {_fields(missing)}."
        f"{_defaults_sentence(project, missing)}"
    )


PLAN_ADVICE = (
    "Ask the member for the missing fields, or set them if they already told you, then send the plan "
    "again. Nothing was created."
)


def plan_row_values(spec: dict[str, Any], default_assignee: str | None, inherited: str | None):
    """A plan task as `create_task`'s values: its own assignee, else the plan's default, else the
    new milestone's sole assignee (`create_task_plan._task_assignee`)."""
    own = spec.get("assignee_id")
    assignee = str(own) if own and own != "null" else (default_assignee or inherited)
    return {
        "assignee_id": assignee,
        "tags": spec.get("tags"),
        "priority": spec.get("priority"),
        "effort_level": spec.get("effort_level"),
        "due_date": spec.get("due_date"),
    }


def plan_refusals(project: ProjectMaster, args: dict[str, Any]) -> list[str]:
    """Every line the plan's rows (and a new milestone) earn, from the plan's own arguments."""
    if not required_params(project, tags_exist=None):
        return []
    tasks = args.get("tasks")
    default = args.get("default_assignee_id")
    default = str(default) if default and default != "null" else None
    lines: list[str] = []
    milestone = args.get("milestone")
    inherited = None
    if isinstance(milestone, dict):
        ids = []
        for uid in milestone.get("assignee_ids") or []:
            if uid and str(uid) not in ids:
                ids.append(str(uid))
        inherited = ids[0] if len(ids) == 1 else None
        values = {
            "assignee_id": ids[0] if ids else None,
            "tags": milestone.get("tags"),
            "priority": milestone.get("priority"),
            "effort_level": milestone.get("effort_level"),
            "due_date": milestone.get("due_date"),
        }
        if line := row_refusal(project, values, "milestone"):
            lines.append(line)
    for i, spec in enumerate(tasks if isinstance(tasks, list) else []):
        if isinstance(spec, dict):
            line = row_refusal(project, plan_row_values(spec, default, inherited), f"tasks[{i}]")
            if line:
                lines.append(line)
    return lines


def update_refusal(project: ProjectMaster, args: dict[str, Any]) -> str | None:
    """The refusal for an edit that clears a required field, or None. An edit that leaves a field
    alone, or sets it, is never refused."""
    cleared = []
    for key, param in FIELDS:
        if param not in CLEARABLE:
            continue
        if param not in args or args[param] is None:
            continue
        value = args[param]
        empty = (value == "") if param == "due_date" else (not _has_tags(value))
        if empty:
            cleared.append((key, param))
    if not cleared:
        return None
    required = {param for _key, param in required_params(project)}
    hit = [param for _key, param in cleared if param in required]
    if not hit:
        return None
    return (
        f"Project {project.project_id} requires {_fields(hit)} on its tasks, so "
        f"{'it' if len(hit) == 1 else 'they'} cannot be cleared. Set a new value instead, or leave "
        "it as it is. Nothing was changed."
    )


def proposal_refusal(name: str, args: dict[str, Any], ctx: ToolContext) -> str | None:
    """The refusal a WRITE proposal earns before it reaches an approval card (the loop calls this
    for `create_task`, `create_task_plan` and `update_task` while the flag is on), or None.

    Anything that is not clearly the caller's own business returns None, so the tool's own checks
    answer as they always did: a project of another team, a missing project, a project the caller
    cannot write to, a task they cannot read. This never says why a project or task is unreachable."""
    if name not in ("create_task", "create_task_plan", "update_task"):
        return None
    from origin.models.task.task_models import TaskMaster  # noqa: PLC0415

    try:
        if name == "update_task":
            if not any(p in args for p in CLEARABLE):
                return None
            task = TaskMaster.objects.filter(
                task_id=int(args.get("task_id")), is_deleted=False
            ).first()
            if task is None or str(task.team_id or "") != ctx.team_id:
                return None
            project_id = task.project_id
            allowed = task_acl_user_ids(project_id, task.assignee_id, task.reporter_id)
        else:
            project_id = int(args.get("project_id"))
            allowed = task_acl_user_ids(project_id, None, None)
    except (TypeError, ValueError):
        return None
    if ctx.user_id not in allowed:
        return None
    project = ProjectMaster.objects.filter(
        project_id=project_id, is_deleted=False, team_id=ctx.team_id
    ).first()
    if project is None:
        return None
    if name == "create_task":
        return create_refusal(project, args)
    if name == "update_task":
        return update_refusal(project, args)
    lines = plan_refusals(project, args)
    if not lines:
        return None
    return "Plan validation failed:\n- " + "\n- ".join(lines[:25]) + "\n" + PLAN_ADVICE
