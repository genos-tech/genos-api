"""Tests for the `AGENT_DISABLED_TOOLS` ops kill-switch.

genos-docs `spotlight/SPOTLIGHT_AGENT_CHANGE_SAFETY.md` §4.4: a risky
agent tool can ship dark / be switched off per environment without a
code change. The load-bearing design property under test is FAIL-OPEN —
an environment that doesn't set the var runs every tool (env vars are
per-service on Railway, so "disabled unless set" would silently drop
tools on whichever service missed the var). The flag can only ever
*remove* tools, never be required for one to exist.

No DB, no LLM — declaration-level checks only.
"""

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent import controller
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import REGISTRY

CONTROLLER_LOGGER = "origin.search_engine.agent.controller"


def _se(**overrides):
    return {**settings.SEARCH_ENGINE, **overrides}


class AgentDisabledToolsTests(SimpleTestCase):
    def setUp(self):
        # Reset the once-per-process log latch so log assertions are
        # deterministic regardless of test order.
        controller._KILLSWITCH_LOGGED = False

    def test_fail_open_empty_setting_declares_every_tool(self):
        with override_settings(SEARCH_ENGINE=_se(AGENT_DISABLED_TOOLS=frozenset())):
            declared = {d.name for d in _build_tool_declarations()}
        self.assertEqual(declared, set(REGISTRY))

    def test_disabled_tool_is_hidden_from_declarations(self):
        with override_settings(
            SEARCH_ENGINE=_se(AGENT_DISABLED_TOOLS=frozenset({"search_web"}))
        ):
            declared = {d.name for d in _build_tool_declarations()}
        self.assertEqual(
            declared,
            set(REGISTRY) - {"search_web"},
            "exactly the switched-off tool must disappear; everything else stays",
        )

    def test_unions_with_caller_disabled_set(self):
        """The env switch stacks with per-request disables (web-search
        toggle, §4.5 subsetting) — it can only shrink the surface."""
        with override_settings(
            SEARCH_ENGINE=_se(AGENT_DISABLED_TOOLS=frozenset({"search_web"}))
        ):
            declared = {d.name for d in _build_tool_declarations({"create_task"})}
        self.assertEqual(declared, set(REGISTRY) - {"search_web", "create_task"})

    def test_unknown_name_disables_nothing_and_logs_error(self):
        """Typo guard: the operator thinks something is off that isn't —
        that must be loud (ERROR), and must not eat any real tool."""
        with override_settings(
            SEARCH_ENGINE=_se(AGENT_DISABLED_TOOLS=frozenset({"no_such_tool"}))
        ):
            with self.assertLogs(CONTROLLER_LOGGER, level="ERROR") as logs:
                declared = {d.name for d in _build_tool_declarations()}
        self.assertEqual(declared, set(REGISTRY))
        self.assertTrue(any("no_such_tool" in line for line in logs.output))

    def test_active_switch_logs_once_per_process(self):
        with override_settings(
            SEARCH_ENGINE=_se(AGENT_DISABLED_TOOLS=frozenset({"search_web"}))
        ):
            with self.assertLogs(CONTROLLER_LOGGER, level="WARNING") as logs:
                _build_tool_declarations()
            self.assertTrue(any("search_web" in line for line in logs.output))
            # Second build: filter still applies, but no new log spam.
            with self.assertNoLogs(CONTROLLER_LOGGER, level="WARNING"):
                declared = {d.name for d in _build_tool_declarations()}
        self.assertNotIn("search_web", declared)
