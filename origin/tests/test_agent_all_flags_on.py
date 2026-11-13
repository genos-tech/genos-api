"""Every model-visible eval flag ON TOGETHER (the `all-on` arm of `agent_eval_group`), no model.

The stacks were built by different people, each proving its own flag on and off. Nothing else proves
they hold up when the paid session flips them all at once (`all-on`). This does, over the surfaces
the model reads: the tool declarations (descriptions + the in-app tool the timeline flag adds), the
three prompts, what each provider adapter sends, the prefix budget, and the resume/approval path.

`ALL_ON` is read from the arm files, so a flag added to `group/arms/*.yaml` is covered here with no
edit. The sizes are printed (`-v 2`) so the session's prefix cost can be quoted.
"""

from __future__ import annotations

import io
import json
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from origin.search_engine.agent import tool_flag_text
from origin.search_engine.agent.controller import _app_tool, _build_tool_declarations
from origin.search_engine.agent.evals.group import harness
from origin.search_engine.agent.prompts import AGENT_SYSTEM_PROMPT, agent_system_prompt
from origin.search_engine.agent.tools import REGISTRY, flagged_tools
from origin.tests.test_agent_prefix_budget import BUDGET_CHARS

# All-on prefix measured 139,157 (off 134,315, +4,842 declarations): same headroom the budget keeps.
ALL_ON_BUDGET_CHARS = 141_000
ALL_ON = dict(harness.all_on_arm(harness.load_arms()).effective)
# The neighbours that change what is declared and are not flags under test.
QUIET = {"AGENT_DISABLED_TOOLS": frozenset(), "AGENT_CHEATSHEET_FROM_REGISTRY": False}


def settings_with(overrides):
    return mock.patch.dict(settings.SEARCH_ENGINE, {**QUIET, **overrides})


def declared_chars(decls) -> int:
    """`test_agent_prefix_budget`'s formula."""
    return sum(
        len(d.name) + len(d.description) + len(json.dumps(d.parameters_schema, ensure_ascii=False))
        for d in decls
    )


class AllOnArmTests(SimpleTestCase):
    def test_the_all_on_arm_is_the_union_of_the_arm_files_without_a_conflict(self):
        # `all_on_arm` raises when two arms set one key to two values.
        self.assertTrue(ALL_ON)
        for key, value in ALL_ON.items():
            self.assertIn(key, settings.SEARCH_ENGINE, key)
            self.assertIs(value, True, key)

    def test_every_flag_is_off_by_default_on_this_stack(self):
        for key in ALL_ON:
            self.assertFalse(settings.SEARCH_ENGINE.get(key), f"{key} must default to off")

    def test_the_flags_the_stacks_added_are_all_in_the_union(self):
        expected = {
            "AGENT_RESUME_KEEPS_BLOCKS",
            "AGENT_RESUME_KEEPS_TURNS",
            "AGENT_CLOSING_LINE_IN_ORIGIN_THREAD",
            "AGENT_REJECT_REASON",
            "AGENT_TELL_MODEL_UNDO",
            "AGENT_FETCH_TASK_ANCHORS",
            "AGENT_INBOX_REPORT_LABEL",
            "AGENT_TEAM_IN_CURRENT_USER",
            "AGENT_CLIENT_IN_TASK_HISTORY",
            "AGENT_NOTE_THREADS_IN_GET_NOTE",
            "AGENT_CLOSED_ANCESTOR_RULE",
            "AGENT_INAPP_TIMELINE_TOOL",
        }
        self.assertLessEqual(expected, set(ALL_ON))


