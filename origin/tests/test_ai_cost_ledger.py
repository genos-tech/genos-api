"""The internal AI cost meter — ledger, pricing, and context hygiene.

What this has to guarantee, in rough order of how expensive it is to
get wrong:

  1. Nothing leaks between requests. Gunicorn runs threaded and reuses
     worker threads, so a context left bound would bill user B's tokens
     to user A. For an accounting ledger that is worse than a missing
     row — silently wrong instead of visibly incomplete.
  2. Cost is integer and priced from the rate card the row records, so
     a later price change cannot restate history.
  3. Unpriced spend is recorded and MARKED, never dropped and never
     counted as zero.
  4. The customer-protection rules hold structurally: a failed request
     is never charged, and a charge never exceeds the quote.
  5. Flag off writes nothing at all.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from origin.search_engine import spend_recorder
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage
from origin.search_engine.models import AiRequestCost, AiSpendEvent


def _se(**overrides):
    from django.conf import settings as dj

    cfg = dict(dj.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


METER_ON = override_settings(SEARCH_ENGINE=_se(AI_COST_METER=True))
METER_OFF = override_settings(SEARCH_ENGINE=_se(AI_COST_METER=False))


def _usage(model="gemini-3.6-flash", provider="gemini", **kw):
    u = CallUsage(provider=provider, model=model)
    for k, v in kw.items():
        setattr(u, k, v)
    return u


class ContextHygieneTests(SimpleTestCase):
    """The cross-request billing leak. See docstring item 1."""

    def test_context_is_none_before_and_after(self):
        self.assertIsNone(spend.current_context())
        with spend.spend_context(surface="ask", user_id="u1"):
            self.assertIsNotNone(spend.current_context())
        self.assertIsNone(
            spend.current_context(),
            "a leaked context bills the NEXT request on this worker thread to "
            "this user — worse than a missing row, because it is silently wrong",
        )

    def test_context_is_reset_even_when_the_body_raises(self):
        with self.assertRaises(ValueError):
            with spend.spend_context(surface="ask", user_id="u1"):
                raise ValueError("boom")
        self.assertIsNone(spend.current_context())

    def test_purpose_is_reset(self):
        with spend.spend_purpose("rerank"):
            self.assertEqual(spend.current_purpose(), "rerank")
        self.assertEqual(
            spend.current_purpose(),
            "",
            "a leaked purpose mislabels the NEXT call — the reranker's tag would "
            "land on the following loop step",
        )

    def test_nested_context_keeps_the_outer_request_id(self):
        """`search()` is called both by the search endpoint and by the
        search_kb tool inside an ask. The inner bind must not split one
        ask's spend across two request_ids."""
        with spend.spend_context(surface="ask", user_id="u1") as outer:
            with spend.spend_context(surface="search", user_id="u1") as inner:
                self.assertIs(inner, outer)
                self.assertEqual(inner.surface, "ask")
            self.assertIsNotNone(spend.current_context())

    def test_run_id_is_late_bound(self):
        # AgentRun does not exist when the first LLM call of an ask fires.
        with spend.spend_context(surface="ask") as ctx:
            self.assertIsNone(ctx.run_id)
            spend.bind_run_id("11111111-1111-1111-1111-111111111111")
            self.assertEqual(ctx.run_id, "11111111-1111-1111-1111-111111111111")

    def test_context_does_not_cross_into_a_bare_thread(self):
        """Documents WHY the worker re-binds instead of inheriting."""
        seen = {}

        def target():
            seen["ctx"] = spend.current_context()

        with spend.spend_context(surface="ask", user_id="u1"):
            t = threading.Thread(target=target)
            t.start()
            t.join()
        self.assertIsNone(seen["ctx"])

    def test_copied_context_does_cross(self):
        seen = {}

        def target():
            seen["ctx"] = spend.current_context()

        with spend.spend_context(surface="ask", user_id="u1"):
            snapshot = spend.copy_context_for_thread()
            t = threading.Thread(target=snapshot.run, args=(target,))
            t.start()
            t.join()
        self.assertIsNotNone(seen["ctx"])
        self.assertEqual(seen["ctx"].surface, "ask")


