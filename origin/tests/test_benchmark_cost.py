"""Benchmark cost measurement (PR-7).

Closes the eval suite's oldest blind spot. The runner calls the
controller with `run_id=None`, and `_persist_llm_call` early-returns on
that — so the 155-case suite has never recorded a single token. The
ledger keys on `request_id` rather than the run, which is precisely why
it can see what `AgentLlmCall` cannot.

The metric contract: TOKENS are primary, yen is secondary. Yen moves
when the rate card or FX pin moves, which would make a price change read
as an engineering regression — the exact confusion this metric exists to
detect.
"""

from __future__ import annotations

from io import StringIO

from django.test import TestCase

from origin.search_engine.agent.evals.runner import CaseResult, _case_cost
from origin.search_engine.llm import spend
from origin.search_engine.models import AiSpendEvent


class CaseCostTests(TestCase):
    def _row(self, request_id, **kw):
        kw.setdefault("surface", "eval")
        kw.setdefault("provider", "gemini")
        kw.setdefault("model", "gemini-3.6-flash")
        kw.setdefault("rate_card_version", "card-a")
        return AiSpendEvent.objects.create(request_id=request_id, **kw)

    def test_collects_tokens_calls_and_cost_for_the_active_case(self):
        with spend.spend_context(surface="eval") as ctx:
            self._row(ctx.request_id, prompt_tokens=1000, output_tokens=200, cost_jpy_milli=300)
            self._row(ctx.request_id, prompt_tokens=500, output_tokens=100, cost_jpy_milli=150)
            cost = _case_cost()

        self.assertEqual(cost["llm_calls"], 2)
        self.assertEqual(cost["total_tokens"], 1800)
        self.assertEqual(cost["cost_jpy_milli"], 450)
        self.assertEqual(cost["rate_card_version"], "card-a")

    def test_does_not_leak_another_cases_spend(self):
        other = "66666666-6666-6666-6666-666666666666"
        self._row(other, prompt_tokens=99_000, cost_jpy_milli=99_000)
        with spend.spend_context(surface="eval") as ctx:
            self._row(ctx.request_id, prompt_tokens=10, cost_jpy_milli=5)
            cost = _case_cost()
        self.assertEqual(cost["total_tokens"], 10)

    def test_zero_without_a_context(self):
        self.assertEqual(_case_cost()["llm_calls"], 0)

    def test_zero_when_the_meter_recorded_nothing(self):
        """Meter off is the default — the suite must behave exactly as
        it did before."""
        with spend.spend_context(surface="eval"):
            self.assertEqual(_case_cost()["total_tokens"], 0)

    def test_thought_tokens_are_counted(self):
        with spend.spend_context(surface="eval") as ctx:
            self._row(ctx.request_id, output_tokens=100, thought_tokens=400)
            self.assertEqual(_case_cost()["total_tokens"], 500)


class CostReportingTests(TestCase):
    def _print(self, results):
        from origin.search_engine.management.commands.agent_eval import Command

        cmd = Command()
        cmd.stdout = StringIO()
        cmd._print_cost(results)
        return cmd.stdout.getvalue()

    def test_tokens_are_the_headline_and_yen_is_parenthetical(self):
        """Yen moves with the rate card; tokens move only when the agent
        does. Leading with yen would make a price change look like a
        regression."""
        out = self._print(
            [
                CaseResult(
                    case_id="a", passed=True, duration_ms=1,
                    llm_calls=3, total_tokens=12_000, cost_jpy_milli=4500,
                    rate_card_version="card-a",
                )
            ]
        )
        self.assertIn("12,000 tokens", out)
        self.assertIn("(¥4.50)", out, "yen belongs in parentheses, not the headline")
        self.assertLess(out.index("tokens"), out.index("¥"))
        self.assertIn("card-a", out, "the trend must be segmentable by rate card")

    def test_reports_tokens_per_case(self):
        results = [
            CaseResult(case_id=str(i), passed=True, duration_ms=1, llm_calls=1, total_tokens=1000)
            for i in range(4)
        ]
        self.assertIn("1,000 tok/case", self._print(results))

    def test_silent_when_the_meter_is_off(self):
        out = self._print([CaseResult(case_id="a", passed=True, duration_ms=1)])
        self.assertEqual(out, "", "no meter, no line — the suite reads exactly as before")

    def test_a_run_spanning_two_rate_cards_is_flagged(self):
        out = self._print(
            [
                CaseResult(case_id="a", passed=True, duration_ms=1, llm_calls=1,
                           total_tokens=10, rate_card_version="card-a"),
                CaseResult(case_id="b", passed=True, duration_ms=1, llm_calls=1,
                           total_tokens=10, rate_card_version="card-b"),
            ]
        )
        self.assertIn("2 rate cards", out)
        self.assertIn("compare TOKENS", out, "say which number is still trustworthy")


class RunnerBindsAContextTests(TestCase):
    def test_run_behavior_case_binds_one_request_per_case(self):
        """Every call a case makes — loop steps, rewrite, rerank,
        critique — must roll up under ONE request_id."""
        from origin.search_engine.agent.evals import runner

        seen: list[str | None] = []
        original = runner._resolve_fixture

        def _spy(case):
            # Runs at the very top of run_behavior_case, i.e. inside the
            # decorator's bound context.
            ctx = spend.current_context()
            seen.append(ctx.request_id if ctx else None)
            return original(case)

        runner._resolve_fixture = _spy
        try:
            runner.run_behavior_case({"id": "x"})
            runner.run_behavior_case({"id": "y"})
        finally:
            runner._resolve_fixture = original

        self.assertEqual(len(seen), 2)
        self.assertTrue(all(seen), "each case must run inside a bound spend context")
        self.assertNotEqual(
            seen[0], seen[1], "each case needs its OWN request id, not a shared one"
        )

    def test_context_is_released_after_a_case(self):
        from origin.search_engine.agent.evals import runner

        runner.run_behavior_case({"id": "x"})
        self.assertIsNone(
            spend.current_context(),
            "a leaked context would attribute the NEXT case's spend to this one",
        )
