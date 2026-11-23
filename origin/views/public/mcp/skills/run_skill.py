"""`run_skill` (MIP-2046): MCP-only, see `skills/__init__.py`.

Hands a skill's steps to the CONNECTED client's model. Genos runs no model and spends no Genos AI
credits: that is the point of doing it here and not through the in-app agent (design note 274,
section 11 and decision K4). Nothing is written on our side, so it is a read.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext, ToolError
from origin.services.skills import mcp as skills_mcp


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        out = skills_mcp.run_instructions(
            ctx.user_id, ctx.team_id, args.get("skill"), args.get("input") or ""
        )
    except skills_mcp.McpSkillError as exc:
        raise ToolError(str(exc)) from exc
    resolved = out["resolved"]
    return {
        "slug": resolved.slug,
        "version_no": resolved.version_no,
        "is_draft": resolved.draft,
        "author": resolved.author,
        "tool_level": resolved.tool_level,
        "genos_ai_credits_spent": 0,
        "instructions": out["instructions"],
        "__summary__": f"Instructions for /{resolved.slug}.",
    }


RUN_SKILL = Tool(
    name="run_skill",
    description="Get a Genos skill's steps to follow yourself.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "skill": {
                "type": "STRING",
                "description": "The skill's id or name, from `list_my_skills`.",
            },
            "input": {
                "type": "STRING",
                "description": "What the member wants the skill applied to, in their words.",
            },
        },
        "required": ["skill"],
    },
    run=_run,
    mcp_only=True,
)
