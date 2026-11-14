"""MIP-1468: the legacy eval commands (`agent_eval`, `agent_eval_compare`). NO real model.

* `agent_eval_compare` refuses an override key that is not in `SEARCH_ENGINE` (a typo'd flag
  changed nothing and the diff said "no difference");
* the cost of an eval case is visible with `AI_COST_METER` OFF (its default): the runner falls
  back to the in-process usage capture, and both commands print tokens and cost;
* with the meter ON the ledger is still the source, and the two agree.
"""

from __future__ import annotations

import io
import json
from unittest import mock

from django.conf import settings
from django.core.management import call_command

from origin.search_engine import llm
from origin.search_engine.agent.evals import runner as eval_runner
from origin.search_engine.agent.evals.runner import CaseResult
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage, FunctionCall
from origin.search_engine.management.commands import agent_eval, agent_eval_compare
from origin.tests.test_base import BaseAPITestCase

MODEL = "gemini-3.5-flash-lite"


class ScriptedAdapter:
    """Call 1: list_tasks. Call 2: answer. Every call reports usage like an adapter does."""

    def __init__(self):
        self.calls = 0

    def generate_step(self, messages, tools, system_instruction, *, usage_sink=None, **kw):
        self.calls += 1
        n = self.calls
        sink = usage_sink if usage_sink is not None else CallUsage()
        sink.provider, sink.model = "gemini", kw.get("model_override") or MODEL
        sink.prompt_tokens, sink.cached_tokens, sink.output_tokens = 800, 200, 50

        def gen():
            try:
                if n % 2:
                    yield None, FunctionCall(name="list_tasks", args={})
                else:
                    yield "Nothing is open right now.", None
            finally:
                spend.record_llm_call(sink)

        return gen()


def result(case_id, *, passed=True, calls=0, tokens=0, usd_micro=0):
    return CaseResult(
        case_id=case_id,
        passed=passed,
        duration_ms=5,
        llm_calls=calls,
        total_tokens=tokens,
        cost_usd_micro=usd_micro,
    )


# --------------------------------------------------------------------------- #
# 1. Typo'd override keys                                                     #
# --------------------------------------------------------------------------- #


class OverrideKeyTests(BaseAPITestCase):
    def run_compare(self, overrides, *argv, runner=None):
        out, err = io.StringIO(), io.StringIO()
        ran = []

        def fake(case):
            ran.append(case["id"])
            return runner(case) if runner else result(case["id"])

        with (
            mock.patch.object(agent_eval_compare, "load_cases", return_value=[{"id": "c1"}]),
            mock.patch.object(agent_eval_compare, "run_retrieval_case", fake),
            mock.patch.object(agent_eval_compare, "run_behavior_case", fake),
        ):
            try:
                call_command(
                    "agent_eval_compare",
                    "--retrieval",
                    "--b-overrides",
                    json.dumps(overrides),
                    *argv,
                    stdout=out,
                    stderr=err,
                )
                code = None
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue(), ran

    def test_a_typo_is_refused_before_anything_runs_and_names_the_closest_key(self):
        code, _out, err, ran = self.run_compare({"RAG_USE_RERANKR": True})
        self.assertEqual(code, 2)
        self.assertEqual(ran, [])
        self.assertIn("unknown SEARCH_ENGINE key 'RAG_USE_RERANKR'", err)
        self.assertIn("did you mean RAG_USE_RERANKER", err)
        self.assertIn("Nothing was run", err)

    def test_one_bad_key_among_good_ones_still_refuses(self):
        code, _out, _err, ran = self.run_compare({"RAG_USE_RERANKER": False, "NOT_A_FLAG": 1})
        self.assertEqual(code, 2)
        self.assertEqual(ran, [])

    def test_a_real_key_runs_both_arms_and_restores_the_setting(self):
        before = settings.SEARCH_ENGINE["RAG_RERANK_OUTPUT_K"]
        code, _out, _err, ran = self.run_compare({"RAG_RERANK_OUTPUT_K": before + 1})
        self.assertIsNone(code)
        self.assertEqual(ran, ["c1", "c1"])
        self.assertEqual(settings.SEARCH_ENGINE["RAG_RERANK_OUTPUT_K"], before)

    def test_an_override_equal_to_the_current_value_is_noted(self):
        current = settings.SEARCH_ENGINE["RAG_RERANK_OUTPUT_K"]
        code, out, _err, _ran = self.run_compare({"RAG_RERANK_OUTPUT_K": current})
        self.assertIsNone(code)
        self.assertIn("RAG_RERANK_OUTPUT_K is already", out)
        self.assertIn("changes nothing between A and B", out)

    def test_every_key_the_bakeoff_workflow_documents_exists(self):
        for key in (
            "CLAUDE_MODEL",
            "LLM_PROVIDER",
            "GEMINI_MODEL",
            "RAG_USE_QUERY_REWRITE",
            "RAG_RERANK_OUTPUT_K",
        ):
            self.assertEqual(agent_eval_compare.unknown_override_keys({key: 1}), [], key)


