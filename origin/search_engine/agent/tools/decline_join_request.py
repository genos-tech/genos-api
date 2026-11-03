"""`decline_join_request` write tool (MIP-882): say no to a request to join a project.

The request closes as rejected and the person is told, as the Decline button does. It lets
nobody in, so on MCP it needs a write key and nothing more; `approve_join_request`, which
does, needs the key's opt-in.

Stricter than the page in one way: it refuses a request that was already answered. The
page's decline never looks at the status, so it can turn an approved request into a
rejected one, and tell the person they were turned down after they were let in.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools.base import Tool, ToolContext
from origin.search_engine.agent.tools.request_access import answer_request
from origin.services.project_requests import decline_join_request


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    answered = answer_request(args, ctx, decline_join_request, label="decline_join_request")
    name, project = answered.requester_name, answered.project.project_name
    return {
        "inbox_item_id": answered.item_id,
        "project_id": answered.project.project_id,
        "project_name": project,
        "user_id": answered.requester_id,
        "username": name,
        "request_status": answered.status,
        "__summary__": f'Declined {name}\'s request to join "{project}" and told them',
    }


DECLINE_JOIN_REQUEST = Tool(
    name="decline_join_request",
    description=(
        "Decline someone's request to join a project: the request closes and they are told. "
        "REQUIRES USER APPROVAL. Call `list_my_inbox` FIRST (category 'requests'): a "
        "join_project_request that is still pending has an `item_id`, which is "
        "`inbox_item_id` here. A request that was already answered can't be changed. Project "
        "requests only. Use `approve_join_request` to say yes."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "inbox_item_id": {
                "type": "INTEGER",
                "description": "The request's `item_id` from list_my_inbox.",
            },
        },
        "required": ["inbox_item_id"],
    },
    run=_run,
    requires_approval=True,
)