class PricingTests(SimpleTestCase):
    def test_prices_the_four_buckets_from_the_rate_card(self):
        # 1M fresh input @ $1.50 + 1M output @ $7.50 = $9.00.
        usd_micro, jpy_milli, basis = spend_recorder.price_llm_usage(
            _usage(prompt_tokens=1_000_000, output_tokens=1_000_000)
        )
        self.assertEqual(basis, "priced")
        self.assertEqual(usd_micro, 9_000_000)
        # x150 JPY/USD, in milli-yen.
        self.assertEqual(jpy_milli, 1_350_000)

    def test_thought_tokens_bill_at_the_output_rate(self):
        """Gemini bills thinking as output. Folding it into input, or
        dropping it, undercharges the DEFAULT provider."""
        with_thought = spend_recorder.price_llm_usage(
            _usage(output_tokens=500_000, thought_tokens=500_000)
        )
        as_output = spend_recorder.price_llm_usage(_usage(output_tokens=1_000_000))
        self.assertEqual(with_thought[0], as_output[0])

    def test_tool_prompt_tokens_bill_at_the_input_rate(self):
        folded = spend_recorder.price_llm_usage(
            _usage(prompt_tokens=500_000, tool_prompt_tokens=500_000)
        )
        as_input = spend_recorder.price_llm_usage(_usage(prompt_tokens=1_000_000))
        self.assertEqual(folded[0], as_input[0])

    def test_cached_input_is_cheaper_than_fresh(self):
        fresh = spend_recorder.price_llm_usage(_usage(prompt_tokens=1_000_000))
        cached = spend_recorder.price_llm_usage(_usage(cached_tokens=1_000_000))
        self.assertLess(cached[0], fresh[0])

    def test_unknown_model_is_unpriced_not_zero_cost(self):
        """An operator can pin a preview id via env. Reporting it as a
        real 0 is how a whole provider vanishes from a total that still
        looks about right."""
        _, _, basis = spend_recorder.price_llm_usage(
            _usage(model="some-preview-model", prompt_tokens=1_000_000)
        )
        self.assertEqual(basis, "unpriced")

    def test_no_reported_usage_is_incomplete(self):
        # The call died before the provider said anything.
        _, _, basis = spend_recorder.price_llm_usage(_usage())
        self.assertEqual(basis, "incomplete")

    def test_cost_is_integer(self):
        usd_micro, jpy_milli, _ = spend_recorder.price_llm_usage(_usage(prompt_tokens=12_345))
        self.assertIsInstance(usd_micro, int)
        self.assertIsInstance(jpy_milli, int)


class _RestoresRecorder:
    """Guarantees the process-wide recorder survives this test class.

    `spend._recorder` is module-global and installed once at app-ready.
    A test that swaps it and restores the wrong object silently breaks
    every test that runs after it — which is exactly what happened
    while writing these, and it presented as unrelated tests finding
    zero rows.
    """

    def setUp(self):
        super().setUp()
        original = spend._recorder
        self.addCleanup(spend.set_recorder, original)


class RecordingTests(_RestoresRecorder, TestCase):
    @METER_OFF
    def test_flag_off_writes_nothing(self):
        with spend.spend_context(surface="ask", user_id="u1"):
            spend.record_llm_call(_usage(prompt_tokens=1000), latency_ms=5)
        self.assertEqual(AiSpendEvent.objects.count(), 0)

    @METER_ON
    def test_records_a_row_with_the_rate_card_it_used(self):
        with spend.spend_context(surface="ask", user_id="u1", team_id="t1", plan="pro"):
            with spend.spend_purpose("loop"):
                spend.record_llm_call(_usage(prompt_tokens=1_000_000), latency_ms=42)
        row = AiSpendEvent.objects.get()
        self.assertEqual((row.surface, row.purpose, row.plan), ("ask", "loop", "pro"))
        self.assertEqual(row.provider, "gemini")
        self.assertEqual(row.latency_ms, 42)
        self.assertGreater(row.cost_jpy_milli, 0)
        self.assertTrue(row.rate_card_version, "a row with no rate card cannot be reconciled")
        self.assertEqual(row.fx_jpy_per_usd, 150.0)

    @METER_ON
    def test_unattributed_spend_is_still_recorded(self):
        """A silently-dropped path makes invoice reconciliation agree to
        'close enough' forever. The row is the tripwire for the next
        uninstrumented entry point."""
        spend.record_llm_call(_usage(prompt_tokens=1000))
        row = AiSpendEvent.objects.get()
        self.assertEqual(row.surface, spend.SURFACE_UNATTRIBUTED)

    @METER_ON
    def test_a_failed_call_is_still_recorded(self):
        with spend.spend_context(surface="ask", user_id="u1"):
            spend.record_llm_call(_usage(prompt_tokens=900), error="RuntimeError: boom")
        row = AiSpendEvent.objects.get()
        self.assertIn("boom", row.error)

    @METER_ON
    def test_non_token_spend_records_units_and_is_unpriced(self):
        with spend.spend_context(surface="ask", user_id="u1"):
            spend.record_units(unit_kind=spend.UNIT_SEARCH, units=2, provider="tavily")
        row = AiSpendEvent.objects.get()
        self.assertEqual((row.unit_kind, row.units), ("search", 2))
        self.assertEqual(row.cost_basis, "unpriced")

    @METER_ON
    def test_a_recorder_failure_never_propagates(self):
        """A dead ledger must lose rows, never the user's answer."""

        def explode(_rec):
            raise RuntimeError("db down")

        # Capture the REAL recorder first and restore that exact object.
        # Restoring `spend_recorder.record` instead would reinstall
        # whatever is patched over it at the time — which silently
        # poisons every later test in the process.
        original = spend._recorder
        spend.set_recorder(explode)
        try:
            with spend.spend_context(surface="ask"):
                spend.record_llm_call(_usage(prompt_tokens=1))
        finally:
            spend.set_recorder(original)


