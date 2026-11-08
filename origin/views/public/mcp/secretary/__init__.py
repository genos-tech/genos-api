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
from origin.views.public.mcp.secretary.create_my_schedule import CREATE_MY_SCHEDULE
from origin.views.public.mcp.secretary.delete_my_schedule import DELETE_MY_SCHEDULE
from origin.views.public.mcp.secretary.get_item_line import GET_ITEM_LINE
from origin.views.public.mcp.secretary.get_my_briefing import GET_MY_BRIEFING
from origin.views.public.mcp.secretary.list_my_schedules import LIST_MY_SCHEDULES
from origin.views.public.mcp.secretary.run_my_schedule import RUN_MY_SCHEDULE
from origin.views.public.mcp.secretary.update_my_schedule import UPDATE_MY_SCHEDULE

SCHEDULE_TOOLS = (
    LIST_MY_SCHEDULES,
    CREATE_MY_SCHEDULE,
    UPDATE_MY_SCHEDULE,
    DELETE_MY_SCHEDULE,
    RUN_MY_SCHEDULE,
)
SECRETARY_TOOLS = (GET_MY_BRIEFING, GET_ITEM_LINE, *SCHEDULE_TOOLS)

for _tool in SECRETARY_TOOLS:
    register_tool(_tool)
