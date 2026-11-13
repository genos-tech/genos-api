"""`get_timeline` (MIP-1281): MCP-only, see `secretary/__init__.py`.

One ordered history of whatever the member points at: a task, a chat thread, a chat window, a note.
The logic, the contract and the access rules are in `origin/services/timeline.py`; this file only
declares the arguments and hands them over.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import timeline


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    result = timeline.timeline(
        ctx,
        args.get("anchor_type"),
        args.get("anchor_id"),
        since=args.get("since"),
        until=args.get("until"),
        limit=args.get("limit"),
        cursor=args.get("cursor"),
        kinds=args.get("kinds"),
        lead_in=args.get("lead_in"),
        text_chars=args.get("text_chars"),
    )
    return result


def canonical_anchor(
    payload: dict[str, Any], args: dict[str, Any], mcp_args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    """`McpTool.augment` for this tool. It runs after the call succeeded, with the very dict the
    "which item did the member's AI app open" recorder is about to read, and puts the CANONICAL
    id in it (the task id, the thread's root message id), whatever the member's words were
    (`PRJ-123`, a reply id). The anchor was opened, so the id is one the member may read. The
    result is returned unchanged."""
    args["anchor_id"] = payload["anchor"]["anchor_id"]
    return payload


GET_TIMELINE = Tool(
    name="get_timeline",
    description="Read the ordered history of a task, a chat thread or a chat window.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "anchor_type": {
                "type": "STRING",
                "enum": list(timeline.ANCHOR_TYPES),
                "description": "What to read the history of.",
            },
            "anchor_id": {
                "type": "STRING",
                "description": (
                    "task: its display id (PRJ-123), its Genos URL or its numeric id. thread: the "
                    "id of the thread's first message (a reply's id works too). chat: the chat's id. "
                    "note: its family and id, such as task:111 or personal:42 (ids are only unique "
                    "inside a family)."
                ),
            },
            "since": {
                "type": "STRING",
                "description": (
                    "An ISO date-time (the member's time zone if it has no offset). A task or a "
                    "thread is told from its whole life by default; a chat window defaults to "
                    "the seven days before `until`, and one call spans at most 31 days."
                ),
            },
            "until": {
                "type": "STRING",
                "description": "An ISO date-time to stop at (excluded). Default: now.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "How many of the newest events to tell. Default 50, at most 100.",
            },
            "cursor": {
                "type": "STRING",
                "description": "`next_cursor` of the previous call, to read the events before it.",
            },
            "kinds": {
                "type": "ARRAY",
                "items": {"type": "STRING", "enum": list(timeline.KINDS)},
                "description": (
                    "Only these kinds of event. Default: all but `marker` (the member's own "
                    "AI-app opens of a task, told only when asked for)."
                ),
            },
            "lead_in": {
                "type": "INTEGER",
                "description": (
                    "For a thread: also tell this many messages of the same chat just before "
                    "its first one (0 to 20, default 0), marked `via: lead_in`."
                ),
            },
            "text_chars": {
                "type": "INTEGER",
                "description": "How long a snippet of text may be. Default 140, at most 500.",
            },
        },
        "required": ["anchor_type", "anchor_id"],
    },
    run=_run,
    mcp_only=True,
)
