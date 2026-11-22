"""`get_task` on MCP also carries the comments left on the task's BODY (MIP-1863).

An augment, so the in-app `fetch_task` is untouched: it is the same tool with an MCP-only addition, as
`get_note`'s `comment_threads` is. The caller has already passed `fetch_task`'s access check, so a body's
threads are shown to exactly the people who can read the task.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import ToolContext
from origin.services.note_comments import task_body_comment_fields


def with_body_threads(
    payload: dict[str, Any], args: dict[str, Any], mcp_args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    task_id = payload.get("task_id")
    if not isinstance(task_id, int) or isinstance(task_id, bool):
        return payload
    wanted = mcp_args.get("include_resolved")
    include_resolved = wanted is True or str(wanted).lower() == "true"
    return {
        **payload,
        **task_body_comment_fields(
            task_id, ctx, include_resolved=include_resolved, include_via=True
        ),
    }
