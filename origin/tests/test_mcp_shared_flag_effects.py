"""MIP-1478: what an in-app agent flag does to the MCP surface, which serves the same functions.

Four things, each pinned here:

  1. With the closed-ancestor flag on, MCP `get_task` / `list_tasks` computed the rule twice (the
     tool, then the MCP augment). The tool leaves it to the augment on MCP: one computation, the same
     result.
  2. MCP `get_current_user` promises "user id, name, team". `team` came only with the in-app flag
     `AGENT_TEAM_IN_CURRENT_USER`; on MCP it is returned whatever the flag says (the description is
     the promise).
  3. The MCP catalog says what the flags add to a result a connected client reads (`anchors`, a report's
     `item_body_cut`, the effective reading of the counts): the sentence is appended to the MCP
     description exactly while the flag is on, and with every flag off the descriptions are the
     catalog's own text, byte for byte.
  4. The flags that only the in-app model reads (note threads: MCP has its own) add nothing to MCP.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings
from django.test import override_settings

from origin.search_engine.agent.tool_flag_text import ADDITIONS
from origin.search_engine.agent.tools import ancestor_rule
from origin.search_engine.agent.tools.fetch_note import DESCRIPTION_ADDITION as NOTE_THREADS
from origin.search_engine.agent.tools.get_current_user import DESCRIPTION_ADDITION as TEAM
from origin.search_engine.agent.tools.inbox_report import DESCRIPTION_ADDITION as INBOX_REPORT
from origin.search_engine.agent.tools.task_anchors import DESCRIPTION_ADDITION as ANCHORS
from origin.tests.test_task_ancestry import AncestryBase
from origin.views.public.mcp import tools as mcp_tools

CLOSED = "AGENT_CLOSED_ANCESTOR_RULE"
ALL_FLAGS = {flag for _, flag, _ in ADDITIONS}


def engine(**flags):
    return override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **flags})


def all_off():
    return engine(**{flag: False for flag in ALL_FLAGS})


def all_on():
    return engine(**{flag: True for flag in ALL_FLAGS})


def mcp_tool(registry_name):
    (tool,) = [t for t in mcp_tools.CATALOG if t.registry_name == registry_name]
    return tool


class TheRuleIsComputedOnceOnMcp(AncestryBase):
    def setUp(self):
        super().setUp()
        self.parent = self.make("top", status="Closed")
        self.child = self.make("leaf", parent=self.parent, status="WIP")
        self._key()

    def _payload(self, name, args):
        return self._result(self._call(name, args))["structuredContent"]

    def _calls(self, name, args):
        # `ancestor_verdicts` is the walk both the tool's and the augment's reading end in
        from origin.services import task_ancestry  # noqa: PLC0415

        with mock.patch.object(
            task_ancestry, "ancestor_verdicts", wraps=task_ancestry.ancestor_verdicts
        ) as spy:
            payload = self._payload(name, args)
        return spy.call_count, payload

    def test_get_task_reads_the_ancestors_once_and_says_the_same(self):
        args = {"task_id": self.child.task_id}
        with engine(**{CLOSED: False}):
            off_calls, off = self._calls("get_task", args)
        with engine(**{CLOSED: True}):
            on_calls, on = self._calls("get_task", args)
        self.assertEqual((off_calls, on_calls), (1, 1))  # the MCP augment's, flag or no flag
        self.assertEqual(on, off)
        self.assertEqual(on["effective_status"], "Closed")

    def test_list_tasks_reads_the_ancestors_once_and_says_the_same(self):
        args = {"limit": 20}
        with engine(**{CLOSED: False}):
            off_calls, off = self._calls("list_tasks", args)
        with engine(**{CLOSED: True}):
            on_calls, on = self._calls("list_tasks", args)
        self.assertEqual((off_calls, on_calls), (1, 1))
        self.assertEqual(on, off)
        rows = {r["task_id"]: r for r in on["tasks"]}
        self.assertEqual(rows[self.child.task_id]["effective_status"], "Closed")

    def test_the_in_app_tool_still_adds_the_fields_itself(self):
        # the other surface is untouched: it has no augment, so the tool does the work
        from origin.search_engine.agent.tools import REGISTRY, ToolContext  # noqa: PLC0415

        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        with engine(**{CLOSED: True}):
            out = REGISTRY["fetch_task"].run({"task_id": self.child.task_id}, ctx)
        self.assertEqual(out["effective_status"], "Closed")
        self.assertEqual(
            out["closed_by_ancestor"],
            ancestor_rule.row_fields([self.child.task_id], ctx)[self.child.task_id][
                "closed_by_ancestor"
            ],
        )

    def test_overdue_only_keeps_the_rule_on_mcp(self):
        # only the duplicate computation left the tool: the overdue exclusion is a filter, not a field
        from datetime import date, timedelta  # noqa: PLC0415

        from origin.models.task.task_models import TaskMaster  # noqa: PLC0415

        TaskMaster.objects.filter(pk=self.child.pk).update(
            due_date=date.today() - timedelta(days=3)
        )
        with engine(**{CLOSED: True}):
            on = self._payload("list_tasks", {"overdue_only": True, "limit": 20})
        self.assertEqual([r["task_id"] for r in on["tasks"]], [])


class TeamOnMcp(AncestryBase):
    def test_get_current_user_returns_the_team_whatever_the_in_app_flag_says(self):
        self._key(team=self.team)
        for flag in (False, True):
            with engine(AGENT_TEAM_IN_CURRENT_USER=flag):
                payload = self._result(self._call("get_current_user"))["structuredContent"]
            self.assertEqual(payload["team"]["team_id"], str(self.team.team_id), flag)
            self.assertEqual(payload["team"]["team_name"], self.team.team_name, flag)

    def test_in_app_with_the_flag_off_still_has_no_team(self):
        from origin.search_engine.agent.tools import REGISTRY, ToolContext  # noqa: PLC0415

        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        with engine(AGENT_TEAM_IN_CURRENT_USER=False):
            self.assertNotIn("team", REGISTRY["get_current_user"].run({}, ctx))
        with engine(AGENT_TEAM_IN_CURRENT_USER=True):
            self.assertIn("team", REGISTRY["get_current_user"].run({}, ctx))


class TheCatalogSaysWhatTheFlagsAdd(AncestryBase):
    def test_every_description_is_the_catalogs_own_text_with_the_flags_off(self):
        with all_off():
            for tool in mcp_tools.CATALOG:
                self.assertEqual(mcp_tools.shared_flag_text(tool), "", tool.name)
                self.assertEqual(mcp_tools.description_for(tool), tool.description, tool.name)

    def test_a_flag_adds_its_sentence_to_the_tool_that_returns_the_field(self):
        with engine(AGENT_FETCH_TASK_ANCHORS=True):
            text = mcp_tools.description_for(mcp_tool("fetch_task"))
        self.assertIn("`anchors` lists where this task's conversation and note are", text)
        # MCP's own names, not the in-app ones
        self.assertIn("`get_chat_thread`", text)
        self.assertIn("call `get_note`", text)
        self.assertNotIn("fetch_note", text)
        with engine(AGENT_FETCH_TASK_ANCHORS=False):
            self.assertNotIn("anchors", mcp_tools.description_for(mcp_tool("fetch_task")))

        with engine(AGENT_INBOX_REPORT_LABEL=True):
            self.assertIn("`genos_report`", mcp_tools.description_for(mcp_tool("list_my_inbox")))
        with engine(AGENT_INBOX_REPORT_LABEL=False):
            self.assertNotIn("genos_report", mcp_tools.description_for(mcp_tool("list_my_inbox")))

    def test_the_closed_ancestor_counts_and_the_focus_list_say_how_they_read_a_task(self):
        counts = (
            "get_my_task_summary",
            "get_milestone_summary",
            "get_sprint_summary",
            "list_milestones",
        )
        with engine(**{CLOSED: True}):
            for name in counts:
                self.assertIn(
                    ancestor_rule.COUNTS_ADDITION, mcp_tools.description_for(mcp_tool(name)), name
                )
            self.assertIn(
                ancestor_rule.FOCUS_ADDITION,
                mcp_tools.description_for(mcp_tool("get_my_focus_tasks")),
            )
        with engine(**{CLOSED: False}):
            for name in (*counts, "get_my_focus_tasks"):
                text = mcp_tools.description_for(mcp_tool(name))
                self.assertNotIn("reads as Closed", text, name)

    def test_what_mcp_already_says_or_does_not_return_is_not_added(self):
        with all_on():
            # the task reads already carry their own `effective_status` text (and the augment)
            for name in ("fetch_task", "list_tasks"):
                text = mcp_tools.description_for(mcp_tool(name))
                self.assertEqual(text.count("`closed_by_ancestor`"), 1, name)
            # note threads: MCP has its own; the team is promised by the catalog text
            self.assertNotIn(NOTE_THREADS, mcp_tools.description_for(mcp_tool("fetch_note")))
            self.assertNotIn(TEAM, mcp_tools.description_for(mcp_tool("get_current_user")))

    def test_the_sentences_are_the_in_app_ones_so_the_two_surfaces_cannot_drift(self):
        with all_on():
            self.assertIn(
                mcp_tools._rewrite_refs(ANCHORS), mcp_tools.description_for(mcp_tool("fetch_task"))
            )
            self.assertIn(INBOX_REPORT, mcp_tools.description_for(mcp_tool("list_my_inbox")))