class RollupTests(_RestoresRecorder, TestCase):
    """The customer-protection rules, made structural."""

    def _ctx(self, **kw):
        return spend.SpendContext(
            request_id=kw.pop("request_id", "22222222-2222-2222-2222-222222222222"),
            surface="ask",
            user_id="u1",
            **kw,
        )

    @METER_ON
    def test_rollup_sums_the_events(self):
        ctx = self._ctx()
        spend_recorder.open_request(ctx, quoted_max_jpy_milli=10_000_000)
        with spend.spend_context(surface="ask", user_id="u1", request_id=ctx.request_id):
            spend.record_llm_call(_usage(prompt_tokens=1_000_000))
            spend.record_llm_call(_usage(output_tokens=1_000_000))
        spend_recorder.close_request(ctx, result=AiRequestCost.RESULT_SUCCESS)

        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        self.assertEqual(row.call_count, 2)
        events = AiSpendEvent.objects.filter(request_id=ctx.request_id)
        self.assertEqual(row.computed_jpy_milli, sum(e.cost_jpy_milli for e in events))

    @METER_ON
    def test_a_failed_request_is_never_charged(self):
        ctx = self._ctx()
        spend_recorder.open_request(ctx, quoted_max_jpy_milli=10_000_000)
        with spend.spend_context(surface="ask", user_id="u1", request_id=ctx.request_id):
            spend.record_llm_call(_usage(prompt_tokens=1_000_000))
        spend_recorder.close_request(ctx, result=AiRequestCost.RESULT_PROVIDER_FAILURE)

        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        self.assertGreater(row.computed_jpy_milli, 0, "we still paid for it")
        self.assertEqual(row.charged_jpy_milli, 0, "but the customer does not")
        self.assertEqual(row.absorbed_jpy_milli, row.computed_jpy_milli)

    @METER_ON
    def test_the_charge_never_exceeds_the_quote(self):
        """'A request cannot exceed the maximum shown before it starts,
        and the excess is absorbed by us.'"""
        ctx = self._ctx()
        spend_recorder.open_request(ctx, quoted_max_jpy_milli=100)
        with spend.spend_context(surface="ask", user_id="u1", request_id=ctx.request_id):
            spend.record_llm_call(_usage(prompt_tokens=1_000_000))
        spend_recorder.close_request(ctx, result=AiRequestCost.RESULT_SUCCESS)

        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        self.assertEqual(row.charged_jpy_milli, 100)
        self.assertEqual(row.absorbed_jpy_milli, row.computed_jpy_milli - 100)

    @METER_ON
    def test_unpriced_events_flag_the_rollup(self):
        """The rollup is then a LOWER BOUND and must not be presented as
        exact."""
        ctx = self._ctx()
        spend_recorder.open_request(ctx)
        with spend.spend_context(surface="ask", user_id="u1", request_id=ctx.request_id):
            spend.record_llm_call(_usage(model="some-preview-model", prompt_tokens=1000))
        spend_recorder.close_request(ctx, result=AiRequestCost.RESULT_SUCCESS)
        self.assertTrue(AiRequestCost.objects.get(request_id=ctx.request_id).has_unpriced)

    @METER_ON
    def test_shadow_credits_are_fractional(self):
        """Milli-credits, so a cheap request keeps sub-credit
        resolution. Storing whole credits would round a 0.25-credit ask
        up to 1 and overcharge it fourfold."""
        # 2000 milli-yen at ¥2/credit = exactly 1 credit.
        self.assertEqual(spend_recorder.shadow_credits_milli(2000), 1000)
        # A quarter-credit ask stays a quarter-credit.
        self.assertEqual(spend_recorder.shadow_credits_milli(500), 250)
        self.assertEqual(spend_recorder.shadow_credits_milli(200), 100)

    @METER_ON
    def test_quote_is_written_at_open_not_at_close(self):
        ctx = self._ctx()
        spend_recorder.open_request(ctx, quoted_max_jpy_milli=555)
        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        self.assertEqual(row.quoted_max_jpy_milli, 555)
        self.assertIsNone(row.finished_at)


