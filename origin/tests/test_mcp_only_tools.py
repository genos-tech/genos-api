"""MCP-only tools (MIP-1070): published on MCP, invisible to the in-app agent.

The secretary's briefing and schedules are driven by a connected client's own model.
Anything that changes what the in-app model sees waits for the December eval, so
these tools must not reach the app by any route. They live in `MCP_ONLY_REGISTRY`,
not `REGISTRY`, because the app reads `REGISTRY` both to BUILD what the model sees
(declarations, prompt, tool directory and its count, tiers) and to RUN what the
model asks for (the controller looks a call up by the name the model sent). Here a
throwaway MCP-only tool is registered and each of those routes is checked.
"""

from __future__ import annotations

import logging
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.search_engine.agent import controller, prompts, tool_tiers
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import (
    MCP_ONLY_REGISTRY,
    REGISTRY,
    Tool,
    ToolContext,
    register_tool,
)
from origin.search_engine.agent.tools.base import get_mcp_tool
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools as mcp_tools

NAME = "fake_secretary_ping"


def _se(**overrides):
    return {**settings.SEARCH_ENGINE, **overrides}


def _fake(name=NAME, *, mcp_only=True, requires_approval=False):
    def run(args, ctx):
        return {"echo": args.get("text", ""), "user_id": ctx.user_id, "team_id": ctx.team_id}

    return Tool(
        name=name,
        description="Echo, for a test.",
        parameters_schema={
            "type": "OBJECT",
            "properties": {"text": {"type": "STRING", "description": "What to echo."}},
        },
        run=run,
        requires_approval=requires_approval,
        mcp_only=mcp_only,
    )


def _published(name=NAME, *, write=False):
    return mcp_tools.McpTool(
        name=name, registry_name=name, description="Echo, for a test.", write=write
    )


class _Registered:
    """A tool registered for one test and gone after it, in both registries."""

    def __init__(self, tool):
        self.tool = tool

    def __enter__(self):
        self._patches = [
            mock.patch.dict(REGISTRY),
            mock.patch.dict(MCP_ONLY_REGISTRY),
        ]
        for p in self._patches:
            p.start()
        register_tool(self.tool)
        return self.tool

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


class TestRegistration(SimpleTestCase):
    def test_an_mcp_only_tool_goes_in_its_own_registry_and_never_in_the_apps(self):
        with _Registered(_fake()) as tool:
            self.assertIs(MCP_ONLY_REGISTRY[NAME], tool)
            self.assertNotIn(NAME, REGISTRY)
            self.assertIsNone(REGISTRY.get(NAME), "the app's dispatcher looks tools up here")
            self.assertIs(get_mcp_tool(NAME), tool, "MCP finds it")

    def test_an_ordinary_tool_still_goes_in_the_apps_registry(self):
        with _Registered(_fake("fake_shared_tool", mcp_only=False)) as tool:
            self.assertIs(REGISTRY["fake_shared_tool"], tool)
            self.assertNotIn("fake_shared_tool", MCP_ONLY_REGISTRY)
            self.assertIs(get_mcp_tool("fake_shared_tool"), tool, "MCP finds the shared ones too")

    def test_the_two_registries_never_share_a_name(self):
        self.assertEqual(set(REGISTRY) & set(MCP_ONLY_REGISTRY), set())

    def test_a_name_taken_in_either_registry_is_refused_and_nothing_changes(self):
        with _Registered(_fake()):
            before = (dict(REGISTRY), dict(MCP_ONLY_REGISTRY))
            with self.assertRaisesRegex(ValueError, "already registered"):
                register_tool(_fake())  # twice as MCP-only
            with self.assertRaisesRegex(ValueError, "already registered"):
                register_tool(_fake(mcp_only=False))  # the app's, over an MCP-only name
            with self.assertRaisesRegex(ValueError, "already registered"):
                register_tool(_fake("search_knowledge_base"))  # MCP-only, over an app tool
            self.assertEqual((dict(REGISTRY), dict(MCP_ONLY_REGISTRY)), before)

    def test_the_fence_stripping_wrapper_applies_to_an_mcp_only_tool_too(self):
        with _Registered(_fake()) as tool:
            self.assertTrue(hasattr(tool.run, "__wrapped__"))

    def test_the_real_registries_hold_no_mcp_only_tool_in_the_apps_and_the_reverse(self):
        self.assertFalse([n for n, t in REGISTRY.items() if t.mcp_only])
        self.assertFalse([n for n, t in MCP_ONLY_REGISTRY.items() if not t.mcp_only])


