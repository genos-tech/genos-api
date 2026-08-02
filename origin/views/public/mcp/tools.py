"""The published tool catalog, and the adapter down to the registry.

Genos has 57 agent tools. This exposes 14. That is not a staging
decision — every tool costs the connected agent context on *every*
request it makes, and most of the 57 exist to answer questions Genos's
own assistant gets asked ("who closed the most tasks last sprint"),
not to help something implement a task. The 14 here are the ones the
dev loop actually walks: find the task, read it, do the work, report
back.

Three things have to change on the way through, and each is a real
mismatch rather than tidying:

1. **Schema dialect.** `parameters_schema` is written for Gemini's
   function-calling, which spells types in uppercase (`"STRING"`).
   MCP wants JSON Schema. Lowercased recursively here.
2. **Descriptions.** The registry's are addressed to Genos's own
   assistant — they promise an approval card that only exists in the
   Genos UI, and they cross-reference tools by names this surface
   renames. Left alone they would instruct the caller to invoke tools
   that 404. Rewritten below, which also stops MCP's contract from
   moving every time the in-app prompt is tuned for evals.
3. **Task references.** Tools take a numeric `task_id`; people type
   `PRJ-123` or paste a URL. Resolved in `resolve.py` before the tool
   runs.

Scope is enforced here rather than by `HasRequiredScope`, because MCP is
all-POST: the method-based rule would refuse a read-only key on a pure
read. Write tools are hidden from `tools/list` for a read key (the spec
blesses varying the set by the caller's authorization) *and* refused at
call time, since a client may be working from a cached list.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from django.conf import settings

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.views.public.mcp.resolve import resolve_task_ref

log = logging.getLogger(__name__)

# Registry name -> published name, applied to every description string so
# a cross-reference can't outlive a rename. Renames are for legibility to
# an outside agent: `fetch_task` and `search_knowledge_base` describe
# Genos's internals; `get_task` and `search_workspace` describe the job.
_RENAMES = {
    "fetch_task": "get_task",
    "get_my_focus_tasks": "list_my_tasks",
    "search_knowledge_base": "search_workspace",
    "fetch_note": "get_note",
}
_RENAME_RE = re.compile(r"\b(" + "|".join(map(re.escape, _RENAMES)) + r")\b")

# A task reference as a person writes it. Replaces the `task_id` integer
# on every tool that takes one.
_TASK_REF_SCHEMA = {
    "type": "string",
    "description": ("The task: its display id (PRJ-123), its Genos URL, or its numeric id."),
}


def _rewrite_refs(text: str) -> str:
    return _RENAME_RE.sub(lambda m: _RENAMES[m.group(1)], text or "")


@dataclass(frozen=True)
class McpTool:
    """One published tool: what to call it, and what it maps to."""

    name: str
    registry_name: str
    description: str
    write: bool = False
    # Argument names whose value is a task reference to resolve before
    # the underlying tool sees it.
    task_ref_args: tuple[str, ...] = ()
    # Applied to the registry schema after dialect conversion.
    schema_overrides: dict[str, Any] = field(default_factory=dict)


CATALOG: tuple[McpTool, ...] = (
    # ---- find and read ----
    McpTool(
        name="get_task",
        registry_name="fetch_task",
        description=(
            "Read one task in full: title, status, priority, assignee, dates, "
            "its description, and its comments. Prefer `content_markdown` for "
            "the description when it is present — it keeps headings, checklists "
            "and code fences — and fall back to `content_text`, the same body "
            "flattened. This is the first call when the user names a task to "
            "work on."
        ),
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="list_my_tasks",
        registry_name="get_my_focus_tasks",
        description=(
            "The caller's own tasks, most worth attention first (overdue and "
            "in-progress before the rest). Use this for 'what am I working "
            "on', 'my queue', or when the user asks you to pick up work "
            "without naming a task."
        ),
    ),
    McpTool(
        name="list_tasks",
        registry_name="list_tasks",
        description=(
            "List tasks with filters — by project, milestone, status, "
            "priority, assignee, or overdue. Use this to survey a milestone "
            "before starting, or to find a task whose id you don't have."
        ),
    ),
    McpTool(
        name="list_projects",
        registry_name="list_projects",
        description=(
            "Every project the caller can see, with the numeric ids and "
            "short codes other tools take. Call this to turn a project name "
            "into an id."
        ),
    ),
    McpTool(
        name="list_milestones",
        registry_name="list_milestones",
        description=(
            "Milestones in a project, with due dates and progress. A "
            "milestone is how a multi-task piece of work is grouped — start "
            "here when the user names one rather than a single task."
        ),
    ),
    McpTool(
        name="get_milestone_summary",
        registry_name="get_milestone_summary",
        description=(
            "One milestone in depth: its tasks, their statuses, what is done "
            "and what remains. Use it to plan the order of work across a "
            "milestone."
        ),
    ),
    McpTool(
        name="get_task_blockers",
        registry_name="get_task_blockers",
        description=(
            "What a task is waiting on — its unfinished dependencies. Check "
            "before starting work, since a blocked task usually means "
            "finishing something else first."
        ),
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="search_workspace",
        registry_name="search_knowledge_base",
        description=(
            "Semantic search across the workspace — tasks, notes, chat "
            "messages and todos. Use it for context the task description "
            "assumes: prior discussion of a bug, a design note, an earlier "
            "decision. Natural-language queries work better than keywords."
        ),
    ),
    McpTool(
        name="get_note",
        registry_name="fetch_note",
        description=(
            "Read a note's full body. Notes hold specs, designs and "
            "decisions — follow one when a task description points at it."
        ),
    ),
    McpTool(
        name="get_current_user",
        registry_name="get_current_user",
        description=(
            "Who this API key acts as: user id, name, team. Call it when you "
            "need the caller's own id for an assignee filter, or to confirm "
            "which account is making changes."
        ),
    ),
    McpTool(
        name="get_team_members",
        registry_name="get_team_members",
        description=(
            "The team's members and their user ids. Use it to turn a person's "
            "name into the id that assignee filters take."
        ),
    ),
    # ---- write: needs a write-scoped key ----
    McpTool(
        name="update_task",
        registry_name="update_task",
        description=(
            "Change a task: status, title, description, priority, effort or "
            "due date. Set status to 'WIP' when you start and 'Closed' when "
            "the work is merged. `content_text` replaces the whole description, "
            "so read the task first and send the full new body rather than a "
            "fragment. Requires a write-scoped API key."
        ),
        write=True,
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="add_comment",
        registry_name="add_comment",
        description=(
            "Post a comment on a task. This is how you report back — the PR "
            "link, what you changed, anything you had to decide. The comment "
            "appears in the task's thread and notifies the assignee. "
            "Requires a write-scoped API key."
        ),
        write=True,
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="create_task",
        registry_name="create_task",
        description=(
            "Create a task in a project. Use it to split discovered work out "
            "of the task you're on, rather than widening that one. Get the "
            "project id from `list_projects`. Requires a write-scoped API key."
        ),
        write=True,
    ),
)

BY_NAME = {t.name: t for t in CATALOG}


# --------------------------------------------------------------------------
# schema conversion
# --------------------------------------------------------------------------


def _to_json_schema(node: Any) -> Any:
    """Lowercase Gemini's uppercase type names, recursively.

    The registry schemas use only `type` / `properties` / `required` /
    `description` / `enum` / `items`, all of which are standard JSON
    Schema once the type spelling is fixed — so this is the whole
    conversion, not the first pass of one.
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "type" and isinstance(value, str):
                out[key] = value.lower()
            elif key == "description" and isinstance(value, str):
                out[key] = _rewrite_refs(value)
            else:
                out[key] = _to_json_schema(value)
        return out
    if isinstance(node, list):
        return [_to_json_schema(v) for v in node]
    return node