class AdapterFinallyTests(_RestoresRecorder, TestCase):
    """The reason recording lives in a `finally` and not at the tail.

    `generate_step` is a GENERATOR. A tail call is skipped by a
    mid-stream raise and by `GeneratorExit`, and that is exactly the
    case that costs money: the provider bills whatever it already
    generated. Recording at the tail — where `_fill_usage_sink` used to
    sit — would have moved the blind spot rather than closed it.
    """

    def _chunk(self, text=None, usage=None):
        from types import SimpleNamespace

        part = SimpleNamespace(text=text, function_call=None, thought_signature=None)
        content = SimpleNamespace(parts=[part])
        return SimpleNamespace(usage_metadata=usage, candidates=[SimpleNamespace(content=content)])

    def _usage_meta(self, prompt=1000, output=200):
        from types import SimpleNamespace

        return SimpleNamespace(
            prompt_token_count=prompt,
            cached_content_token_count=0,
            candidates_token_count=output,
            thoughts_token_count=0,
            tool_use_prompt_token_count=0,
            total_token_count=prompt + output,
        )

    def _run(self, chunks):
        from origin.search_engine.llm.gemini_client import GeminiClient

        class _Models:
            def generate_content_stream(self, **_kw):
                return iter(chunks)

        fake = mock.Mock()
        fake.models = _Models()
        with mock.patch(
            "origin.search_engine.llm.gemini_client._get_client", return_value=fake
        ):
            client = GeminiClient()
            with spend.spend_context(surface="ask", user_id="u1"):
                return list(client.generate_step([], [], "sys"))

    @METER_ON
    def test_a_completed_stream_is_recorded(self):
        out = self._run([self._chunk("hi", self._usage_meta())])
        self.assertTrue(out)
        row = AiSpendEvent.objects.get()
        self.assertEqual(row.prompt_tokens, 1000)
        self.assertEqual(row.output_tokens, 200)
        self.assertGreater(row.cost_jpy_milli, 0)

    @METER_ON
    def test_a_stream_that_dies_mid_flight_is_still_billed(self):
        """The bug this closes: those tokens were generated, the
        provider charged for them, and nothing recorded a thing."""

        def exploding():
            yield self._chunk("partial", self._usage_meta(prompt=5000, output=900))
            raise RuntimeError("connection reset")

        with self.assertRaises(RuntimeError):
            self._run(exploding())

        row = AiSpendEvent.objects.get()
        self.assertIn("connection reset", row.error)
        self.assertEqual(row.prompt_tokens, 5000, "usage seen before the fault must survive")
        self.assertEqual(row.output_tokens, 900)
        self.assertGreater(row.cost_jpy_milli, 0, "we were billed, so the ledger must say so")

    @METER_ON
    def test_an_abandoned_generator_is_still_billed(self):
        """A client disconnect closes the generator, raising
        GeneratorExit — a BaseException an `except Exception` cannot
        see."""
        from origin.search_engine.llm.gemini_client import GeminiClient

        chunks = [
            self._chunk("one", self._usage_meta(prompt=3000, output=100)),
            self._chunk("two", self._usage_meta(prompt=3000, output=400)),
        ]

        class _Models:
            def generate_content_stream(self, **_kw):
                return iter(chunks)

        fake = mock.Mock()
        fake.models = _Models()
        with mock.patch(
            "origin.search_engine.llm.gemini_client._get_client", return_value=fake
        ):
            with spend.spend_context(surface="ask", user_id="u1"):
                gen = GeminiClient().generate_step([], [], "sys")
                next(gen)
                gen.close()  # what an abandoned response does

        row = AiSpendEvent.objects.get()
        self.assertGreater(row.prompt_tokens, 0)
        self.assertIn("GeneratorExit", row.error)

    @METER_ON
    def test_identity_survives_a_failure_before_any_usage(self):
        """A row with no provider or model cannot be reconciled against
        any invoice, so identity is stamped up front, not at fill time."""

        def dies_immediately():
            raise RuntimeError("stream open failed")
            yield  # pragma: no cover

        with self.assertRaises(RuntimeError):
            self._run(dies_immediately())

        row = AiSpendEvent.objects.get()
        self.assertEqual(row.provider, "gemini")
        self.assertTrue(row.model)
        self.assertEqual(row.cost_basis, "incomplete")


