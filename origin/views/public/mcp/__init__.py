"""Genos as an MCP server — the Genos tools, published to outside agents.

`POST /api/public/v1/mcp`, authenticated with the same
`Authorization: ApiKey gnos_…` header as the rest of the public API.

The point is to close a manual loop: today you write a task in Genos, set
it to WIP, and then paste its description into a coding agent by hand.
With this, the agent reads the task itself, does the work, comments the PR
link back, and closes it — you only ever write the description.

The whole thing is a thin dispatcher, because the hard parts already
exist. `Tool.run(args, ctx)` in `search_engine/agent/tools/` is
self-contained and enforces its own ACL, and `agent_digest` already
proves a tool can be invoked outside any HTTP request from a bare
`ToolContext`. So there is no LLM here and no agent loop — just JSON-RPC
in, `run()`, JSON-RPC out.

    protocol.py  JSON-RPC envelope, the two protocol eras, error codes
    tools.py     the curated catalog and the registry adapter
    resolve.py   "PRJ-123" / a task URL / a raw id  ->  a task id
    views.py     the endpoint: auth, team resolution, dispatch
"""
