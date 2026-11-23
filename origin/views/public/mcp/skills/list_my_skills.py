"""`list_my_skills` (MIP-2046): MCP-only, see `skills/__init__.py`.

The Genos skills the member may run in this team, in the order `/name` resolves a typed name. The
logic, and every access rule, is in `origin/services/skills/mcp.py`.
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.base import wrap_workspace_content
from origin.services.skills import mcp as skills_mcp
from origin.services.skills import run


def _list(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        out = skills_mcp.list_runnable(ctx.user_id, ctx.team_id, args.get("query"))
    except skills_mcp.McpSkillError as exc:
        raise ToolError(str(exc)) from exc
    rows = []
    for r in out["skills"]:
        # A teammate's or a stranger's words are data: fenced. The member's own are not.
        mine = r["group"] == run.GROUP_MINE

        def words(value: str | None) -> str | None:
            return value if mine or not value else wrap_workspace_content(value)

        rows.append(
            {
                **r,
                "title": words(r["title"]),
                "description": words(r["description"]),
                "argument_hint": words(r["argument_hint"]),
                "owner": words(r["owner"]),
            }
        )
    payload: dict[str, Any] = {"skills": rows}
    if not rows:
        payload["note"] = (
            "No skill matches. Skills are written in the Genos app (Skills); one a teammate "
            "published to the team, or a public one the member installed, appears here too."
            if out["query"]
            else "The member has no skills to run yet. They are written in the Genos app "
            "(Skills); one a teammate published to the team, or a public one the member "
            "installed, appears here too."
        )
    payload["__summary__"] = f"{len(rows)} skill(s)."
    return payload


LIST_MY_SKILLS = Tool(
    name="list_my_skills",
    description="List the Genos skills the member may run in this team.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Only skills whose name, title or description contains this text.",
            },
        },
    },
    run=_list,
    mcp_only=True,
)