def input_schema(tool: McpTool) -> dict[str, Any]:
    schema = _to_json_schema(REGISTRY[tool.registry_name].parameters_schema)
    if tool.schema_overrides:
        properties = dict(schema.get("properties") or {})
        properties.update(tool.schema_overrides)
        schema = {**schema, "properties": properties}
    return schema


def _disabled_tools() -> set[str]:
    """The `AGENT_DISABLED_TOOLS` ops kill switch, honoured here too.

    It names REGISTRY tools, so it is matched on `registry_name`: an
    operator switching off `add_comment` in an incident means everywhere,
    not just in the app. Fail-open on an unset value, matching the
    agent controller — a service that misses the env var runs at full
    capability rather than silently losing tools.
    """
    configured = settings.SEARCH_ENGINE.get("AGENT_DISABLED_TOOLS") or frozenset()
    return set(configured)


def visible_tools(scope: str) -> list[McpTool]:
    """The catalog this key may see, in a stable order.

    Write tools are hidden from a read-only key rather than listed and
    then refused: the spec allows the set to vary by the caller's
    authorization, and hiding stops the agent spending turns on a tool
    it can never call. Call-time refusal still exists for a client
    working from a cached list.

    Order is the catalog's, which is fixed — clients cache the list, and
    a stable order keeps prompt caches warm.
    """
    disabled = _disabled_tools()
    return [
        t
        for t in CATALOG
        if t.registry_name not in disabled and (scope == SCOPE_WRITE or not t.write)
    ]