class NonLlmSourceTests(_RestoresRecorder, TestCase):
    """Paid calls that are not measured in tokens.

    These bill per call or per document, so they carry `units` and an
    explicit `unpriced` basis rather than an invented per-unit price.
    Recording them keeps invoice reconciliation complete — each is a
    separate billing line — without putting an estimate into a table
    whose whole value is that it contains none.
    """

    @METER_ON
    def test_web_search_records_two_credits_but_charges_one_quota_unit(self):
        """The gap is deliberate. `web_search_daily` is a product
        promise — "N searches a day" — and making each search cost two
        of them would halve every tier's allowance to fix an accounting
        problem. The 2x belongs in the ledger, not the quota."""
        from origin.search_engine.agent.tools import web_search

        charged = []
        fake_client = mock.Mock()
        fake_client.search.return_value = {"results": []}

        with mock.patch.object(
            web_search, "check_remaining", return_value=(True, 0, 10)
        ), mock.patch.object(
            web_search, "increment_usage", side_effect=lambda uid, key: charged.append(key)
        ), mock.patch.dict(
            __import__("django.conf", fromlist=["settings"]).settings.SEARCH_ENGINE,
            {"TAVILY_API_KEY": "tvly-test"},
        ), mock.patch.dict(
            "sys.modules", {"tavily": mock.Mock(TavilyClient=lambda api_key: fake_client)}
        ):
            ctx = mock.Mock(user_id="u1", team_id="t1")
            with spend.spend_context(surface="ask", user_id="u1"):
                web_search._run({"query": "django migrations"}, ctx)

        self.assertEqual(charged, ["__web_search__"], "one quota unit per search")
        row = AiSpendEvent.objects.get()
        self.assertEqual(row.provider, "tavily")
        self.assertEqual(row.units, 2, "advanced search bills Tavily 2 credits")
        self.assertEqual(row.unit_kind, "search")

    @METER_ON
    def test_a_failed_web_search_records_zero_units(self):
        """Tavily does not bill a failed search, but the row is what
        makes 'how much spend came from failures' answerable."""
        from origin.search_engine.agent.tools import web_search
        from origin.search_engine.agent.tools.base import ToolError

        fake_client = mock.Mock()
        fake_client.search.side_effect = RuntimeError("upstream down")

        with mock.patch.object(
            web_search, "check_remaining", return_value=(True, 0, 10)
        ), mock.patch.object(web_search, "increment_usage"), mock.patch.dict(
            __import__("django.conf", fromlist=["settings"]).settings.SEARCH_ENGINE,
            {"TAVILY_API_KEY": "tvly-test"},
        ), mock.patch.dict(
            "sys.modules", {"tavily": mock.Mock(TavilyClient=lambda api_key: fake_client)}
        ):
            ctx = mock.Mock(user_id="u1", team_id="t1")
            with spend.spend_context(surface="ask", user_id="u1"):
                with self.assertRaises(ToolError):
                    web_search._run({"query": "x"}, ctx)

        row = AiSpendEvent.objects.get()
        self.assertEqual(row.units, 0)
        self.assertIn("upstream down", row.error)

    @METER_ON
    def test_embeddings_are_recorded_at_the_wire_not_the_cache(self):
        """`embed_one` sits on an L1 lru_cache wrapping an L2 Redis
        lookup. Instrumenting the API surface would invent spend for
        calls that never left the process — so a second identical embed
        must NOT produce a second row."""
        from origin.search_engine import embeddings

        calls = {"n": 0}

        def fake_embed(texts, task_type=None):
            calls["n"] += 1
            with spend.spend_context(surface="search"):
                spend.record_units(
                    unit_kind=spend.UNIT_EMBED,
                    units=len(texts),
                    provider="vertex",
                    model="gemini-embedding-001",
                )
            return [[0.0] * 4 for _ in texts]

        fake_embedder = mock.Mock(model_name="gemini-embedding-001")
        fake_embedder.embed.side_effect = fake_embed

        embeddings._embed_one_cached.cache_clear()
        with mock.patch.object(embeddings, "_get_embedder", return_value=fake_embedder):
            embeddings.embed_one("same text")
            embeddings.embed_one("same text")

        self.assertEqual(calls["n"], 1, "the cache should have served the second call")
        self.assertEqual(
            AiSpendEvent.objects.count(),
            1,
            "a cache hit must not manufacture a ledger row",
        )
