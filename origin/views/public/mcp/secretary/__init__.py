"""The secretary's MCP-only tools (MIP-1070): published to a connected client, never
declared to the in-app agent.

They live here, not under `origin/search_engine/`, on purpose. Anything under that
tree is on the path of the billed `agent-evals` run, which exists to catch a change in
what the in-app model sees, and these tools change nothing it sees: they are filed in
`MCP_ONLY_REGISTRY`, which the in-app agent cannot read. Importing this package
registers them; `mcp/tools.py` imports it before it builds the catalog.

One tool per module, as in `search_engine/agent/tools/`: the catalog contract tests
read each tool's own module to check it only takes the arguments it declares.
"""

from origin.search_engine.agent.tools import register_tool
from origin.views.public.mcp.secretary.capture_worklog import CAPTURE_WORKLOG
from origin.views.public.mcp.secretary.delete_worklog_entry import DELETE_WORKLOG_ENTRY
from origin.views.public.mcp.secretary.get_my_briefing import GET_MY_BRIEFING
from origin.views.public.mcp.secretary.list_my_worklog import LIST_MY_WORKLOG
from origin.views.public.mcp.secretary.update_worklog_item import UPDATE_WORKLOG_ITEM

WORKLOG_TOOLS = (CAPTURE_WORKLOG, LIST_MY_WORKLOG, UPDATE_WORKLOG_ITEM, DELETE_WORKLOG_ENTRY)
SECRETARY_TOOLS = (GET_MY_BRIEFING, *WORKLOG_TOOLS)

for _tool in SECRETARY_TOOLS:
    register_tool(_tool)
