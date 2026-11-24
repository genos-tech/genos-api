"""`get_genos_help` (MIP-2327): MCP-only, see `help/__init__.py`.

Answers "where do I change X / how do I ... in Genos" from the vendored manual. The lookup is in
`origin/services/guide_help.py`: keyword matching, no model call, no member data.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext, ToolError
from origin.services import guide_help


def _help(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("`query` is required: the topic or question, in the member's words.")
    language = args.get("language") or "auto"
    if language not in ("auto", "en", "ja"):
        raise ToolError("`language` is `en` or `ja`; leave it out to follow the question.")
    payload = guide_help.answer(query, language)
    payload["__summary__"] = f"{len(payload['matches'])} manual section(s)."
    return payload


GET_GENOS_HELP = Tool(
    name="get_genos_help",
    description="Answer a question about using Genos itself from its manual.",
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
    run=_help,
    mcp_only=True,
)
