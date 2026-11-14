"""Every model-visible eval flag ON TOGETHER (the `all-on` arm of `agent_eval_group`), no model.

The stacks were built by different people, each proving its own flag on and off. Nothing else proves
they hold up when the paid session flips them all at once (`all-on`). This does, over the surfaces
the model reads: the tool declarations (descriptions + the in-app tool the timeline flag adds), the
three prompts, what each provider adapter sends, the prefix budget, and the resume/approval path.

`ALL_ON` is read from the arm files, so a flag added to `group/arms/*.yaml` is covered here with no
edit. The sizes are printed (`-v 2`) so the session's prefix cost can be quoted.
"""

from __future__ import annotations

import copy
import io
import json
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from apis import settings_test as test_settings
from origin.search_engine.agent import content_length as cl
from origin.search_engine.agent import tool_flag_params, tool_flag_text
from origin.search_engine.agent.controller import _app_tool, _build_tool_declarations
from origin.search_engine.agent.evals.group import harness
from origin.search_engine.agent.prompts import AGENT_SYSTEM_PROMPT, agent_system_prompt
from origin.search_engine.agent.tools import REGISTRY, ToolContext, flagged_tools, get_app_tool
from origin.tests.fixtures.sequence_scenario import SequenceScenario
from origin.tests.test_agent_content_length_pins import SCENARIOS, LoopBase, transcript
from origin.tests.test_agent_prefix_budget import BUDGET_CHARS
from origin.tests.test_eval_group_resume_runner import PROMPT_CHECKS, RunnerBase, cases

