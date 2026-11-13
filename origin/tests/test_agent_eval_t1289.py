"""The grouped-eval pieces of MIP-1289 (arm `t1289`, case files `t1289.yaml` and `t1281.yaml`).

No model runs: a stub client picks the first tool of a case's `ok` list when that tool is declared
and answers in text when it is not. That proves what the paid session needs proven first:

  * the arm file is valid and its override is the flag the tool is gated on;
  * under the arm the routing cases run and the positives CAN pass (the tool is declared); without
    the arm they cannot (and the plan skips them, `requires_arm`);
  * the negatives name tools that exist and run on arm0 and on the arm (the baseline);
  * the legacy routing cases (`routing_cases.yaml`, both languages) run with the flag on: every one
    gets the surface with `get_timeline` in it, and the intents whose right answer is one of the
    three tools the new one overlaps are named, so a drop there is read as "may shift" and not as a
    regression of something else.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

import yaml
from django.test import SimpleTestCase

from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.evals import routing
from origin.search_engine.agent.evals.group import case_runner, harness
from origin.search_engine.agent.tools import REGISTRY, get_app_tool
from origin.search_engine.llm.types import CallUsage, FunctionCall

FLAG = "AGENT_INAPP_TIMELINE_TOOL"
# The legacy intents whose right first step is one of the three tools `get_timeline` overlaps.
OVERLAP = {"get_task_history", "fetch_chat_thread", "fetch_note_history"}
LEGACY_THAT_MAY_SHIFT = {
    "task_history",
    "task_body_restore",
    "chat_read",
    "note_history",
    "note_restore",
}


class StubClient:
    """Picks the first right tool of the case when it is declared; else answers in text."""

    def __init__(self, ok_by_query: dict[str, list[str]]):
        self.ok_by_query = ok_by_query
        self.declared: list[list[str]] = []

    def generate_step(self, messages, tools, system_instruction, *, usage_sink=None, **kw):
        names = [t.name for t in tools]
        self.declared.append(names)
        query = messages[-1].text
        if usage_sink is not None:
            usage_sink.provider, usage_sink.model = "gemini", "gemini-3.5-flash-lite"
            usage_sink.prompt_tokens, usage_sink.output_tokens = 100, 10
        wanted = [n for n in self.ok_by_query.get(query, []) if n in names]
        if wanted:
            yield None, FunctionCall(name=wanted[0], args={})
        else:
            yield "I would answer directly.", None


def group_cases(tag: str):
    return [c for c in harness.load_cases(include_legacy=False) if tag in c.tags]


class T1289CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = group_cases("t1289")

    def test_at_least_twelve_routing_cases_in_both_languages(self):
        self.assertGreaterEqual(len(self.cases), 12)
        for lang in ("en", "ja"):
            mine = [c for c in self.cases if c.lang == lang]
            self.assertGreaterEqual(len(mine), 12, lang)
        self.assertEqual({c.kind for c in self.cases}, {"routing"})

    def test_every_english_case_has_a_japanese_twin_asked_in_japanese(self):
        slugs = {
            lang: {c.id.split(f"-{lang}-", 1)[1] for c in self.cases if c.lang == lang}
            for lang in ("en", "ja")
        }
        self.assertEqual(slugs["en"], slugs["ja"])
        for c in self.cases:
            has_ja = bool(re.search(r"[぀-ヿ㐀-鿿]", c.raw["query"]))
            self.assertEqual(has_ja, c.lang == "ja", c.id)

    def test_there_are_positives_and_the_negatives_the_brief_names(self):
        positives = [c for c in self.cases if "get_timeline" in c.raw["ok"]]
        negatives = [c for c in self.cases if "get_timeline" not in c.raw["ok"]]
        self.assertGreaterEqual(len(positives), 6)
        self.assertGreaterEqual(len(negatives), 12)
        slugs = {c.id.split("-", 2)[2] for c in negatives}
        for needle in (
            "my-day",
            "find-task",
            "who-on-project",
            "who-changed-field",
            "read-chat-page",
            "note-versions",
        ):
            self.assertIn(needle, slugs)

    def test_positives_wait_for_the_arm_and_negatives_run_on_arm0_too(self):
        for c in self.cases:
            if c.raw["ok"] == ["get_timeline"] or "get_timeline" in c.raw["ok"]:
                self.assertEqual(c.requires_arm, "t1289", c.id)
            else:
                self.assertIsNone(c.requires_arm, c.id)

    def test_every_tool_a_case_names_exists(self):
        for c in self.cases:
            for name in c.raw["ok"]:
                self.assertTrue(name in REGISTRY or name == "get_timeline", f"{c.id}: {name}")

    def test_the_t1281_cases_name_tools_the_arm_declares(self):
        with mock.patch.dict("django.conf.settings.SEARCH_ENGINE", {FLAG: True}):
            declared = {d.name for d in _build_tool_declarations()}
        for c in group_cases("t1281"):
            expect = c.raw["expect"]
            for key in ("tools_used_contains", "tools_used_contains_any", "tools_used_excludes"):
                for name in expect.get(key, []):
                    self.assertIn(name, declared, f"{c.id}: {name}")
        fixed = [c for c in group_cases("t1281") if c.id.endswith("client-v1-fixed")]
        self.assertEqual(len(fixed), 2)
        for c in fixed:
            self.assertIn("get_timeline", c.raw["expect"]["tools_used_contains_any"])


class T1289ArmTests(SimpleTestCase):
    def test_the_arm_overrides_exactly_the_flag_the_tool_is_gated_on(self):
        arms = harness.load_arms()
        arm = arms["t1289"]
        self.assertEqual(arm.effective, {FLAG: True})
        self.assertEqual(arm.requires, [])
        self.assertEqual(harness.validate_overrides(arm), [])

    def test_the_all_on_arm_carries_it(self):
        self.assertIs(harness.all_on_arm(harness.load_arms()).effective[FLAG], True)

    def test_the_plan_runs_negatives_on_both_arms_and_positives_only_on_the_arm(self):
        cases = harness.load_cases(include_legacy=False)
        plan = harness.build_plan(cases, harness.load_arms(), arm_names=["t1289"])
        ran = {(i.arm.name, i.case.id) for i in plan.items if not i.skip}
        self.assertIn(("arm0", "t1289-en-my-day"), ran)
        self.assertIn(("t1289", "t1289-en-my-day"), ran)
        self.assertIn(("t1289", "t1289-en-task-story"), ran)
        self.assertNotIn(("arm0", "t1289-en-task-story"), ran)
        self.assertIn(("t1289", "t1281-en-what-happened"), ran)
        self.assertNotIn(("arm0", "t1281-en-what-happened"), ran)


class T1289RoutingRunsTests(SimpleTestCase):
    def run_cases(self, overrides):
        cases = group_cases("t1289")
        client = StubClient({c.raw["query"]: list(c.raw["ok"]) for c in cases})
        results = {}
        runner = case_runner.DefaultCaseRunner()
        with (
            mock.patch("origin.search_engine.llm.get_model_client", return_value=client),
            harness.apply_overrides(overrides),
        ):
            for case in cases:
                results[case.id] = runner(case, "arm", overrides)
        return results, client

    def test_with_the_arm_every_case_runs_and_passes_the_positives_can_pass(self):
        results, client = self.run_cases({FLAG: True})
        self.assertTrue(
            all(r["passed"] for r in results.values()),
            {k: v["tools"] for k, v in results.items() if not v["passed"]},
        )
        self.assertTrue(all("get_timeline" in names for names in client.declared))
        picked = {k for k, r in results.items() if r["tools"] == ["get_timeline"]}
        self.assertGreaterEqual(len(picked), 8)

    def test_without_the_arm_the_positives_cannot_pass_and_the_negatives_do(self):
        results, client = self.run_cases({FLAG: False})
        self.assertTrue(all("get_timeline" not in names for names in client.declared))
        for case in group_cases("t1289"):
            if case.raw["ok"] == ["get_timeline"]:
                self.assertEqual(results[case.id]["tools"], [], case.id)
                self.assertFalse(results[case.id]["passed"], case.id)
            elif case.requires_arm:
                # the ambiguous one: the old tool is also right, so it can pass without the arm
                self.assertTrue(results[case.id]["passed"], case.id)
            else:
                self.assertTrue(results[case.id]["passed"], (case.id, results[case.id]["tools"]))

    def test_the_arm_overrides_are_restored(self):
        from django.conf import settings  # noqa: PLC0415

        before = settings.SEARCH_ENGINE[FLAG]
        self.run_cases({FLAG: True})
        self.assertEqual(settings.SEARCH_ENGINE[FLAG], before)
        self.assertIsNone(get_app_tool("get_timeline") if not before else None)


class LegacyRoutingWithTheFlagOnTests(SimpleTestCase):
    """The 126 intents x 2 languages of `routing_cases.yaml` run on the surface with the tool in it
    (`agent_eval_routing` with the env var set does exactly this against a real model)."""

    def test_every_legacy_case_runs_and_sees_the_new_tool_when_the_flag_is_on(self):
        cases = routing.load_cases()
        self.assertEqual(len(cases), 252)
        seen: list[list[str]] = []

        class Client:
            def generate_step(self, messages, tools, system_instruction, *, usage_sink=None, **kw):
                seen.append([t.name for t in tools])
                yield "answer", None

        with mock.patch.dict("django.conf.settings.SEARCH_ENGINE", {FLAG: True}):
            declarations = _build_tool_declarations()
            for case in cases:
                routing.route_once(
                    case,
                    arm="current",
                    declarations=declarations,
                    system_instruction="s",
                    model="m",
                    client=Client(),
                )
        self.assertEqual(len(seen), 252)
        self.assertTrue(all(names[-1] == "get_timeline" for names in seen))

    def test_the_legacy_intents_a_new_tool_may_take_from_are_known(self):
        path = Path(routing.__file__).with_name("routing_cases.yaml")
        items = yaml.safe_load(path.read_text(encoding="utf-8"))
        overlapping = {i["id"] for i in items if OVERLAP & set(i.get("ok") or [])}
        self.assertEqual(overlapping, LEGACY_THAT_MAY_SHIFT)
        # and none of them lists the new tool: a model that picks it there is a miss to read, not a pass
        for i in items:
            self.assertNotIn("get_timeline", i.get("ok") or [])

    def test_no_legacy_case_is_scored_differently_by_the_flag_alone(self):
        """`score` only reads the first tools; the surface is the one variable."""
        case = routing.RoutingCase(id="x", lang="en", query="q", ok=frozenset({"get_task_history"}))
        self.assertTrue(routing.score(case, ["get_task_history"]))
        self.assertFalse(routing.score(case, ["get_timeline"]))
        self.assertIsInstance(CallUsage(), CallUsage)
