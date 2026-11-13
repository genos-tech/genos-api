"""The tool counts that `genos-docs/operations/MCP_SERVER.md` states in §3 (MIP-1448).

That section opens with how many tools the server publishes, how many of them are the
agent registry's and how many exist on MCP only, with the read/write split. The sentence was
written by hand and had drifted three times (it said 89 published and 7 MCP-only when the code
had 96 and 13), because nothing failed when a tool was added.

This pins the numbers. A failure here is not a bug in the code: it means a tool was added,
removed or moved, and the sentence in MCP_SERVER.md §3 (and the numbers below) must change in
the same PR. Same idea as `BUDGET_CHARS` in `test_agent_prefix_budget`.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.views.public.mcp.tools import CATALOG

# What the doc says. Update both together.
PUBLISHED = 96
FROM_REGISTRY = (
    83  # published tools backed by a tool in the agent registry (some under another name)
)
FROM_REGISTRY_READS = 47
FROM_REGISTRY_WRITES = 36
MCP_ONLY = 13
MCP_ONLY_READS = 6
MCP_ONLY_WRITES = 7
REGISTRY_SIZE = 88
# Registry tools with no MCP tool at all: 4 reads, and `delete_project` (a write that is in the app only).
LEFT_OUT = {
    "delete_project",
    "get_my_schedule",
    "list_my_milestones",
    "search_past_conversations",
    "search_web",
}

HOW = (
    "A tool was added, removed or moved. Update the counts at the start of §3 of "
    "genos-docs operations/MCP_SERVER.md AND the constants in this test, in the same change."
)


class McpToolCountTests(SimpleTestCase):
    def setUp(self):
        self.mcp_only = [t for t in CATALOG if t.registry_name in MCP_ONLY_REGISTRY]
        self.shared = [t for t in CATALOG if t.registry_name in REGISTRY]

    def test_every_published_tool_is_backed_by_exactly_one_registry(self):
        self.assertEqual(len(self.mcp_only) + len(self.shared), len(CATALOG))
        self.assertEqual({t.registry_name for t in self.mcp_only} & set(REGISTRY), set())

    def test_published_total(self):
        self.assertEqual(len(CATALOG), PUBLISHED, HOW)

    def test_from_the_agent_registry(self):
        self.assertEqual(len(self.shared), FROM_REGISTRY, HOW)
        self.assertEqual(sum(1 for t in self.shared if not t.write), FROM_REGISTRY_READS, HOW)
        self.assertEqual(sum(1 for t in self.shared if t.write), FROM_REGISTRY_WRITES, HOW)

    def test_on_mcp_only(self):
        self.assertEqual(len(self.mcp_only), MCP_ONLY, HOW)
        self.assertEqual(sum(1 for t in self.mcp_only if not t.write), MCP_ONLY_READS, HOW)
        self.assertEqual(sum(1 for t in self.mcp_only if t.write), MCP_ONLY_WRITES, HOW)
        self.assertEqual(len(MCP_ONLY_REGISTRY), MCP_ONLY, HOW)

    def test_the_registry_and_what_was_left_out(self):
        self.assertEqual(len(REGISTRY), REGISTRY_SIZE, HOW)
        published = {t.registry_name for t in self.shared}
        self.assertEqual(set(REGISTRY) - published, LEFT_OUT, HOW)
        self.assertEqual(len(REGISTRY) - len(published), 5, HOW)