# All-on prefix measured 139,012 (off 134,315: +4,551 declarations, +146 prompt rule): same headroom
# the budget keeps. 140,982 with the parity batch's parameters (AGENT_PARITY_TOOLS, MIP-1561: +1,898
# characters over the three task tools), so 143,500 here: a Long member adds the 1,453-character
# content block on top (142,437) and the same ~1,000 characters of headroom stay.
# A Long member adds the content block (MIP-905, appended to the system instruction by the loop, not
# part of `agent_system_prompt()`); `test_the_content_block_...` below adds it on top of this number.
ALL_ON_BUDGET_CHARS = 143_500
ALL_ON = dict(harness.all_on_arm(harness.load_arms()).effective)
# Turned on by default by MIP-1089 (grouped eval 2026-11-14; the timeline tool the same day after the
# Japanese cases were re-read). Everything else in ALL_ON is held.
SHIPPED_ON = frozenset(
    {
        "AGENT_RESUME_KEEPS_BLOCKS",
        "AGENT_RESUME_KEEPS_TURNS",
        "AGENT_CLOSING_LINE_IN_ORIGIN_THREAD",
        "AGENT_REJECT_REASON",
        "AGENT_TELL_MODEL_UNDO",
        "AGENT_FETCH_TASK_ANCHORS",
        "AGENT_INBOX_REPORT_LABEL",
        "AGENT_TEAM_IN_CURRENT_USER",
        "AGENT_NOTE_THREADS_IN_GET_NOTE",
        "AGENT_CLOSED_ANCESTOR_RULE",
        "AGENT_INAPP_TIMELINE_TOOL",
        "AGENT_CONTENT_LENGTH",
    }
)
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

    def test_the_shipped_flags_are_on_by_default_and_the_held_ones_are_off(self):
        # MIP-1089 flipped ten flags on by the 2026-11-14 grouped eval. The others stay dark until
        # their own decision: content length (tune first) and the body on the user model (empty
        # bodies, MIP-1503). (The client name in task history was dropped instead, MIP-1089.)
        # The PRODUCTION defaults: `apis.settings` itself. The test settings keep the shipped flags
        # off (see `apis/settings_test.py`), so `django.conf.settings` would not tell.
        from apis import settings as production  # noqa: PLC0415

        for key in ALL_ON:
            want = key in SHIPPED_ON
            self.assertIs(
                bool(production.SEARCH_ENGINE.get(key)),
                want,
                f"{key} must default to {'on' if want else 'off'}",
            )
        self.assertEqual(
            set(SHIPPED_ON), set(getattr(test_settings, "AGENT_FLAGS_ON_IN_PRODUCTION"))
        )

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
            "AGENT_NOTE_THREADS_IN_GET_NOTE",
            "AGENT_CLOSED_ANCESTOR_RULE",
            "AGENT_INAPP_TIMELINE_TOOL",
            "AGENT_CONTENT_LENGTH",
            "AGENT_BODY_ON_USER_MODEL",
            "AGENT_PARITY_TOOLS",
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

    def test_schemas_only_gain_the_declared_parameters_and_descriptions_only_grow(self):
        # The parity flag (MIP-1561) is the one flag that adds PARAMETERS (`tool_flag_params`).
        # Take the declared ones back out of the all-on schema and it must be the all-off schema
        # exactly: every parameter a tool had is kept as it was. Every other tool's schema does
        # not move at all, and a description only grows (the one sentence a flag REPLACES is
        # swapped in place, `tool_flag_text.REPLACEMENTS`).
        added = {a.tool: a for a in tool_flag_params.ADDITIONS}
        replaced = {name: (old, new) for name, _flag, old, new in tool_flag_text.REPLACEMENTS}
        off, on = self._off_and_on()
        for before, after in zip(off, on, strict=False):
            addition = added.get(before.name)
            if addition is None:
                self.assertEqual(before.parameters_schema, after.parameters_schema, before.name)
            else:
                stripped = copy.deepcopy(after.parameters_schema)
                for name in addition.properties:
                    self.assertIn(name, stripped["properties"], before.name)
                    del stripped["properties"][name]
                for array_name, row_props in addition.nested:
                    rows = stripped["properties"][array_name]["items"]["properties"]
                    for name in row_props:
                        self.assertIn(name, rows, before.name)
                        del rows[name]
                self.assertEqual(before.parameters_schema, stripped, before.name)
            base = before.description
            if before.name in replaced:
                base = base.replace(*replaced[before.name], 1)
            self.assertTrue(after.description.startswith(base), before.name)

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
        # `test_agent_prefix_budget` counts the CONSTANT prompt, so AGENT_REJECT_REASON's extra rule
        # (+146 chars, in `agent_system_prompt()`) is not in its number; it is in this one.
        with settings_with(ALL_ON):
            on_prompt = len(agent_system_prompt())
        on_total = declared_chars(on) + on_prompt
        delta = declared_chars(on) - declared_chars(off)
        print(
            f"\nALL-ON declarations: off={declared_chars(off)} on={declared_chars(on)} "
            f"delta=+{delta} chars (~{delta / 4.2:.0f} tokens at 4.2 chars/token); "
            f"with the system prompt: off={off_total} on={on_total} budget={BUDGET_CHARS}"
        )
        self.assertGreater(delta, 0)
        # With EVERY flag on the prefix was over `test_agent_prefix_budget`'s BUDGET_CHARS until the
        # flips of 2026-11-14 raised it (140,000 with the timeline tool). Now even all-on fits it:
        # a flag turned on later must keep that true or raise the budget with its routing evidence.
        # `ALL_ON_BUDGET_CHARS` also stops all-on growing unnoticed.
        self.assertLessEqual(on_total, ALL_ON_BUDGET_CHARS)
        self.assertLessEqual(on_total, BUDGET_CHARS, "all-on no longer fits the production budget")

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
        for line in (
            "tools.neutral.full",
            "tools.gemini.full",
            "tools.claude.full",
            "tools.openai.full",
        ):
            self.assertIn(line, moved)
        # the reject-reason sentence is in every assembled prompt; the templates are not touched
        self.assertNotIn("prompt.thread_ask.template", moved)
        self.assertNotIn("prompt.note_ask.template", moved)
        # results move only for the tools a flag edits (closed-ancestor rows, the team line, note threads)
        self.assertEqual(
            {k for k in moved if k.startswith("result.")},
            {
                "result.list_tasks",
                "result.get_current_user",
                "result.fetch_note",
                "result.fetch_task",
                # the second fixture (MIP-1478): the inbox report, the focus list, the counts, the
                # milestone and sprint tools, the rows and the note's threads
                "result.b.fetch_note_threads",
                "result.b.fetch_task",
                "result.b.get_milestone_summary",
                "result.b.get_my_blockers",
                "result.b.get_my_focus_tasks",
                "result.b.get_my_schedule",
                "result.b.get_my_task_summary",
                "result.b.get_project_summary",
                "result.b.get_sprint_summary",
                "result.b.get_stale_tasks",
                "result.b.get_task_blockers",
                "result.b.get_team_task_summary",
                "result.b.get_workload_distribution",
                "result.b.list_milestones",
                "result.b.list_my_inbox",
                "result.b.list_my_milestones",
                "result.b.list_tasks",
            },
        )
        # and these do NOT move: the closed-ancestor rule does not reach them (MIP-1478: a decision is open for
        # each, see the PR). When one is covered its line moves, and moves from here to the set above.
        self.assertTrue(
            {
                "result.b.get_task_throughput_stats",
                "result.b.get_milestone_assignee_counts",
            }.isdisjoint(moved)
        )