class AllOnDeclarationTests(SimpleTestCase):
    def _off_and_on(self):
        with settings_with({k: False for k in ALL_ON}):
            off = _build_tool_declarations()
        with settings_with(ALL_ON):
            on = _build_tool_declarations()
        return off, on

    def test_same_tools_in_the_same_order_plus_the_flagged_ones_at_the_end(self):
        off, on = self._off_and_on()
        self.assertEqual([d.name for d in on[: len(off)]], [d.name for d in off])
        extra = [d.name for d in on[len(off) :]]
        self.assertEqual(extra, ["get_timeline"])
        self.assertEqual(len({d.name for d in on}), len(on), "a tool is declared twice")

    def test_schemas_do_not_move_only_descriptions_grow(self):
        off, on = self._off_and_on()
        for before, after in zip(off, on, strict=False):
            self.assertEqual(before.parameters_schema, after.parameters_schema, before.name)
            self.assertTrue(after.description.startswith(before.description), before.name)

    def test_each_flag_addition_is_in_its_tool_and_in_the_declared_order(self):
        _, on = self._off_and_on()
        by_name = {d.name: d.description for d in on}
        for name, flag, addition in tool_flag_text.ADDITIONS:
            self.assertIn(flag, ALL_ON)
            self.assertIn(addition, by_name[name], f"{name} lost the {flag} sentence")
        # two flags on one tool: the sentences follow ADDITIONS order, nothing duplicated
        text = by_name["fetch_task"]
        anchors = tool_flag_text.ADDITIONS[0][2]
        rows = [a for n, f, a in tool_flag_text.ADDITIONS if n == "fetch_task"][1]
        self.assertEqual(text.count(anchors), 1)
        self.assertEqual(text.count(rows), 1)
        self.assertLess(text.index(anchors), text.index(rows))

    def test_the_flagged_tool_is_runnable_and_found_by_the_loop_only_with_its_flag(self):
        with settings_with({k: False for k in ALL_ON}):
            self.assertIsNone(_app_tool("get_timeline"))
            self.assertEqual(flagged_tools(), [])
        with settings_with(ALL_ON):
            self.assertIsNotNone(_app_tool("get_timeline"))
            self.assertEqual([t.name for t in flagged_tools()], ["get_timeline"])
        self.assertNotIn("get_timeline", REGISTRY)

    def test_the_operator_kill_switch_still_hides_the_flagged_tool_with_all_on(self):
        with settings_with({**ALL_ON, "AGENT_DISABLED_TOOLS": frozenset({"get_timeline"})}):
            names = [d.name for d in _build_tool_declarations()]
        self.assertNotIn("get_timeline", names)

    def test_the_all_on_prefix_is_measured_and_held(self):
        off, on = self._off_and_on()
        off_total = declared_chars(off) + len(AGENT_SYSTEM_PROMPT)
        on_total = declared_chars(on) + len(AGENT_SYSTEM_PROMPT)
        delta = declared_chars(on) - declared_chars(off)
        print(
            f"\nALL-ON declarations: off={declared_chars(off)} on={declared_chars(on)} "
            f"delta=+{delta} chars (~{delta / 4.2:.0f} tokens at 4.2 chars/token); "
            f"with the system prompt: off={off_total} on={on_total} budget={BUDGET_CHARS}"
        )
        self.assertGreater(delta, 0)
        # FINDING: with EVERY flag on the prefix is over `test_agent_prefix_budget`'s BUDGET_CHARS
        # (the budget is the production number; each flag alone fits). The flip PR that turns several
        # on must raise it with its routing evidence. This pin only stops all-on growing unnoticed.
        self.assertLessEqual(on_total, ALL_ON_BUDGET_CHARS)
        self.assertGreater(on_total, BUDGET_CHARS, "all-on now fits the production budget: tighten this")

    def test_only_the_reject_reason_flag_edits_the_system_prompt(self):
        # AGENT_REJECT_REASON adds the "a user_rejected result may carry a reason" lines; every other
        # flag edits declarations or what a resume carries. So all-on == reject-reason-only != off.
        # (The per-level and locale prompts are in the fingerprint test below.)
        with settings_with({k: False for k in ALL_ON}):
            off = agent_system_prompt()
        with settings_with({k: False for k in ALL_ON} | {"AGENT_REJECT_REASON": True}):
            reason_only = agent_system_prompt()
        with settings_with(ALL_ON):
            on = agent_system_prompt()
        self.assertNotEqual(off, on)
        self.assertEqual(reason_only, on)


class AllOnProviderTests(SimpleTestCase):
    def test_every_provider_carries_the_whole_declaration_set(self):
        from google.genai import types  # noqa: PLC0415

        from origin.search_engine.llm.gemini_client import _tools_to_sdk  # noqa: PLC0415
        from origin.tests.test_inapp_timeline_tool import _request_kwargs  # noqa: PLC0415

        with settings_with(ALL_ON):
            decls = _build_tool_declarations()
        sdk = _tools_to_sdk(decls, types)
        self.assertEqual(
            [f.name for t in sdk for f in (t.function_declarations or [])],
            [d.name for d in decls],
        )
        for client_cls, path in (
            ("ClaudeClient", "origin.search_engine.llm.claude_client"),
            ("OpenAIClient", "origin.search_engine.llm.openai_client"),
        ):
            tools = _request_kwargs(client_cls, path, decls)["tools"]
            names = [t.get("name") or t["function"]["name"] for t in tools]
            self.assertEqual(names, [d.name for d in decls], client_cls)
            self.assertEqual(names[-1], "get_timeline")


class AllOnFingerprintTests(TestCase):
    """The fingerprint is the off-path proof; with everything on it must MOVE where it should."""

    def _run(self):
        out = io.StringIO()
        call_command("agent_fingerprint", stdout=out)
        return {ln.split()[0]: ln.split()[1] for ln in out.getvalue().strip().splitlines()}

    def test_the_tool_lines_and_the_reject_reason_prompt_lines_move(self):
        with settings_with({k: False for k in ALL_ON}):
            off = self._run()
        with settings_with(ALL_ON):
            on = self._run()
        moved = {k for k in off if off[k] != on[k]}
        for line in ("tools.neutral.full", "tools.gemini.full", "tools.claude.full", "tools.openai.full"):
            self.assertIn(line, moved)
        # the reject-reason sentence is in every assembled prompt; the templates are not touched
        self.assertNotIn("prompt.thread_ask.template", moved)
        self.assertNotIn("prompt.note_ask.template", moved)
        # results move only for the tools a flag edits (closed-ancestor rows, the team line, note threads)
        self.assertEqual(
            {k for k in moved if k.startswith("result.")},
            {"result.list_tasks", "result.get_current_user", "result.fetch_note", "result.fetch_task"},
        )
