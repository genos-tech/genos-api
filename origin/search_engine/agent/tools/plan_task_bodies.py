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

MIP-1561 (flag `AGENT_PARITY_TOOLS`, default OFF; with it off everything above is unchanged, byte
for byte): the same fill reaches `create_task` (`new_task_body`), a task can NAME its template
(`template`: a built-in or one of the project's own, by name), and a project whose default is a
built-in id gets that built-in. The app's create form starts from exactly that choice. The built-in
templates live in the browser (`taskTemplates.ts`); the server keeps only their section HEADINGS
(`BUILT_IN_HEADINGS`, a mirror of client text, kept in step by `test_agent_task_templates`), and
puts the task's title under the first one, as it does for a custom template. Their grey prompts are
not copied: a prompt that lost its styling in markdown would be read back as content (MIP-825).
"""

from __future__ import annotations

import copy
from typing import Any

from origin.models.project.prj_models import ProjectMaster, ProjectTaskTemplate
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.parity import parity_on
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.body_limits import check_body_size, check_body_sizes

# The picker value a custom template carries in `default_task_template`.
_CUSTOM_PREFIX = "custom:"
# Language-neutral on purpose: a label, not prose. The title is the content.
_MINIMAL_HEADING = "### Summary"


# The app's built-in task templates (`taskTemplates.ts` `TASK_TEMPLATES`): id -> (label, section
# headings in order). Headings only; see the module docstring.
BUILT_IN_TEMPLATES: dict[str, tuple[str, tuple[str, ...]]] = {
    "default": (
        "Standard task",
        ("🧾 Summary", "🪜 Motivation", "✅ Acceptance criteria", "🎯 Notes & links"),
    ),
    "bug": (
        "Bug report",
        (
            "🐞 Summary",
            "🔁 Steps to reproduce",
            "🎯 Expected behavior",
            "💥 Actual behavior",
            "🧪 Environment",
        ),
    ),
    "spike": (
        "Research / spike",
        (
            "❓ Question",
            "💡 Hypothesis",
            "🧭 Approach",
            "⏱ Timebox",
            "📌 Findings",
            "🚧 Out of scope",
        ),
    ),
    "milestone": (
        "Milestone",
        (
            "🎯 Goal",
            "✅ Success criteria",
            "📦 In scope",
            "🚫 Out of scope",
            "⚠️ Risks & dependencies",
            "🧭 Order of work",
        ),
    ),
}
BUILT_IN_HEADINGS = {tid: headings for tid, (_label, headings) in BUILT_IN_TEMPLATES.items()}
_MAX_NAMES_LISTED = 20


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


def built_in_body(template_id: str, title: str) -> str:
    """A built-in template as markdown: its section headings, with `title` under the first."""
    headings = BUILT_IN_HEADINGS[template_id]
    parts = [f"### {headings[0]}", title, *(f"### {h}" for h in headings[1:])]
    return "\n\n".join(p for p in parts if p)


def _built_in_id(name: str) -> str | None:
    wanted = name.strip().casefold()
    for tid, (label, _headings) in BUILT_IN_TEMPLATES.items():
        if wanted in (tid, label.casefold()):
            return tid
    return None


def _custom_templates(project: ProjectMaster, name: str | None = None) -> list[ProjectTaskTemplate]:
    qs = ProjectTaskTemplate.objects.filter(project_id=project.project_id)
    if name is not None:
        qs = qs.filter(template_name__iexact=name.strip())
    return list(qs.order_by("template_name", "pk")[: _MAX_NAMES_LISTED + 1])


def named_template_body(project: ProjectMaster, name: Any, title: str) -> str:
    """The body for a task that asked for template `name`: one of THIS project's own templates
    (checked first: it is the team's text), else a built-in by id or label. A ToolError, worded for
    the model, when there is none, or when the name is ambiguous or the template is empty."""
    if not isinstance(name, str) or not name.strip():
        raise ToolError(f"`template` must be a template name (got {name!r}).")
    found = _custom_templates(project, name)
    if len(found) > 1:
        raise ToolError(
            f"This project has {len(found)} templates called {name.strip()!r}; "
            "rename one of them in the app, or use another template."
        )
    if found:
        body = found[0].body
        blocks = [b for b in body if isinstance(b, dict)] if isinstance(body, list) else []
        if not blocks:
            raise ToolError(f"Template {name.strip()!r} has no content. Use another template.")
        return templated_body(blocks, title)
    built_in = _built_in_id(name)
    if built_in is not None:
        return built_in_body(built_in, title)
    names = [t.template_name for t in _custom_templates(project)]
    mine = (
        ", ".join(repr(n) for n in names[:_MAX_NAMES_LISTED])
        + (", ..." if len(names) > _MAX_NAMES_LISTED else "")
        if names
        else "none"
    )
    raise ToolError(
        f"Template {name.strip()!r} not found. Built-in: "
        f"{', '.join(BUILT_IN_TEMPLATES)}. This project's own: {mine}."
    )


def _built_in_default(project: ProjectMaster) -> str | None:
    """The built-in template id the project made its default, if it did."""
    raw = (project.default_task_template or "").strip()
    return raw if raw in BUILT_IN_TEMPLATES else None


def new_task_body(project: ProjectMaster, title: str, template: Any = None) -> str:
    """The body for a task created without one (parity flag on): the template it names, else the
    project's default (a custom template, or a built-in the project chose), else the one-line
    `### Summary` + title. Never over the size cap a body is held to: a template that would
    be falls back to the one-line body, as a plan's does."""
    title = " ".join(title.split())
    if template not in (None, ""):
        body = named_template_body(project, template, title)
    elif blocks := _template_blocks(project):
        body = templated_body(blocks, title)
    elif built_in := _built_in_default(project):
        body = built_in_body(built_in, title)
    else:
        body = minimal_body(title)
    try:
        check_body_size(body, "content_text")
    except ToolError:
        body = minimal_body(title)
    return body


def _tasks_without_body(args: dict[str, Any]) -> list[int]:
    tasks = args.get("tasks")
    if not isinstance(tasks, list):
        return []
    return [
        i
        for i, spec in enumerate(tasks)
        if isinstance(spec, dict) and _is_blank(spec.get("content_markdown")) and _clean_title(spec)
    ]


def fill_plan_task_bodies(
    args: dict[str, Any], project: ProjectMaster, *, strict: bool = True
) -> dict[str, Any]:
    """`args` with a body on every plan task that has none. `args` itself is never changed: the
    same object comes back when there is nothing to fill.

    With the parity flag on a task may name its `template`, and a project default that is a
    built-in applies; a template that is not found is a ToolError, or, with `strict=False` (the
    approval card, built before the tool's checks), a task left unfilled."""
    missing = _tasks_without_body(args)
    if not missing:
        return args
    parity = parity_on()
    blocks = _template_blocks(project)
    built_in = _built_in_default(project) if parity else None

    def one(i: int, use_template: bool) -> str:
        spec = args["tasks"][i]
        title = _clean_title(spec) or ""
        if not use_template:
            return minimal_body(title)
        if parity and spec.get("template") not in (None, ""):
            return named_template_body(project, spec["template"], title)
        if blocks:
            return templated_body(blocks, title)
        if built_in:
            return built_in_body(built_in, title)
        return minimal_body(title)

    def build(use_template: bool) -> dict[int, str]:
        out: dict[int, str] = {}
        for i in missing:
            try:
                out[i] = one(i, use_template)
            except ToolError:
                if strict:
                    raise
        return out

    bodies = build(True)
    if blocks or built_in or parity:
        # A template must never make a plan fail the size cap the model's own bodies are held to.
        try:
            check_body_sizes((f"tasks[{i}].content_markdown", b) for i, b in bodies.items())
        except ToolError:
            bodies = build(False)
    if not bodies:
        return args
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
    return fill_plan_task_bodies(args, project, strict=False)


def new_task_body_for_caller(args: dict[str, Any], team_id: str, user_id: str) -> str | None:
    """`new_task_body` for `create_task`'s approval card, which is built before the tool runs and
    so before its checks: None when there is nothing to show (the task came with a body, or the
    project is one the caller cannot create tasks in: other team, not a member, gone, so a card
    never shows a template body the caller may not read, or the template is not found, which the
    tool then refuses)."""
    content = args.get("content_text")
    if isinstance(content, str) and content.strip():
        return None
    title = args.get("title")
    if not isinstance(title, str) or not " ".join(title.split()):
        return None
    try:
        project = ProjectMaster.objects.get(
            project_id=int(args.get("project_id")), is_deleted=False
        )
    except (TypeError, ValueError, ProjectMaster.DoesNotExist):
        return None
    if str(getattr(project, "team_id", "") or "") != str(team_id or ""):
        return None
    if str(user_id or "") not in task_acl_user_ids(
        project_id=project.project_id, assignee_id=None, reporter_id=None
    ):
        return None
    try:
        return new_task_body(project, title, args.get("template"))
    except ToolError:
        return None
