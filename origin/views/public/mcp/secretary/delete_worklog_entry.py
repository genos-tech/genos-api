"""`delete_worklog_entry` (MIP-1071): MCP-only, see `secretary/__init__.py`.

Deletes one entry and its items for good. The logic is in `origin/services/worklog.py`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import worklog


def _delete(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return worklog.delete_entry(ctx, entry_id=args.get("entry_id"))


DELETE_WORKLOG_ENTRY = Tool(
    name="delete_worklog_entry",
    description="Delete one worklog entry and all its items.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "entry_id": {
                "type": "INTEGER",
                "description": "From `capture_worklog` or `list_my_worklog`.",
            },
        },
        "required": ["entry_id"],
    },
    run=_delete,
    # An MCP-only tool is never asked for approval (scope is the gate there); this marks
    # it as a write, which the catalog contract test checks against the catalog's flag.
    requires_approval=True,
    mcp_only=True,
)
