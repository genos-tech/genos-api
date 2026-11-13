"""`get_timeline` for the in-app agent (MIP-1289, the in-app half of MIP-1281).

One time-ordered line for a task, a chat thread or a note: field changes, comments, messages,
note versions and merged PRs of ONE thing, merged and oldest first. Without it the model has to
call `fetch_task`, `get_task_history` (newest first), `fetch_chat_thread` (oldest first) and
`fetch_note_history` and put the four in order itself.

THIN on purpose. The service is `origin/services/timeline.py`, the same one the MCP tool calls
(`origin/views/public/mcp/secretary/get_timeline.py`): every access rule (the anchor is opened
first, one sentence for "missing" and "not yours", the audience rule of a thread, the cursor),
the paging, the size budget and the fencing of every free-text field are in it and only in it.
This file declares the arguments the app needs and hands them over. Nothing is copied.

Differences from the MCP tool, all in what is DECLARED, none in what is read:
  * the description is written for the in-app model: when to use it instead of
    `get_task_history`, `fetch_chat_thread` and `fetch_note_history`;
  * `chat` windows, `kinds`, `since` and `until` are not declared (the model has tools for a
    channel page and for a period) and a `chat` anchor is refused here; the declaration stays
    small, because it is paid for on every call of every run;
  * the fences stay: over MCP the layer takes them off, in the app the model reads them like it
    does in every other in-app tool.

Published ONLY while `SEARCH_ENGINE["AGENT_INAPP_TIMELINE_TOOL"]` is on (default off): see
`tools/base.py::FLAGGED_REGISTRY`. Not in `REGISTRY`, so it is neither declared nor runnable
while the flag is off.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError

# What the app declares. A subset of `timeline.ANCHOR_TYPES`, pinned by a test.
APP_ANCHOR_TYPES = ("task", "thread", "note")


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    # Imported here: the service imports `tools.base`, which runs this package's `__init__`,
    # which imports this module.
    from origin.services import timeline  # noqa: PLC0415

    anchor_type = args.get("anchor_type")
    if anchor_type not in APP_ANCHOR_TYPES:
        raise ToolError(f"`anchor_type` must be one of: {', '.join(APP_ANCHOR_TYPES)}.")
    return timeline.timeline(
        ctx,
        anchor_type,
        args.get("anchor_id"),
        limit=args.get("limit"),
        cursor=args.get("cursor"),
        lead_in=args.get("lead_in"),
        text_chars=args.get("text_chars"),
    )


GET_TIMELINE_INAPP = Tool(
    name="get_timeline",
    description=(
        "The ordered story of ONE task, chat thread or note, oldest to newest, in one call: "
        "who did what and who said what, merged (status and assignee changes, comments, "
        "thread messages, note edits, merged PRs). Use it for 'what happened with X', 'how "
        "did this start', 'walk me through it', 'what led to this', 'in what order'. For one "
        "fact use the others: who changed a field or when it closed -> get_task_history; the "
        "full text of a page of messages -> fetch_chat_thread; one note version -> "
        "fetch_note_history. Events are short snippets. The newest `limit` come first and "
        "`next_cursor` walks back to older ones. Not for finding a task or listing tasks."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "anchor_type": {
                "type": "STRING",
                "enum": list(APP_ANCHOR_TYPES),
                "description": "What to tell the story of.",
            },
            "anchor_id": {
                "type": "STRING",
                "description": (
                    "task: PRJ-123, its URL or its id. thread: the id of its first message "
                    "(a reply's id works). note: family:id, such as task:111 or personal:42."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": "How many of the newest events. Default 50, at most 100.",
            },
            "cursor": {
                "type": "STRING",
                "description": "`next_cursor` of the last call, with the same other arguments.",
            },
            "lead_in": {
                "type": "INTEGER",
                "description": "Thread only: also this many messages (0-20) before its first.",
            },
            "text_chars": {
                "type": "INTEGER",
                "description": "Longest snippet of text. Default 140, at most 500.",
            },
        },
        "required": ["anchor_type", "anchor_id"],
    },
    run=_run,
)
