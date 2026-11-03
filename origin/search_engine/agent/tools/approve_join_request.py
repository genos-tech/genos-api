"""`approve_join_request` write tool (MIP-882): let someone into a project.

Adds the person who asked to the project and tells them, as the Approve button does.
That changes who can see and edit the project, and MCP has no approval card, so on MCP it
needs the key's opt-in like a delete does (Ken's decision 2). Declining is its own tool,
`decline_join_request`, because it lets nobody in and needs no opt-in.

Project requests only. A request to join a team can add a paid seat, and one to join a
group chat is a different space, so both stay in the app. What counts as a request the
caller may answer is `origin.services.project_requests`'.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools.base import Tool, ToolContext
from origin.search_engine.agent.tools.request_access import answer_request
from origin.services.project_requests import approve_join_request


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    answered = answer_request(args, ctx, approve_join_request, label="approve_join_request")
    name, project = answered.requester_name, answered.project.project_name
    return {
        "inbox_item_id": answered.item_id,
        "project_id": answered.project.project_id,
        "project_name": project,
        "user_id": answered.requester_id,
        "username": name,
        "request_status": answered.status,
        "already_member": answered.already_member,
        "__summary__": (
            f'{name} is already a member of "{project}"; the request is closed'
            if answered.already_member
            else f'Let {name} into "{project}" and told them'
        ),
    }


APPROVE_JOIN_REQUEST = Tool(
    name="approve_join_request",
    description=(
        "Approve someone's request to join a project: they are added and told. REQUIRES USER "
        "APPROVAL — it changes who can see and edit the project. Call `list_my_inbox` FIRST "
        "(category 'requests'): a join_project_request that is still pending has an `item_id`, "
        "which is `inbox_item_id` here. Project requests only; team and group-chat requests "
        "stay in the app. Use `decline_join_request` to say no."
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
