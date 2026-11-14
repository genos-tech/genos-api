"""Server-side default body for a `create_task_plan` task that came without one (MIP-1510).

The model often proposes a plan whose tasks carry a title and nothing else (4 of 4 probe runs on
a Japanese prompt), so the approval card shows empty bodies and the tasks are created with no
description. This fills the gap deterministically, with no model call:

  * the project's DEFAULT TASK body template (`ProjectMaster.default_task_template`, api #129),
    when it names a custom template (`custom:{pk}`) of THAT project: its blocks as markdown, the
    task's title as the line under the first heading (the Summary line), or above the template
    when it has no heading. The template is the members' own text, so the language is theirs;
  * otherwise (no default, a built-in id such as `default` / `bug` that only the browser knows,
    a template that is gone, empty or too large) the one-line `### Summary` + title.

A task the model gave a body for is never touched. There is no "titles only" argument (and none
is invented here): a missing, null or blank `content_markdown` means "no body".

The fill is applied in the two places a plan's tasks are read, with the SAME text, so the card
equals what is created: the approval card (`controller._friendly_task_plan_arguments`) and
`create_task_plan._run` (which MCP also reaches). It always works on a copy: the persisted
`arguments_json`, which is replayed to the model as its own call, stays as the model sent it.
"""

from __future__ import annotations

import copy
from typing import Any

from origin.models.project.prj_models import ProjectMaster, ProjectTaskTemplate
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.body_limits import check_body_sizes

# The picker value a custom template carries in `default_task_template`.
_CUSTOM_PREFIX = "custom:"
# Language-neutral on purpose: a label, not prose. The title is the content.
_MINIMAL_HEADING = "### Summary"


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _clean_title(spec: dict[str, Any]) -> str | None:
    title = spec.get("title")
    if not isinstance(title, str):
        return None
    title = " ".join(title.split())
    return title or None


def minimal_body(title: str) -> str:
    return f"{_MINIMAL_HEADING}\n\n{title}"


def _template_blocks(project: ProjectMaster) -> list[dict[str, Any]] | None:
    """Top-level blocks of the project's default task template, or None.

    The lookup is bound to `project`: the default is a free string a member can set to any
    `custom:{pk}`, so a pk of another project (or team) must read as "no template".
    """
    raw = project.default_task_template or ""
    if not raw.startswith(_CUSTOM_PREFIX):
        return None
    try:
        pk = int(raw[len(_CUSTOM_PREFIX) :])
    except ValueError:
        return None
    body = (
        ProjectTaskTemplate.objects.filter(pk=pk, project_id=project.project_id)
        .values_list("body", flat=True)
        .first()
    )
    if not isinstance(body, list):
        return None
    blocks = [b for b in body if isinstance(b, dict)]
    return blocks or None


def _is_placeholder(block: dict[str, Any]) -> bool:
    """An empty paragraph, or one made only of the grey italic hint text the built-in templates
    use: the title takes its place."""
    if block.get("type") != "paragraph" or block.get("children"):
        return False
    content = block.get("content")
    if not content:
        return True
    if not isinstance(content, list):
        return False
    for run in content:
        if not isinstance(run, dict) or run.get("type") != "text":
            return False
        styles = run.get("styles") if isinstance(run.get("styles"), dict) else {}
        if (run.get("text") or "").strip() and not (
            styles.get("italic") and styles.get("textColor") == "gray"
        ):
            return False
    return True


def templated_body(blocks: list[dict[str, Any]], title: str) -> str:
    """The template as markdown with `title` as the Summary line (see the module docstring)."""
    title_block = {
        "type": "paragraph",
        "content": [{"type": "text", "text": title, "styles": {}}],
        "children": [],
    }
    first_heading = next((i for i, b in enumerate(blocks) if b.get("type") == "heading"), None)
    if first_heading is None:
        return minimal_body(title) + "\n\n" + blocks_to_markdown(blocks)
    rest = blocks[first_heading + 1 :]
    if rest and _is_placeholder(rest[0]):
        rest = rest[1:]
    filled = [*blocks[: first_heading + 1], title_block, *rest]
    return blocks_to_markdown(filled)


def _tasks_without_body(args: dict[str, Any]) -> list[int]:
    tasks = args.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [
        i
        for i, spec in enumerate(tasks)
        if isinstance(spec, dict) and _is_blank(spec.get("content_markdown")) and _clean_title(spec)
    ]


def fill_plan_task_bodies(args: dict[str, Any], project: ProjectMaster) -> dict[str, Any]:
    """`args` with a body on every plan task that has none. `args` itself is never changed: the
    same object comes back when there is nothing to fill."""
    missing = _tasks_without_body(args)
    if not missing:
        return args
    blocks = _template_blocks(project)

    def build(use_template: bool) -> dict[int, str]:
        out: dict[int, str] = {}
        for i in missing:
            title = _clean_title(args["tasks"][i]) or ""
            out[i] = (
                templated_body(blocks, title) if use_template and blocks else minimal_body(title)
            )
        return out

    bodies = build(True)
    if blocks:
        # A template must never make a plan fail the size cap the model's own bodies are held to.
        try:
            check_body_sizes((f"tasks[{i}].content_markdown", b) for i, b in bodies.items())
        except ToolError:
            bodies = build(False)
    filled = copy.deepcopy(args)
    for i, body in bodies.items():
        filled["tasks"][i]["content_markdown"] = body
    return filled


def fill_plan_task_bodies_for_caller(
    args: dict[str, Any], team_id: str, user_id: str
) -> dict[str, Any]:
    """`fill_plan_task_bodies` for the approval card, which is built before the tool runs and so
    before `_run`'s checks: a project the caller cannot create tasks in (other team, not a member,
    gone) gets no fill, so a card never shows a template body the caller may not read."""
    if not _tasks_without_body(args):
        return args
    try:
        project = ProjectMaster.objects.get(
            project_id=int(args.get("project_id")), is_deleted=False
        )
    except (TypeError, ValueError, ProjectMaster.DoesNotExist):
        return args
    if str(getattr(project, "team_id", "") or "") != str(team_id or ""):
        return args
    if str(user_id or "") not in task_acl_user_ids(
        project_id=project.project_id, assignee_id=None, reporter_id=None
    ):
        return args
    return fill_plan_task_bodies(args, project)