# --------------------------------------------------------------------------- #
# Interactions: each stack's tools and resume path, every flag on at once        #
# --------------------------------------------------------------------------- #


class AllOnToolResultTests(SequenceScenario):
    """The sequence story's task (thread + chat + note origins, a client key) read by every tool a
    flag touches, with every flag on: each flag's addition is present and no tool breaks."""

    def setUp(self):
        super().setUp()
        patcher = settings_with(ALL_ON)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seq = self.build_sequence(client_on_project=True, c_in_gm=True)

    def ctx(self, user):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))

    def run_tool(self, name, user, **args):
        tool = get_app_tool(name)
        self.assertIsNotNone(tool, name)
        return tool.run(args, self.ctx(user))

    def test_fetch_task_carries_the_anchors_and_the_closed_ancestor_fields_together(self):
        out = self.run_tool("fetch_task", self.seq.b, task_id=self.seq.task.task_id)
        self.assertIn("anchors", out)
        keys = list(out)
        self.assertEqual(keys[-2:], ["anchors", "__summary__"])
        # the task's own status is still there; the rule's fields appear only when the rule changes it
        self.assertIn("status", out)

    def test_task_history_still_serves_with_the_client_flag_and_the_rule_on(self):
        out = self.run_tool("get_task_history", self.seq.b, task_id=self.seq.task.task_id)
        self.assertTrue(out.get("history") or out.get("entries") or out.get("events") or out)

    def test_current_user_carries_the_team_line(self):
        out = self.run_tool("get_current_user", self.seq.b)
        self.assertIn("team", out)

    def test_the_inbox_and_the_summaries_still_run(self):
        self.run_tool("list_my_inbox", self.seq.b, include_read=True, limit=20)
        self.run_tool("get_my_task_summary", self.seq.b)
        self.run_tool("get_my_focus_tasks", self.seq.b)
        self.run_tool("list_tasks", self.seq.b, limit=20)

    def test_the_timeline_tool_runs_through_the_controllers_lookup(self):
        tool = _app_tool("get_timeline")
        self.assertIsNotNone(tool)
        out = tool.run(
            {"anchor_type": "task", "anchor_id": str(self.seq.task.task_id)}, self.ctx(self.seq.b)
        )
        self.assertTrue(out)

    def test_a_flagged_tool_name_cannot_shadow_a_registered_one(self):
        # `_app_tool` consults REGISTRY first: no declared name resolves to two tools
        for d in _build_tool_declarations():
            self.assertIsNotNone(_app_tool(d.name), d.name)


