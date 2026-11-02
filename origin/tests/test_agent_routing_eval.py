"""The routing eval's own rules (MIP-881). No LLM: the client here is a fake."""

from __future__ import annotations

from collections import Counter
from typing import Any
from unittest import mock

from django.test import SimpleTestCase

from origin.search_engine.agent.evals import routing
from origin.search_engine.agent.evals.routing import Outcome, RoutingCase
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.llm.types import FunctionCall

CASE = RoutingCase(id="c", lang="en", query="q", ok=frozenset({"list_tasks", "fetch_task"}))
NO_TOOL = RoutingCase(id="n", lang="en", query="hello", no_tool=True)


class FakeClient:
    """Plays one scripted turn per call: (text, calls), or an exception."""

    def __init__(self, *turns: Any) -> None:
        self.turns = list(turns)
        self.calls = 0

    def generate_step(self, **kwargs: Any) -> Any:
        self.calls += 1
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        text, calls = turn
        sink = kwargs["usage_sink"]
        sink.prompt_tokens = 10
        sink.cached_tokens = 90
        if text:
            yield text, None
        for name, args in calls:
            yield None, FunctionCall(name=name, args=args)


def route(client: FakeClient, case: RoutingCase = CASE, **kwargs: Any) -> Outcome:
    return routing.route_once(
        case,
        arm="a",
        declarations=[],
        system_instruction="s",
        model="m",
        client=client,
        **kwargs,
    )


class CaseFileTests(SimpleTestCase):
    def setUp(self) -> None:
        self.cases = routing.load_cases()

    def test_every_registered_tool_is_reachable_from_some_case(self) -> None:
        # A tool with no case is one the eval can't see being dropped, which
        # is how subsetting's failures stayed invisible.
        named = set().union(*(c.ok for c in self.cases))
        self.assertEqual(sorted(set(REGISTRY) - named), [])

    def test_gold_names_only_registered_tools(self) -> None:
        named = set().union(*(c.ok for c in self.cases))
        self.assertEqual(sorted(named - set(REGISTRY)), [])

    def test_every_intent_is_asked_in_every_language(self) -> None:
        per_intent = Counter(c.id for c in self.cases)
        self.assertEqual(set(per_intent.values()), {len(routing.LANGUAGES)})
        for case in self.cases:
            self.assertTrue(case.query.strip(), f"{case.id} [{case.lang}] has no prompt")

    def test_a_case_has_gold_or_says_no_tool_is_right(self) -> None:
        for case in self.cases:
            self.assertNotEqual(bool(case.ok), case.no_tool, case.id)

    def test_loading_one_language_gives_only_that_language(self) -> None:
        self.assertEqual({c.lang for c in routing.load_cases(languages=("ja",))}, {"ja"})


class ScoreTests(SimpleTestCase):
    def test_a_right_tool_passes(self) -> None:
        self.assertTrue(routing.score(CASE, ["list_tasks"]))

    def test_a_wrong_tool_fails(self) -> None:
        self.assertFalse(routing.score(CASE, ["get_team_members"]))

    def test_no_call_fails_a_case_that_needs_a_tool(self) -> None:
        self.assertFalse(routing.score(CASE, []))

    def test_one_right_call_among_parallel_calls_passes(self) -> None:
        self.assertTrue(routing.score(CASE, ["get_team_members", "fetch_task"]))

    def test_a_no_tool_case_passes_only_without_a_call(self) -> None:
        self.assertTrue(routing.score(NO_TOOL, []))
        self.assertFalse(routing.score(NO_TOOL, ["list_tasks"]))


class SystemInstructionTests(SimpleTestCase):
    def test_english_has_the_date_line_and_no_directive(self) -> None:
        text = routing.system_instruction_for("en", today_line="DATE", base="BASE")
        self.assertEqual(text, "BASE\n\nDATE")

    def test_japanese_adds_the_language_directive_last(self) -> None:
        text = routing.system_instruction_for("ja", today_line="DATE", base="BASE")
        self.assertTrue(text.startswith("BASE\n\nDATE\n\n"))
        self.assertGreater(len(text), len("BASE\n\nDATE\n\n"))


class RouteOnceTests(SimpleTestCase):
    def test_reads_the_first_turn_and_its_usage(self) -> None:
        outcome = route(FakeClient(("", [("list_tasks", {})])))
        self.assertEqual(outcome.tools, ["list_tasks"])
        self.assertTrue(outcome.passed)
        self.assertFalse(outcome.answered_in_text)
        self.assertEqual((outcome.prompt_tokens, outcome.cached_tokens), (10, 90))

    def test_text_with_no_call_is_recorded_as_answered_in_text(self) -> None:
        outcome = route(FakeClient(("Sure.", [])))
        self.assertTrue(outcome.answered_in_text)
        self.assertFalse(outcome.passed)

    def test_translate_scores_a_merged_tool_as_the_tool_behind_it(self) -> None:
        client = FakeClient(("", [("stats", {"metric": "tasks"})]))
        outcome = route(client, translate=lambda call: "list_tasks")
        self.assertEqual(outcome.tools, ["list_tasks"])
        self.assertTrue(outcome.passed)

    @mock.patch.object(routing.time, "sleep")
    def test_a_provider_failure_is_retried_and_the_error_cleared(self, _sleep: Any) -> None:
        client = FakeClient(RuntimeError("429"), ("", [("fetch_task", {})]))
        outcome = route(client)
        self.assertEqual(client.calls, 2)
        self.assertEqual(outcome.error, "")
        self.assertTrue(outcome.passed)

    @mock.patch.object(routing.time, "sleep")
    def test_persistent_failure_is_recorded_not_raised(self, _sleep: Any) -> None:
        client = FakeClient(RuntimeError("a"), RuntimeError("b"), RuntimeError("c"))
        outcome = route(client)
        self.assertEqual(client.calls, 3)
        self.assertIn("RuntimeError", outcome.error)
        self.assertFalse(outcome.passed)


class SummarizeTests(SimpleTestCase):
    def test_an_errored_call_stays_out_of_the_rate(self) -> None:
        summary = routing.summarize(
            [
                Outcome("a", "en", "x", tools=["t"], passed=True, latency_ms=100),
                Outcome("b", "en", "x", error="boom"),
            ]
        )
        self.assertEqual(summary["calls"], 2)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["pass_rate"], 1.0)

    def test_rates_are_split_by_language(self) -> None:
        summary = routing.summarize(
            [
                Outcome("a", "en", "x", passed=True),
                Outcome("a", "ja", "x", passed=False),
            ]
        )
        self.assertEqual(summary["pass_rate_by_lang"], {"en": 1.0, "ja": 0.0})

    def test_misses_group_by_case_language_and_what_was_called(self) -> None:
        summary = routing.summarize(
            [
                Outcome("a", "ja", "x", tools=["t"]),
                Outcome("a", "ja", "x", tools=["t"]),
                Outcome("b", "en", "x"),
            ]
        )
        self.assertEqual(
            summary["misses"],
            [
                {"case": "a", "lang": "ja", "called": "t", "count": 2},
                {"case": "b", "lang": "en", "called": "(answered without a tool)", "count": 1},
            ],
        )

    def test_nothing_scored_does_not_crash(self) -> None:
        summary = routing.summarize([Outcome("a", "en", "x", error="boom")])
        self.assertEqual(summary["pass_rate"], 0.0)
        self.assertEqual(summary["input_tokens_mean"], 0)
