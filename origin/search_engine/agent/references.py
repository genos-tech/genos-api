"""Let the in-app agent name things the way people do (MIP-881).

The task tools declared `task_id` as an integer, so a person who typed "mark
WRD-5 closed" sent the model to `search_knowledge_base` just to learn the
number. That is the slowest tool there is (5.4 s at the median in production)
plus another model call, on the commonest kind of request. In the routing eval
it was 74 of the 95 misses.

MCP solved this already, so this reuses it rather than repeating it. The
catalog says which argument of which tool is a reference, and
`mcp.tools.resolve_references` says what a reference means. This adds the two
things the in-app path needs: declare those arguments as strings, and resolve
them when the call arrives.

Only the declaration changes. Registry schemas stay as they are, so the tools
keep receiving integers and every test of them still holds. A numeric id passes
straight through without a query.
"""

from __future__ import annotations

import copy
from functools import lru_cache
from typing import Any

from origin.search_engine.agent.chip_syntax import CHIP_POINTER, CHIP_SYNTAX_HINT

_MILESTONE_HINT = (
    " Also accepts the milestone's display id (the id of its backing task, e.g. "
    "PRJ-123) or its Genos URL; a bare number is always the milestone id."
)
_TASK_REF = "The task: its display id (PRJ-123), its Genos URL, or its numeric id."


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    """Published tools by registry name. Lazy: the catalog imports the registry."""
    from origin.views.public.mcp.tools import CATALOG  # noqa: PLC0415

    return {tool.registry_name: tool for tool in CATALOG}


def _reference(prop: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """`prop` as a string reference, worded as the catalog words it for this
    argument when it has wording of its own. The dialect is the registry's."""
    description = (override or {}).get("description") or _TASK_REF
    return {**prop, "type": "STRING", "description": description}


def _with_hint(prop: dict[str, Any]) -> dict[str, Any]:
    wording = str(prop.get("description") or "").rstrip()
    return {**prop, "type": "STRING", "description": wording + _MILESTONE_HINT}


def _point_at_chips(prop: dict[str, Any]) -> None:
    """The system prompt carries the chip syntax once, so a body argument only
    has to point at it. MCP has no such prompt and keeps the full text."""
    if CHIP_SYNTAX_HINT in str(prop.get("description")):
        prop["description"] = prop["description"].replace(CHIP_SYNTAX_HINT, CHIP_POINTER)
    for child in ((prop.get("items") or {}).get("properties") or {}).values():
        _point_at_chips(child)
    for child in (prop.get("properties") or {}).values():
        _point_at_chips(child)


def declared_schema(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """The schema the model sees for tool `name`. `schema` is never mutated: it
    is module state, and MCP builds its own declaration from it."""
    tool = _catalog().get(name)
    if tool is None:
        return schema
    declared = copy.deepcopy(schema)
    props = declared.get("properties") or {}
    for prop in props.values():
        _point_at_chips(prop)
    for arg in (*tool.task_ref_args, *tool.note_ref_args):
        if arg in props:
            props[arg] = _reference(props[arg], tool.schema_overrides.get(arg))
    for arg in tool.milestone_ref_args:
        if arg in props:
            props[arg] = _with_hint(props[arg])
    for list_arg, item_key in tool.nested_task_ref_args:
        items = ((props.get(list_arg) or {}).get("items") or {}).get("properties") or {}
        if item_key in items:
            override = tool.nested_schema_overrides.get((list_arg, item_key))
            items[item_key] = _reference(items[item_key], override)
    for list_arg, item_key in tool.nested_milestone_ref_args:
        items = ((props.get(list_arg) or {}).get("items") or {}).get("properties") or {}
        if item_key in items:
            items[item_key] = _with_hint(items[item_key])
    return declared


def resolve_call(name: str, args: dict[str, Any], team_id: str) -> dict[str, Any]:
    """`args` with references turned into ids. Raises `ToolError` when one names
    nothing, worded for the model to correct on its next step."""
    from origin.views.public.mcp.tools import resolve_references  # noqa: PLC0415

    tool = _catalog().get(name)
    return dict(args) if tool is None else resolve_references(tool, args, team_id)