# --------------------------------------------------------------------------- #
# 2. Cost with the meter off                                                  #
# --------------------------------------------------------------------------- #


class CompareCostReportTests(BaseAPITestCase):
    def run_compare(self, a_cost, b_cost):
        costs = iter([a_cost, b_cost])
        out = io.StringIO()

        def fake(case):
            calls, tokens, usd = next(costs)
            return result(case["id"], calls=calls, tokens=tokens, usd_micro=usd)

        with (
            mock.patch.object(agent_eval_compare, "load_cases", return_value=[{"id": "c1"}]),
            mock.patch.object(agent_eval_compare, "run_behavior_case", fake),
        ):
            call_command(
                "agent_eval_compare",
                "--b-overrides",
                json.dumps(
                    {"RAG_RERANK_OUTPUT_K": settings.SEARCH_ENGINE["RAG_RERANK_OUTPUT_K"] + 1}
                ),
                stdout=out,
                stderr=io.StringIO(),
            )
        return out.getvalue()

    def test_the_diff_shows_calls_tokens_and_cost_per_arm(self):
        out = self.run_compare((4, 10_000, 40_000), (5, 15_000, 60_000))
        self.assertIn("LLM calls: A=4  B=5", out)
        self.assertIn("Tokens: A=10,000  B=15,000  delta +5,000", out)
        self.assertIn("Cost: A=$0.0400  B=$0.0600  delta $+0.0200 (+50%)", out)
        # and each run line says what it cost
        self.assertIn("10.0k tok, $0.0400", out)

    def test_a_suite_with_no_llm_call_prints_no_cost_lines(self):
        out = self.run_compare((0, 0, 0), (0, 0, 0))
        self.assertNotIn("Cost:", out)
        self.assertNotIn("LLM calls:", out)


class CaseCostMeterOffTests(BaseAPITestCase):
    """The real `run_behavior_case` on a stub model."""

    def run_case(self):
        case = {
            "id": "cost1",
            "query": "what is open?",
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.id),
            "expect": {},
        }
        adapter = ScriptedAdapter()
        with mock.patch.object(llm, "_build_adapter", lambda provider: adapter):
            return eval_runner.run_behavior_case(case), adapter

    def test_the_meter_state_is_what_the_class_says(self):
        self.assertFalse(settings.SEARCH_ENGINE.get("AI_COST_METER", False))

    def test_a_case_reports_calls_tokens_and_cost_with_the_meter_off(self):
        res, adapter = self.run_case()
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(res.llm_calls, 2)
        # prompt 800 + cached 200 + output 50 per call
        self.assertEqual(res.total_tokens, 2 * (800 + 200 + 50))
        self.assertGreater(res.cost_usd_micro, 0)
        self.assertGreater(res.cost_jpy_milli, 0)

    def test_the_figures_do_not_leak_between_cases(self):
        first, _ = self.run_case()
        second, _ = self.run_case()
        self.assertEqual(second.llm_calls, first.llm_calls)
        self.assertEqual(second.total_tokens, first.total_tokens)
        self.assertIsNone(eval_runner._case_usage.get())

    def test_a_case_that_makes_no_call_costs_nothing(self):
        case = {"id": "free1", "query": "x", "team_id": "t", "user_id": "u", "expect": {}}
        with mock.patch.object(eval_runner, "run_agent", lambda *a, **k: None):
            res = eval_runner.run_behavior_case(case)
        self.assertEqual((res.llm_calls, res.total_tokens, res.cost_usd_micro), (0, 0, 0))

    def test_agent_eval_prints_the_suite_cost_with_the_meter_off(self):
        case = {
            "id": "cost1",
            "query": "what is open?",
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.id),
            "expect": {},
        }
        out = io.StringIO()
        with (
            mock.patch.object(agent_eval, "load_cases", return_value=[case]),
            mock.patch.object(llm, "_build_adapter", lambda provider: ScriptedAdapter()),
        ):
            try:
                call_command("agent_eval", stdout=out, stderr=io.StringIO())
            except SystemExit:  # the stub may not satisfy the case; the cost line is the point
                pass
        text = out.getvalue()
        self.assertIn("Suite cost: 2,100 tokens over 2 LLM call(s)", text)
        self.assertIn("2.1k tok", text)  # on the case's own line too


class CaseCostMeterOnTests(CaseCostMeterOffTests):
    """With the meter ON the ledger is the source and says the same."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {"AI_COST_METER": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_meter_state_is_what_the_class_says(self):
        self.assertTrue(settings.SEARCH_ENGINE["AI_COST_METER"])

    def test_the_ledger_and_the_capture_agree(self):
        from origin.search_engine.models import AiSpendEvent

        res, _ = self.run_case()
        rows = AiSpendEvent.objects.filter(surface="eval")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(res.llm_calls, 2)
        self.assertEqual(res.cost_usd_micro, sum(r.cost_usd_micro for r in rows))