class AllOnResumeTests(RunnerBase):
    """The resume runner's cases (977 / 991 / 889 reason + undo / 1283) with EVERY flag on at
    once: reject-reason + undo note + both resume flags + the closing line. Stub model."""

    def checks(self, result):
        return {
            c["name"]: c
            for c in result["checks"]
            if c["name"] in PROMPT_CHECKS + ("paused_on_the_write",)
        }

    def test_every_resume_case_pauses_and_meets_its_own_direction_with_all_on(self):
        problems: list[str] = []
        seen = 0
        for case in cases():
            seen += 1
            result = self.run_case(case, ALL_ON)
            if result.get("error"):
                problems.append(f"{case['id']}: crashed: {result['error']}")
                continue
            checks = self.checks(result)
            if case.get("scenario") != "closing_line" and not checks.get(
                "paused_on_the_write", {}
            ).get("ok"):
                problems.append(f"{case['id']}: the first leg did not pause")
                continue
            failed = [n for n, c in checks.items() if n in PROMPT_CHECKS and not c["ok"]]
            if failed:
                problems.append(f"{case['id']} ({case['polarity']}): failed {failed}")
        self.maxDiff = None
        self.assertEqual(problems, [])
        self.assertGreaterEqual(seen, 30)

    def test_the_settings_are_restored_after_an_all_on_run(self):
        before = dict(settings.SEARCH_ENGINE)
        self.run_case(cases()[0], ALL_ON)
        self.assertEqual(dict(settings.SEARCH_ENGINE), before)


class AllOnContentLengthTests(SimpleTestCase):
    """MIP-905 is the one flag whose text is per member: a block at the END of the system
    instruction for a Short or Long member, nothing for Standard."""

    def test_the_content_block_sizes_and_the_all_on_total_for_each_level(self):
        with settings_with({k: False for k in ALL_ON}):
            off_total = declared_chars(_build_tool_declarations()) + len(agent_system_prompt())
        with settings_with(ALL_ON):
            on_total = declared_chars(_build_tool_declarations()) + len(agent_system_prompt())
        sizes = {lv: len(cl.guidance_block(lv)) for lv in cl.LEVELS}
        print(
            f"\nALL-ON content block: {sizes}; prompt+declarations off={off_total} "
            f"all-on standard={on_total} short={on_total + 2 + sizes['short']} "
            f"long={on_total + 2 + sizes['long']} (~{(on_total + 2 + sizes['long'] - off_total) / 4.2:.0f} "
            f"tokens over all-off); all-on ceiling {ALL_ON_BUDGET_CHARS}"
        )
        self.assertEqual(sizes["standard"], 0)
        self.assertGreater(sizes["long"], 0)
        # even a Long member in the all-on arm stays inside the all-on ceiling
        self.assertLessEqual(on_total + 2 + sizes["long"], ALL_ON_BUDGET_CHARS)


class AllOnLoopTests(LoopBase):
    """`_drive_loop` against a recording client with EVERY flag on: the content block lands at the
    tail for a Long member, nothing moves for Standard, and the other flags' text is present."""

    def run_all_on(self, key, level):
        self.user.preferred_content_length = level
        self.user.save(update_fields=["preferred_content_length"])
        original = LoopBase.settings_with

        def with_all_on(inner, **flags):
            return original(inner, **{**flags, **ALL_ON})

        with mock.patch.object(LoopBase, "settings_with", with_all_on):
            return SCENARIOS[key](self)

    def test_long_and_short_end_with_the_block_standard_does_not(self):
        for key in ("plain", "plain-ja", "effort-medium/gemini"):
            standard = transcript(self.run_all_on(key, "standard").calls)
            for level in ("short", "long"):
                on = transcript(self.run_all_on(key, level).calls)
                self.assertEqual(len(on), len(standard), key)
                block = cl.guidance_block(level)
                for a, b in zip(standard, on, strict=True):
                    self.assertEqual(b["system"], f"{a['system']}\n\n{block}", (key, level))
                    self.assertEqual(b["tools"], a["tools"], (key, level))
                    self.assertEqual(b["messages"], a["messages"], (key, level))

    def test_the_other_flags_are_in_what_the_model_is_sent(self):
        calls = transcript(self.run_all_on("plain", "long").calls)
        system = calls[0]["system"]
        with settings_with({"AGENT_REJECT_REASON": True}):
            rule = agent_system_prompt()
        self.assertTrue(system.startswith(rule), "the all-on prompt is the reject-reason one")
        names = [t["name"] if isinstance(t, dict) else t for t in calls[0]["tools"]]
        self.assertIn("get_timeline", str(names))

    def test_the_two_tier_write_runs_with_every_flag_on(self):
        client = self.run_all_on("two-tier-write", "long")
        self.assertTrue(client.calls)
