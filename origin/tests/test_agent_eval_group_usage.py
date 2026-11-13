"""`evals/group/usage.py`: capture of tokens and cost from the production LLM-call seam.

No model is called: the stub below does what every adapter does at the end of a call,
`spend.record_llm_call(CallUsage)`.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from django.test import SimpleTestCase

from origin.search_engine.agent.evals.group import usage as group_usage
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage

# gemini-3.5-flash-lite: input 0.30, output 2.50, cached 0.03 USD per 1M (apis/llm_models.yaml).
MODEL = "gemini-3.5-flash-lite"


def stub_call(
    *,
    prompt=1000,
    cached=0,
    output=100,
    thought=0,
    tool_prompt=0,
    cache_write=0,
    model=MODEL,
):
    """One 'model call': exactly what an adapter's `finally` does."""
    sink = CallUsage(
        provider="gemini",
        model=model,
        prompt_tokens=prompt,
        cached_tokens=cached,
        output_tokens=output,
        thought_tokens=thought,
        tool_prompt_tokens=tool_prompt,
        cache_write_tokens=cache_write,
    )
    spend.record_llm_call(sink)


class CaptureTests(SimpleTestCase):
    def test_tokens_and_cost_come_from_catalog_prices(self):
        with group_usage.capture_usage() as cap:
            stub_call(prompt=1_000_000, cached=0, output=1_000_000)
        usage = cap.as_dict()
        self.assertEqual(usage["calls"], 1)
        self.assertEqual(usage["input_tokens"], 1_000_000)
        self.assertEqual(usage["output_tokens"], 1_000_000)
        self.assertAlmostEqual(usage["cost_usd"], 0.30 + 2.50, places=6)

    def test_cached_tokens_are_billed_at_the_cached_rate_and_counted_in_input(self):
        with group_usage.capture_usage() as cap:
            stub_call(prompt=1_000_000, cached=1_000_000, output=0)
        usage = cap.as_dict()
        self.assertEqual(usage["input_tokens"], 2_000_000)
        self.assertEqual(usage["cached_tokens"], 1_000_000)
        self.assertAlmostEqual(usage["cost_usd"], 0.30 + 0.03, places=6)

    def test_thinking_is_output_and_tool_declarations_are_input(self):
        with group_usage.capture_usage() as cap:
            stub_call(prompt=0, output=500, thought=500, tool_prompt=2000)
        usage = cap.as_dict()
        self.assertEqual(usage["output_tokens"], 1000)
        self.assertEqual(usage["input_tokens"], 2000)
        self.assertAlmostEqual(usage["cost_usd"], (2000 * 0.30 + 1000 * 2.50) / 1e6, places=9)

    def test_several_calls_add_up(self):
        with group_usage.capture_usage() as cap:
            for _ in range(3):
                stub_call(prompt=100, output=10)
        self.assertEqual(cap.as_dict()["calls"], 3)
        self.assertEqual(cap.as_dict()["input_tokens"], 300)

    def test_an_unknown_model_is_unpriced_not_free(self):
        with group_usage.capture_usage() as cap:
            stub_call(model="made-up-model-9")
        self.assertEqual(cap.unpriced_calls, 1)
        self.assertEqual(cap.as_dict()["cost_usd"], 0.0)
        self.assertEqual(cap.as_dict()["calls"], 1)

    def test_a_call_outside_any_capture_is_not_counted_and_the_recorder_is_restored(self):
        before = spend._recorder
        with group_usage.capture_usage() as cap:
            self.assertIsNot(spend._recorder, before)
        self.assertIs(spend._recorder, before)
        stub_call()
        self.assertEqual(cap.as_dict()["calls"], 0)

    def test_the_real_recorder_still_runs(self):
        seen = []
        before = spend._recorder
        spend.set_recorder(seen.append)
        try:
            with group_usage.capture_usage() as cap:
                stub_call()
            self.assertEqual(len(seen), 1)
            self.assertEqual(cap.calls, 1)
        finally:
            spend.set_recorder(before)

    def test_a_failure_inside_the_block_still_restores_the_recorder(self):
        before = spend._recorder
        with self.assertRaises(RuntimeError):
            with group_usage.capture_usage():
                raise RuntimeError("boom")
        self.assertIs(spend._recorder, before)
        self.assertIsNone(spend.current_context())

    def test_concurrent_captures_do_not_mix(self):
        barrier = threading.Barrier(4)

        def work(n):
            import contextvars

            def run():
                with group_usage.capture_usage() as cap:
                    barrier.wait(timeout=10)
                    for _ in range(n):
                        stub_call(prompt=10, output=1)
                return cap.as_dict()["calls"]

            return contextvars.copy_context().run(run)

        with ThreadPoolExecutor(max_workers=4) as pool:
            counts = list(pool.map(work, [1, 2, 3, 4]))
        self.assertEqual(counts, [1, 2, 3, 4])

    def test_records_with_no_owner_are_kept_apart_not_dropped(self):
        group_usage.UNATTRIBUTED.calls = 0
        group_usage.UNATTRIBUTED.cost_usd = 0.0
        with group_usage.capture_usage() as cap:
            # A worker thread that did not copy the context: no request id of ours.
            thread = threading.Thread(target=stub_call)
            thread.start()
            thread.join()
        self.assertEqual(cap.as_dict()["calls"], 0)
        self.assertEqual(group_usage.UNATTRIBUTED.calls, 1)

    def test_the_runner_decorator_nested_inside_a_capture_shares_its_request(self):
        with group_usage.capture_usage() as cap:
            with spend.spend_context(surface="eval") as inner:
                self.assertEqual(inner.request_id, cap.request_id)
                stub_call()
        self.assertEqual(cap.calls, 1)


class EstimateTests(SimpleTestCase):
    def test_estimate_matches_the_captured_cost(self):
        est = group_usage.estimate_cost_usd(
            MODEL, input_tokens=5000, output_tokens=400, cached_tokens=3000
        )
        with group_usage.capture_usage() as cap:
            stub_call(prompt=2000, cached=3000, output=400)
        self.assertAlmostEqual(est, cap.as_dict()["cost_usd"], places=9)

    def test_unknown_model_has_no_estimate(self):
        self.assertIsNone(group_usage.estimate_cost_usd("nope", input_tokens=1, output_tokens=1))


class ResultShapeTests(SimpleTestCase):
    def test_new_result_has_every_contract_key(self):
        result = group_usage.new_result("c1", "arm0", "ja")
        self.assertEqual(
            set(result),
            {"case_id", "arm", "lang", "passed", "checks", "tools", "answer", "usage"},
        )
        self.assertEqual(
            set(result["usage"]),
            {"input_tokens", "output_tokens", "cached_tokens", "cost_usd", "calls"},
        )
        self.assertFalse(result["passed"])