def describe(tool: McpTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": input_schema(tool),
    }


# --------------------------------------------------------------------------
# invocation
# --------------------------------------------------------------------------


def _strip_workspace_content(value: Any) -> Any:
    """Remove the `<workspace_content>` fences from tool output.

    They are a prompt-injection guard aimed at Genos's own system prompt,
    which instructs the model to treat anything inside them as data. The
    agent on the other end of MCP has its own defences and no idea what
    these tags mean, so leaving them in is noise that costs tokens and
    reads like markup in the task body.
    """
    if isinstance(value, str):
        if value.startswith("<workspace_content>\n") and value.endswith("\n</workspace_content>"):
            return value[len("<workspace_content>\n") : -len("\n</workspace_content>")]
        return value
    if isinstance(value, dict):
        return {k: _strip_workspace_content(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_workspace_content(v) for v in value]
    return value


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call(
    name: str,
    arguments: dict[str, Any],
    ctx: ToolContext,
    scope: str,
    *,
    on_unknown_tool: Callable[[str], Exception],
) -> dict[str, Any]:
    """Run a published tool and shape its output as a `CallToolResult`.

    Returns `isError: true` for anything the caller could plausibly fix
    by trying differently — a bad task reference, a missing permission,
    the wrong key scope. Those go back to the model, which is the only
    party that can act on them. An *unknown tool* is not that: it means
    the client and server disagree about what exists, so it raises and
    becomes a JSON-RPC protocol error instead.
    """
    tool = BY_NAME.get(name)
    if tool is None or tool.registry_name in _disabled_tools():
        raise on_unknown_tool(name)

    if tool.write and scope != SCOPE_WRITE:
        return _text_result(
            f"`{name}` writes to the workspace, and this API key is read-only. "
            f"Create a key with write scope in Genos under Settings → Developer, "
            f"then reconnect. Reads will keep working with the current key.",
            is_error=True,
        )

    args = dict(arguments or {})
    try:
        for arg in tool.task_ref_args:
            if arg in args and args[arg] is not None:
                args[arg] = resolve_task_ref(args[arg], ctx.team_id)
        payload = REGISTRY[tool.registry_name].run(args, ctx)
    except ToolError as exc:
        # The tool's own vocabulary — not authorized, not found, invalid
        # argument. Exactly the feedback a model can retry against.
        return _text_result(str(exc), is_error=True)

    payload = _strip_workspace_content(dict(payload or {}))
    # `__summary__` is the one-line human summary Genos renders in its own
    # UI. Promoted to the first line here so the answer leads with what
    # happened, and dropped from the body so it isn't said twice.
    summary = payload.pop("__summary__", None)

    body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    text = f"{summary}\n\n{body}" if summary else body
    return {
        "content": [{"type": "text", "text": text}],
        # The parsed payload alongside the text, so a client that
        # understands structured results doesn't have to re-parse ours.
        "structuredContent": payload,
        "isError": False,
    }
