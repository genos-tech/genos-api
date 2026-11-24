"""`get_genos_help` for the in-app agent, behind `AGENT_INAPP_HELP_TOOL` (MIP-2328).

Flag off: the tool is neither declared nor runnable, and the prompt and declarations are what main serves
(the pins in `test_agent_pins_registry` / `record_agent_goldens` hold the bytes; these tests hold the
behaviour). Flag on: the tool is declared once and runs the manual lookup, the two "how do I" lines send
Genos questions to it and keep `search_web` for outside ones, and the prefix stays inside its budget.
Stubbed: no model is called.
"""

from __future__ import annotations

import json
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from apis import settings as production
from origin.search_engine.agent import prompts
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.prompts import (
    AGENT_SYSTEM_PROMPT,
    AGENT_SYSTEM_PROMPT_REGISTRY,
    agent_system_prompt,
)
from origin.search_engine.agent.tools import (
    MCP_ONLY_REGISTRY,
    REGISTRY,
    ToolContext,
    ToolError,
    flagged_tools,
    get_app_tool,
    get_mcp_tool,
)
from origin.search_engine.agent.tools.get_genos_help import GET_GENOS_HELP_INAPP
from origin.tests.test_agent_prefix_budget import BUDGET_CHARS

FLAG = "AGENT_INAPP_HELP_TOOL"


def flag(on: bool, **more):
    return mock.patch.dict(
        settings.SEARCH_ENGINE,
        {FLAG: on, "AGENT_CHEATSHEET_FROM_REGISTRY": False, "AGENT_REJECT_REASON": False, **more},
    )


class FlagOff(SimpleTestCase):
    def test_the_flag_defaults_to_off(self):
        self.assertIs(production.SEARCH_ENGINE[FLAG], False)

    def test_not_declared_not_runnable_prompt_unchanged(self):
        with flag(False):
            self.assertNotIn("get_genos_help", [d.name for d in _build_tool_declarations()])
            self.assertIsNone(get_app_tool("get_genos_help"))
            self.assertNotIn("get_genos_help", [t.name for t in flagged_tools()])
            self.assertEqual(agent_system_prompt(), AGENT_SYSTEM_PROMPT)
            self.assertNotIn("get_genos_help", agent_system_prompt())

    def test_the_registry_prompt_is_unchanged_too(self):
        with flag(False, AGENT_CHEATSHEET_FROM_REGISTRY=True):
            self.assertEqual(agent_system_prompt(), AGENT_SYSTEM_PROMPT_REGISTRY)


class FlagOn(SimpleTestCase):
    def test_declared_once_after_the_unflagged_tools(self):
        with flag(False):
            off = [d.name for d in _build_tool_declarations()]
        with flag(True):
            on = [d.name for d in _build_tool_declarations()]
        self.assertEqual(on[: len(off)], off)
        self.assertEqual(on[len(off) :], ["get_genos_help"])

    def test_the_app_runs_it_and_mcp_keeps_its_own_twin(self):
        with flag(True):
            self.assertIs(get_app_tool("get_genos_help"), GET_GENOS_HELP_INAPP)
        self.assertNotIn("get_genos_help", REGISTRY)
        self.assertTrue(get_mcp_tool("get_genos_help").mcp_only)
        self.assertIs(get_mcp_tool("get_genos_help"), MCP_ONLY_REGISTRY["get_genos_help"])
        self.assertFalse(GET_GENOS_HELP_INAPP.mcp_only)
        self.assertFalse(GET_GENOS_HELP_INAPP.requires_approval)

    def test_the_declaration_is_short(self):
        self.assertLess(len(GET_GENOS_HELP_INAPP.description), 220)
        self.assertEqual(GET_GENOS_HELP_INAPP.parameters_schema["required"], ["query"])

    def test_it_runs_the_manual_lookup(self):
        ctx = ToolContext(team_id="t", user_id="u")
        with mock.patch(
            "origin.search_engine.agent.tools.get_genos_help.guide_help.answer",
            return_value={"matches": [{"heading": "x"}], "settings": []},
        ) as answer:
            out = GET_GENOS_HELP_INAPP.run(
                {"query": " two-step verification ", "language": "en"}, ctx
            )
        answer.assert_called_once_with(" two-step verification ", "en")
        self.assertEqual(out["__summary__"], "1 manual section(s).")

    def test_a_bad_call_is_an_error_the_model_can_read(self):
        ctx = ToolContext(team_id="t", user_id="u")
        with self.assertRaises(ToolError):
            GET_GENOS_HELP_INAPP.run({"query": "  "}, ctx)
        with self.assertRaises(ToolError):
            GET_GENOS_HELP_INAPP.run({"query": "x", "language": "fr"}, ctx)

    def test_how_do_i_about_genos_goes_to_help_and_outside_questions_keep_the_web(self):
        for registry_prompt in (False, True):
            with (
                self.subTest(registry=registry_prompt),
                flag(True, AGENT_CHEATSHEET_FROM_REGISTRY=registry_prompt),
            ):
                text = agent_system_prompt()
                self.assertEqual(text.count("get_genos_help"), 2)
                self.assertNotIn('"how do I …" or best-practice questions, use search_web', text)
                self.assertIn("For outside or\n     best-practice questions, use search_web.", text)
                self.assertIn("External / best-practice questions:\n  - search_web (Tavily)", text)

    def test_the_prefix_with_the_flag_on_stays_inside_its_budget(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {**production.SEARCH_ENGINE, FLAG: True}):
            declared = sum(
                len(d.name)
                + len(d.description)
                + len(json.dumps(d.parameters_schema, ensure_ascii=False))
                for d in _build_tool_declarations()
            )
            self.assertLessEqual(declared + len(agent_system_prompt()), BUDGET_CHARS)


class TheAnchorsAreInBothPrompts(SimpleTestCase):
    def test_anchors(self):
        for name, text in (
            ("legacy", AGENT_SYSTEM_PROMPT),
            ("registry", AGENT_SYSTEM_PROMPT_REGISTRY),
        ):
            for anchor in (prompts._HELP_CHEAT_SHEET, prompts._HELP_STEP):
                # assertTrue, not assertIn: a failing assertIn would print the whole prompt.
                self.assertTrue(
                    anchor in text,
                    f"a how-do-I line moved in the {name} prompt: update prompts._HELP_* (MIP-2328) or the "
                    "flag's routing is silently NOT added",
                )
