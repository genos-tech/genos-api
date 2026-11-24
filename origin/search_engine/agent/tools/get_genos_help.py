"""`get_genos_help` for the in-app agent, behind `AGENT_INAPP_HELP_TOOL` (MIP-2328).

The same lookup MCP publishes (MIP-2327, `views/public/mcp/genos_help`): keyword matching over the
vendored manual in `origin/services/guide_help.py`, no model call, no member data. The in-app twin
has its own declaration (MCP's is `mcp_only`, so it is a second `Tool` of the same name, see the note
on `base.FLAGGED_REGISTRY`) and shares `run_help` so the two cannot drift.

With the flag off the tool is neither declared nor runnable in the app and the prompt is byte-identical
to before. With it on, the prompt's "how do I ..." lines send questions about Genos itself here and keep
`search_web` for outside questions (`prompts._with_help_routing`).
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.services import guide_help

FLAG = "AGENT_INAPP_HELP_TOOL"


def run_help(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("`query` is required: the topic or question, in the member's words.")
    language = args.get("language") or "auto"
    if language not in ("auto", "en", "ja"):
        raise ToolError("`language` is `en` or `ja`; leave it out to follow the question.")
    payload = guide_help.answer(query, language)
    payload["__summary__"] = f"{len(payload['matches'])} manual section(s)."
    return payload


GET_GENOS_HELP_INAPP = Tool(
    name="get_genos_help",
    description=(
        "How to do something in Genos itself, or where a setting is, from its manual. "
        "Not for the member's own data (search_knowledge_base) or outside advice (search_web)."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "The topic or question, in the member's words.",
            },
            "language": {
                "type": "STRING",
                "enum": ["auto", "en", "ja"],
                "description": "Default `auto`: follows the question.",
            },
        },
        "required": ["query"],
    },
    run=run_help,
)