class TestTheAppNeverSeesOne(SimpleTestCase):
    """Everything the in-app model sees or can run is the same with one registered."""

    def _app_view(self):
        declared = [
            (d.name, d.description, d.parameters_schema) for d in _build_tool_declarations()
        ]
        with override_settings(SEARCH_ENGINE=_se(AGENT_CHEATSHEET_FROM_REGISTRY=True)):
            directory = prompts._generated_tool_directory()
            writes = prompts._generated_write_tools_block()
            prompt = prompts._build_registry_prompt()
        tiers = {
            level: tool_tiers.disabled_tools_for_level(level)
            for level in ("organize", "act", "read")
        }
        return {
            "declarations": declared,
            "directory": directory,
            "write_block": writes,
            "registry_prompt": prompt,
            "tiers": tiers,
            "names": sorted(REGISTRY),
        }

    def test_declarations_prompt_directory_count_and_tiers_are_unchanged(self):
        without = self._app_view()
        with (
            _Registered(_fake()),
            _Registered(_fake("fake_secretary_write", requires_approval=True)),
        ):
            with_one = self._app_view()
        self.assertEqual(with_one["declarations"], without["declarations"])
        self.assertEqual(with_one["directory"], without["directory"])
        self.assertEqual(with_one["write_block"], without["write_block"])
        self.assertEqual(with_one["registry_prompt"], without["registry_prompt"])
        self.assertEqual(with_one["tiers"], without["tiers"])
        self.assertEqual(with_one["names"], without["names"])

    def test_the_tool_count_the_prompt_states_does_not_move(self):
        count = len(REGISTRY)
        with _Registered(_fake()):
            self.assertIn(f"all {count} tools", prompts._generated_tool_directory())
            self.assertNotIn(NAME, prompts._generated_tool_directory())

    def test_the_apps_dispatcher_cannot_find_one_by_the_name_a_model_sends(self):
        """The controller runs `REGISTRY.get(call.name)` and treats None as an
        unknown tool: the guarantee is that this is None, not that it was undeclared."""
        with _Registered(_fake()):
            self.assertIsNone(REGISTRY.get(NAME))

    def test_a_pending_approval_for_one_is_vetoed_as_unknown(self):
        """Resume looks the paused call up the same way (`REGISTRY.get(call_name)`)."""
        with _Registered(_fake()):
            self.assertIsNone(REGISTRY.get(NAME))
            self.assertIsNone(getattr(REGISTRY.get(NAME), "requires_approval", None))


class TestTheKillSwitch(SimpleTestCase):
    def setUp(self):
        controller._KILLSWITCH_LOGGED = False

    def test_naming_an_mcp_only_tool_is_not_a_typo(self):
        with (
            _Registered(_fake()),
            override_settings(SEARCH_ENGINE=_se(AGENT_DISABLED_TOOLS=frozenset({NAME}))),
            self.assertNoLogs(controller.log, level=logging.ERROR),
        ):
            self.assertEqual(controller._operator_disabled_tools(), {NAME})

    def test_a_misspelt_name_still_logs_the_typo_error(self):
        with (
            _Registered(_fake()),
            override_settings(
                SEARCH_ENGINE=_se(AGENT_DISABLED_TOOLS=frozenset({NAME, "fake_secretary_pnig"}))
            ),
            self.assertLogs(controller.log, level=logging.ERROR) as logs,
        ):
            self.assertEqual(controller._operator_disabled_tools(), {NAME})
        self.assertIn("fake_secretary_pnig", "\n".join(logs.output))


