"""The Genos help tool on MCP (MIP-2327): published to a connected client, never shown to the in-app agent.

One read, MCP-only (see `secretary/__init__.py` for why such tools live outside `origin/search_engine/`:
this tree is not on the path of the billed `agent-evals` run, and the tool is filed in
`MCP_ONLY_REGISTRY`, which the in-app agent cannot read):

  * `get_genos_help`: "where do I change X / how do I ... in Genos", answered from the vendored user
    manual by keyword matching. No model call, no Genos AI credits, no member data.
"""

from origin.search_engine.agent.tools import register_tool
from origin.views.public.mcp.genos_help.get_genos_help import GET_GENOS_HELP

register_tool(GET_GENOS_HELP)
