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

from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from apis import settings as production
from origin.search_engine.agent.tools import FLAGGED_REGISTRY, MCP_ONLY_REGISTRY, REGISTRY
from origin.views.public.mcp.tools import CATALOG, published

# What the doc says. Update both together.
# `PUBLISHED` is what a client is offered while every flag is off (the catalog minus `FLAGGED`).
# MIP-1563 added one flag-gated WRITE tool (`delete_task_attachment`, flag `AGENT_PARITY_TOOLS`),
# which is in `CATALOG` but published only while its flag is on. MIP-1059 added one flag-gated READ
# (`get_calendar_event`, the same flag): `FLAGGED_READS`. The flag has been ON by default in production
# since the 2026-11-15 flip, so a client is offered `OFFERED` = `PUBLISHED` + `FLAGGED` = 106 (55 reads,
# 51 writes) by default and `PUBLISHED` = 104 with `AGENT_PARITY_TOOLS=false` (the rollback).
PUBLISHED = 106
FLAGGED = 2
FLAGGED_WRITES = 1
FLAGGED_READS = 1
OFFERED = 108
OFFERED_READS = 57
OFFERED_WRITES = 51
FROM_REGISTRY = (
    83  # published tools backed by a tool in the agent registry (some under another name)
)
FROM_REGISTRY_READS = 47
FROM_REGISTRY_WRITES = 36
MCP_ONLY = 23
# MIP-2046: three more MCP-only reads (list_my_skills, get_skill, run_skill), published only while the
# top-level setting GENOS_SKILLS is on (`McpTool.setting`). They are in CATALOG and MCP_ONLY_REGISTRY but in
# neither PUBLISHED nor OFFERED: with the switch off (the default) a client is offered what it was.
# MIP-2214 added a fourth, a WRITE: `save_html_note`, published only while the setting HTML_NOTES is on
# (off by default). `SKILLS_GATED` are the three reads; `SETTING_GATED` is all of them.
SKILLS_GATED = 3
SETTING_GATED = 4
OFFERED_WITH_SKILLS = OFFERED + SKILLS_GATED
MCP_ONLY_READS = 9
MCP_ONLY_WRITES = 14
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
        self.flagged = [t for t in CATALOG if t.flag]
        self.gated = [t for t in CATALOG if t.setting]
        plain = [t for t in CATALOG if not t.flag and not t.setting]
        self.mcp_only = [t for t in plain if t.registry_name in MCP_ONLY_REGISTRY]
        self.shared = [t for t in plain if t.registry_name in REGISTRY]

    def test_every_published_tool_is_backed_by_exactly_one_registry(self):
        self.assertEqual(
            len(self.mcp_only) + len(self.shared) + len(self.flagged) + len(self.gated),
            len(CATALOG),
        )
        self.assertEqual({t.registry_name for t in self.mcp_only} & set(REGISTRY), set())
        # A flag-gated tool is backed by the app's flag-gated registry and by no other, so it
        # cannot shadow (or be shadowed by) a registered tool of the same name.
        for tool in self.flagged:
            with self.subTest(tool=tool.name):
                self.assertIn(tool.registry_name, FLAGGED_REGISTRY)
                self.assertEqual(FLAGGED_REGISTRY[tool.registry_name][0], tool.flag)
                self.assertNotIn(tool.registry_name, REGISTRY)

    def test_published_total(self):
        self.assertEqual(len(CATALOG) - len(self.flagged) - len(self.gated), PUBLISHED, HOW)
        self.assertEqual(len(self.flagged), FLAGGED, HOW)
        self.assertEqual(sum(1 for t in self.flagged if t.write), FLAGGED_WRITES, HOW)
        self.assertEqual(sum(1 for t in self.flagged if not t.write), FLAGGED_READS, HOW)

    def test_from_the_agent_registry(self):
        self.assertEqual(len(self.shared), FROM_REGISTRY, HOW)
        self.assertEqual(sum(1 for t in self.shared if not t.write), FROM_REGISTRY_READS, HOW)
        self.assertEqual(sum(1 for t in self.shared if t.write), FROM_REGISTRY_WRITES, HOW)

    def test_on_mcp_only(self):
        self.assertEqual(len(self.mcp_only), MCP_ONLY, HOW)
        self.assertEqual(sum(1 for t in self.mcp_only if not t.write), MCP_ONLY_READS, HOW)
        self.assertEqual(sum(1 for t in self.mcp_only if t.write), MCP_ONLY_WRITES, HOW)
        self.assertEqual(len(MCP_ONLY_REGISTRY), MCP_ONLY + SETTING_GATED, HOW)

    def test_the_setting_gated_tools_are_three_skills_reads_and_one_html_write_off_by_default(self):
        self.assertEqual(len(self.gated), SETTING_GATED, HOW)
        self.assertEqual({t.setting for t in self.gated}, {"GENOS_SKILLS", "HTML_NOTES"})
        skills = [t for t in self.gated if t.setting == "GENOS_SKILLS"]
        html = [t for t in self.gated if t.setting == "HTML_NOTES"]
        self.assertEqual(len(skills), SKILLS_GATED, HOW)
        self.assertFalse(any(t.write for t in skills))
        self.assertEqual([(t.name, t.write) for t in html], [("save_html_note", True)])
        for tool in self.gated:
            with self.subTest(tool=tool.name):
                self.assertIn(tool.registry_name, MCP_ONLY_REGISTRY)
        self.assertFalse(settings.GENOS_SKILLS)
        self.assertFalse(settings.HTML_NOTES)
        self.assertFalse(any(published(t) for t in self.gated))
        with self.settings(GENOS_SKILLS=True):
            self.assertTrue(all(published(t) for t in skills))
            self.assertFalse(any(published(t) for t in html))
        with self.settings(HTML_NOTES=True):
            self.assertTrue(all(published(t) for t in html))
            self.assertFalse(any(published(t) for t in skills))
        # Production keeps both switches off until the features are released.
        self.assertFalse(production.GENOS_SKILLS)
        self.assertFalse(production.HTML_NOTES)

    def test_the_registry_and_what_was_left_out(self):
        self.assertEqual(len(REGISTRY), REGISTRY_SIZE, HOW)
        published = {t.registry_name for t in self.shared}
        self.assertEqual(set(REGISTRY) - published, LEFT_OUT, HOW)
        self.assertEqual(len(REGISTRY) - len(published), 5, HOW)

    def test_what_a_client_is_offered_by_default_and_after_the_rollback(self):
        # The production default (`apis.settings`; the test settings keep the flag off) and the rollback.
        with mock.patch.dict(settings.SEARCH_ENGINE, production.SEARCH_ENGINE):
            offered = [t for t in CATALOG if published(t)]
        self.assertEqual(len(offered), OFFERED, HOW)
        self.assertEqual(sum(1 for t in offered if not t.write), OFFERED_READS, HOW)
        self.assertEqual(sum(1 for t in offered if t.write), OFFERED_WRITES, HOW)
        self.assertEqual(OFFERED, PUBLISHED + FLAGGED)
        with mock.patch.dict(
            settings.SEARCH_ENGINE, {**production.SEARCH_ENGINE, "AGENT_PARITY_TOOLS": False}
        ):
            rolled_back = [t for t in CATALOG if published(t)]
        self.assertEqual(len(rolled_back), PUBLISHED, HOW)

    def test_with_the_html_notes_switch_on_a_client_is_offered_one_more_write(self):
        with (
            mock.patch.dict(settings.SEARCH_ENGINE, production.SEARCH_ENGINE),
            self.settings(HTML_NOTES=True),
        ):
            offered = [t for t in CATALOG if published(t)]
        self.assertEqual(len(offered), OFFERED + 1, HOW)
        self.assertEqual(sum(1 for t in offered if not t.write), OFFERED_READS, HOW)
        self.assertEqual(sum(1 for t in offered if t.write), OFFERED_WRITES + 1, HOW)

    def test_with_the_skills_switch_on_a_client_is_offered_three_more_reads(self):
        with (
            mock.patch.dict(settings.SEARCH_ENGINE, production.SEARCH_ENGINE),
            self.settings(GENOS_SKILLS=True),
        ):
            offered = [t for t in CATALOG if published(t)]
        self.assertEqual(len(offered), OFFERED_WITH_SKILLS, HOW)
        self.assertEqual(sum(1 for t in offered if not t.write), OFFERED_READS + SKILLS_GATED, HOW)
        self.assertEqual(sum(1 for t in offered if t.write), OFFERED_WRITES, HOW)