class TestMcpPublishesOne(McpBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_READ)

    def _published_with(self, write=False):
        tool = _published(write=write)
        stack = [
            _Registered(_fake()),
            mock.patch.object(mcp_tools, "CATALOG", [*mcp_tools.CATALOG, tool]),
            mock.patch.dict(mcp_tools.BY_NAME, {NAME: tool}),
        ]
        return stack

    def _listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def test_it_is_listed_with_the_schema_it_declares(self):
        stack = self._published_with()
        for cm in stack:
            cm.__enter__()
        try:
            listed = self._listed()
            self.assertIn(NAME, listed)
            self.assertEqual(listed[NAME]["inputSchema"]["properties"]["text"]["type"], "string")
        finally:
            for cm in reversed(stack):
                cm.__exit__(None, None, None)
        self.assertNotIn(NAME, self._listed(), "and gone with the patch")

    def test_it_runs_as_the_caller_in_the_callers_team(self):
        stack = self._published_with()
        for cm in stack:
            cm.__enter__()
        try:
            result = self._result(self._call(NAME, {"text": "hello"}))
        finally:
            for cm in reversed(stack):
                cm.__exit__(None, None, None)
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["echo"], "hello")
        self.assertEqual(result["structuredContent"]["user_id"], str(self.user.id))
        self.assertEqual(result["structuredContent"]["team_id"], str(self.team.team_id))

    def test_an_unknown_argument_is_refused_as_for_any_tool(self):
        stack = self._published_with()
        for cm in stack:
            cm.__enter__()
        try:
            text, is_error = self._tool_text(self._call(NAME, {"txt": "x"}))
        finally:
            for cm in reversed(stack):
                cm.__exit__(None, None, None)
        self.assertTrue(is_error)
        self.assertIn("text", text)

    def test_a_write_tool_needs_a_write_key(self):
        stack = self._published_with(write=True)
        for cm in stack:
            cm.__enter__()
        try:
            text, is_error = self._tool_text(self._call(NAME, {"text": "x"}))
            self.assertTrue(is_error)
            self.assertIn("read-only", text)
            self._key(scope=SCOPE_WRITE)
            self.assertFalse(self._result(self._call(NAME, {"text": "x"}))["isError"])
        finally:
            for cm in reversed(stack):
                cm.__exit__(None, None, None)

    def test_the_ops_kill_switch_hides_it_and_refuses_the_call(self):
        stack = self._published_with()
        for cm in stack:
            cm.__enter__()
        try:
            with override_settings(SEARCH_ENGINE=_se(AGENT_DISABLED_TOOLS=frozenset({NAME}))):
                self.assertNotIn(NAME, self._listed())
                res = self._call(NAME, {"text": "x"})
                self.assertIn("error", res.data, "an unknown tool is a protocol error")
        finally:
            for cm in reversed(stack):
                cm.__exit__(None, None, None)

    def test_a_published_name_missing_from_both_registries_fails_loudly(self):
        tool = _published("fake_unregistered")
        with self.assertRaises(KeyError):
            mcp_tools.input_schema(tool)


class TestNoDeadMcpOnlyTools(McpBase):
    def test_every_mcp_only_tool_is_published(self):
        """An MCP-only tool nobody can call is dead code: publish it on the catalog."""
        published = {t.registry_name for t in mcp_tools.CATALOG}
        self.assertEqual(set(MCP_ONLY_REGISTRY) - published, set())

    def test_every_catalog_entry_resolves_to_a_registered_tool(self):
        for tool in mcp_tools.CATALOG:
            self.assertIsNotNone(get_mcp_tool(tool.registry_name), tool.name)

    def test_the_app_context_type_still_builds_for_an_mcp_only_tool(self):
        with _Registered(_fake()) as tool:
            out = tool.run({"text": "a"}, ToolContext(team_id="t", user_id="u"))
        self.assertEqual(out["echo"], "a")
