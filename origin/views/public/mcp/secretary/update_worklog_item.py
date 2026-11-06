"""`update_worklog_item` (MIP-1071): MCP-only, see `secretary/__init__.py`.

Rewords an item, confirms or rejects it, or marks it resolved. The logic is in
`origin/services/worklog.py`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import worklog


def _update(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return worklog.update_item(
        ctx,
        item_id=args.get("item_id"),
        summary=args.get("summary"),
        kind=args.get("kind"),
        review_state=args.get("review_state"),
        status=args.get("status"),
    )


UPDATE_WORKLOG_ITEM = Tool(
    name="update_worklog_item",
    description="Change one worklog item: its wording or kind, confirm or reject it, or mark it resolved.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "item_id": {
                "type": "INTEGER",
                "description": "From `capture_worklog` or `list_my_worklog`.",
            },
            "summary": {
                "type": "STRING",
                "description": f"New wording, at most {worklog.MAX_SUMMARY_CHARS} characters.",
            },
            "kind": {"type": "STRING", "enum": list(worklog.KINDS)},
            "review_state": {
                "type": "STRING",
                "enum": list(worklog.REVIEW_STATES),
                "description": "accepted once the member confirms it; rejected if they say it is wrong.",
            },
            "status": {
                "type": "STRING",
                "enum": list(worklog.STATUSES),
                "description": "resolved when a next or waiting item is done; open to reopen it.",
            },
        },
        "required": ["item_id"],
    },
    run=_update,
    # An MCP-only tool is never asked for approval (scope is the gate there); this marks
    # it as a write, which the catalog contract test checks against the catalog's flag.
    requires_approval=True,
    mcp_only=True,
)
