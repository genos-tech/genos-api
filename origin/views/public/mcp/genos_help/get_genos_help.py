"""`get_genos_help` (MIP-2327): MCP-only, see `help/__init__.py`.

Answers "where do I change X / how do I ... in Genos" from the vendored manual. The lookup is in
`origin/services/guide_help.py`: keyword matching, no model call, no member data.
"""

from __future__ import annotations

from origin.search_engine.agent.tools import Tool
from origin.search_engine.agent.tools.get_genos_help import run_help

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
    run=run_help,
    mcp_only=True,
)
