"""Flag-gated PARAMETERS on a tool (MIP-1561, the MIP-578 parity batch).

`tool_flag_text` adds a sentence to a description while a default-off `SEARCH_ENGINE` flag is
on. This is its twin for the arguments: a tool gains parameters the model may send, but only
while the flag is on, and with the flag off nothing the model or an MCP client is shown moves
by one byte.

**The registry is never edited.** `Tool.parameters_schema` is module state, shared by the
in-app loop, MCP, the approval-edit allow-list and the tests, so the new properties live in
`ADDITIONS` below (each tool module owns its own text) and are laid over a COPY of the schema at
the two places a schema is published: the in-app declaration builder
(`controller._build_tool_declarations`) and the MCP catalog (`mcp.tools.input_schema`). A third
reader that forgot would see the base schema, which is the safe direction.

**The tool applies the same gate** (`parity.ignore_unless_parity`): while the flag is off a
parameter that arrives anyway is ignored, as an unknown argument always was.

One entry per (tool, flag). The parity tasks share the one flag `AGENT_PARITY_TOOLS`: they append
rows here, they do not add a mechanism. A nested row is a property of an ARRAY of objects
(`create_task_plan`'s `tasks`): `(array property, {row property: schema})`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from origin.search_engine.agent.parity import PARITY_FLAG, flag_on
from origin.search_engine.agent.tools.create_calendar_event import (
    PARITY_PROPERTIES as _CALENDAR_ACCOUNT,
)
from origin.search_engine.agent.tools.create_task import PARITY_PROPERTIES as _CREATE_TASK
from origin.search_engine.agent.tools.create_task_plan import PARITY_ROW_PROPERTIES as _PLAN_ROWS
from origin.search_engine.agent.tools.list_calendars import PARITY_PROPERTIES as _CALENDARS_ACCOUNT
from origin.search_engine.agent.tools.milestone_convert import (
    PARITY_DESCRIPTIONS as _MILESTONE_DESCRIPTIONS,
)
from origin.search_engine.agent.tools.milestone_convert import (
    PARITY_ENUM_VALUES as _MILESTONE_ENUM,
)
from origin.search_engine.agent.tools.milestone_convert import (
    PARITY_PROPERTIES as _MILESTONE_PROPERTIES,
)
from origin.search_engine.agent.tools.todo_source import PARITY_PROPERTIES as _CREATE_TODO
from origin.search_engine.agent.tools.update_comment import PARITY_OPTIONAL as _COMMENT_OPTIONAL
from origin.search_engine.agent.tools.update_comment import PARITY_PROPERTIES as _UPDATE_COMMENT
from origin.search_engine.agent.tools.update_task import PARITY_PROPERTIES as _UPDATE_TASK


@dataclass(frozen=True)
class ParamAddition:
    tool: str
    flag: str
    #: Properties added at the top level of the tool's schema.
    properties: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: `(array property, row properties)`: properties added to each row of an array.
    nested: tuple[tuple[str, dict[str, dict[str, Any]]], ...] = ()
    #: Top-level parameters that stop being `required` while the flag is on: the new parameters
    #: are an alternative to them (a reaction has no `body_text`).
    optional: tuple[str, ...] = ()
    #: `property -> values appended to its `enum``: an action the tool gains (`manage_milestone`'s
    #: `convert`, MIP-1564). The tool's own enum is never edited.
    enum_values: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: `property -> description`: an EXISTING property worded for the flag (`manage_milestone`'s
    #: `project_id` also names a destination). Only the description changes; the property stays.
    descriptions: dict[str, str] = field(default_factory=dict)


#: Append, never reorder: a tool's added properties follow the order of the rows.
ADDITIONS: tuple[ParamAddition, ...] = (
    ParamAddition("create_task", PARITY_FLAG, properties=_CREATE_TASK),
    ParamAddition("update_task", PARITY_FLAG, properties=_UPDATE_TASK),
    ParamAddition("create_task_plan", PARITY_FLAG, nested=(("tasks", _PLAN_ROWS),)),
    # MIP-1562: the comment bubble's reaction and flag, and "Add to To-Do" from a task or comment.
    ParamAddition(
        "update_comment", PARITY_FLAG, properties=_UPDATE_COMMENT, optional=_COMMENT_OPTIONAL
    ),
    ParamAddition("create_todo_item", PARITY_FLAG, properties=_CREATE_TODO),
    # MIP-1564: convert a root task to a milestone and back, and move a milestone to another project.
    ParamAddition(
        "manage_milestone",
        PARITY_FLAG,
        properties=_MILESTONE_PROPERTIES,
        enum_values=_MILESTONE_ENUM,
        descriptions=_MILESTONE_DESCRIPTIONS,
    ),
    # MIP-1059: which of the member's Google accounts a calendar tool uses.
    ParamAddition("list_calendars", PARITY_FLAG, properties=_CALENDARS_ACCOUNT),
    *(
        ParamAddition(name, PARITY_FLAG, properties=_CALENDAR_ACCOUNT)
        for name in (
            "list_calendar_events",
            "create_calendar_event",
            "update_calendar_event",
            "delete_calendar_event",
        )
    ),
)


def with_flagged_params(tool: str, schema: dict[str, Any]) -> dict[str, Any]:
    """`schema` with the parameters whose flag is on. The SAME object comes back when there are
    none (a flag-off declaration is the registry's own dict, untouched); otherwise a copy."""
    active = [a for a in ADDITIONS if a.tool == tool and flag_on(a.flag)]
    if not active:
        return schema
    out = copy.deepcopy(schema)
    props = out.setdefault("properties", {})
    for addition in active:
        for name, prop in addition.properties.items():
            props[name] = copy.deepcopy(prop)
        for array_name, row_props in addition.nested:
            row_schema = (props.get(array_name) or {}).get("items")
            if isinstance(row_schema, dict):
                row_schema.setdefault("properties", {}).update(copy.deepcopy(row_props))
        for name, values in addition.enum_values.items():
            prop = props.get(name)
            if isinstance(prop, dict) and isinstance(prop.get("enum"), list):
                prop["enum"] = [*prop["enum"], *(v for v in values if v not in prop["enum"])]
        for name, text in addition.descriptions.items():
            prop = props.get(name)
            if isinstance(prop, dict):
                prop["description"] = text
        if addition.optional and "required" in out:
            out["required"] = [r for r in out["required"] if r not in addition.optional]
    return out
