"""The "search first" line on the MCP `create_task` description (MIP-1619).

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

    def test_the_in_app_agent_does_not_read_it(self):
        self.assertNotIn("Search first", CREATE_TASK.description)
        self.assertNotIn("duplicate", CREATE_TASK.description)
        declared = references.declared_schema("create_task", CREATE_TASK.parameters_schema)
        self.assertNotIn("Search first", str(declared))
        self.assertNotIn("a duplicate is worse", str(declared))
