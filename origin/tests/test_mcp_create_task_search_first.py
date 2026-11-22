"""The "search first" line on the MCP (MIP-1619) and in-app (MIP-1769) `create_task` descriptions.

The MCP catalog text and the in-app agent's declaration are two different strings: the in-app
model reads `Tool.description` in `search_engine/agent/tools/create_task.py` (plus the catalog's
ARGUMENT-level wording via `references.declared_schema`), never `McpTool.description`. So the
rule is MCP-only, and a test must fail if it ever leaks into what the in-app model sees, because
that would move the prompt prefix, the declaration golden and the routing evals.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from origin.search_engine.agent import references
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.views.public.mcp import tools

SEARCH_FIRST = "Search first (`search_workspace`): a duplicate is worse than a missing task"


class TestCreateTaskSearchFirst(SimpleTestCase):
    def test_the_mcp_description_says_search_first(self):
        text = tools.description_for(tools.BY_NAME["create_task"])
        self.assertIn(SEARCH_FIRST, text)

    def test_the_line_names_a_published_tool(self):
        self.assertIn("search_workspace", tools.BY_NAME)

    def test_the_in_app_agent_reads_its_own_wording_of_the_rule(self):
        # MIP-1769: the in-app model gets the rule too, naming ITS search tool (the MCP line names
        # `search_workspace`, which the in-app agent does not have).
        self.assertIn("Search first (`search_knowledge_base`)", CREATE_TASK.description)
        self.assertIn("a duplicate is worse than a missing task", CREATE_TASK.description)
        self.assertNotIn("search_workspace", CREATE_TASK.description)

    def test_the_line_names_a_tool_the_in_app_agent_has(self):
        from origin.search_engine.agent.tools import REGISTRY

        self.assertIn("search_knowledge_base", REGISTRY)

    def test_the_argument_schema_is_unchanged(self):
        declared = references.declared_schema("create_task", CREATE_TASK.parameters_schema)
        self.assertNotIn("Search first", str(declared))
