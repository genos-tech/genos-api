"""`get_skill` (MIP-2046): MCP-only, see `skills/__init__.py`.

One skill with its text, to read before running it or to show the member what it says. The text is
fenced as workspace content (the MCP layer turns that into the untrusted-content notice).
"""

from __future__ import annotations

from typing import Any

from origin.search_engine.agent.tools import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.base import wrap_workspace_content
from origin.services.skills import guard
from origin.services.skills import mcp as skills_mcp


def _get(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    try:
        found = skills_mcp.read_skill(ctx.user_id, ctx.team_id, args.get("skill"))
    except skills_mcp.McpSkillError as exc:
        raise ToolError(str(exc)) from exc
    skill, meta = found["skill"], found["meta"]
    out: dict[str, Any] = {
        "id": str(skill.id),
        "slug": meta.slug or skill.slug,
        "author": found["author"],
        "title": wrap_workspace_content(guard.normalise(meta.title or "")),
        "description": wrap_workspace_content(guard.normalise(meta.description or "")),
        "argument_hint": wrap_workspace_content(guard.normalise(meta.argument_hint or "")) or None,
        "tags": list(meta.tags or []),
        "tool_level": found["tool_level"],
        "visibility": skill.visibility,
        "official": bool(skill.is_official),
        # 0 = the owner's own draft (the live text); a teammate or a reader always gets a version.
        "version_no": found["version_no"],
        "is_draft": found["draft"],
        "text": wrap_workspace_content(found["markdown"]),
        "text_chars": len(found["markdown"]),
        "run_with": "run_skill",
    }
    if found["installed_version_no"] is not None:
        out["installed_version_no"] = found["installed_version_no"]
        out["update_available"] = found["installed_version_no"] < found["published_version_no"]
    if found["author"] == "you" and skill.visibility != "private":
        out["published_version_no"] = found["published_version_no"]
    out["__summary__"] = f"Skill /{out['slug']}."
    return out


GET_SKILL = Tool(
    name="get_skill",
    description="Read one Genos skill: its details and its text.",
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "skill": {
                "type": "STRING",
                "description": "The skill's id or name, from `list_my_skills`.",
            },
        },
        "required": ["skill"],
    },
    run=_get,
    mcp_only=True,
)
