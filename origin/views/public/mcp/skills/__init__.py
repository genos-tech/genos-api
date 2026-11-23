"""Genos Skills on MCP (MIP-2046): published to a connected client, never shown to the in-app agent.

Three tools, all reads, all MCP-only (see `secretary/__init__.py` for why those live outside
`origin/search_engine/`: this tree is not on the path of the billed `agent-evals` run, and the tools
are filed in `MCP_ONLY_REGISTRY`, which the in-app agent cannot read):

  * `list_my_skills`: what the member may run in this team, in the order `/name` resolves a name;
  * `get_skill`: one skill with its text, fenced as untrusted;
  * `run_skill`: the skill's steps as instructions for the CLIENT'S model. Genos runs no model and
    spends no Genos AI credits.

They exist only while `GENOS_SKILLS` is on (`McpTool.setting`): off, they are neither listed nor
callable. The rules live in `origin/services/skills/mcp.py`; one tool per module, as in
`search_engine/agent/tools/`.
"""

from origin.search_engine.agent.tools import register_tool
from origin.views.public.mcp.skills.get_skill import GET_SKILL
from origin.views.public.mcp.skills.list_my_skills import LIST_MY_SKILLS
from origin.views.public.mcp.skills.run_skill import RUN_SKILL

SKILL_TOOLS = (LIST_MY_SKILLS, GET_SKILL, RUN_SKILL)

for _tool in SKILL_TOOLS:
    register_tool(_tool)
