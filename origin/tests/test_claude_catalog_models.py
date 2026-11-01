"""The Claude 5.5 catalog rungs (MIP-859): prices, tiering, thinking/effort.

claude-haiku-5-5 / claude-sonnet-5-5 / claude-opus-5-5 replace
haiku-4-5 / sonnet-5 / opus-5. Three things about them are easy to get
silently wrong and expensive when wrong:

  * PRICES. Pinned to Anthropic's pricing page (2026-11-02). Note the
    5.5 Sonnet and Opus read the cache at 5% of input, not the 10% every
    earlier Claude model used.
  * THE LONG-PROMPT TIER. Haiku 5.5 bills a request at $0.50/$2.50
    instead of $0.10/$0.50 once its prompt passes 100K tokens, cache
    reads and writes included. A flat price under-costs those calls 5x.
  * WHAT THE CLIENT SENDS. These models think by default, reject a
    non-default temperature (400), and each have a different default
    effort. The request must pin thinking + effort per model from the
    catalog, send no temperature — and send NOTHING new to a model that
    does not declare `effort` (Haiku 4.5 rejects the parameters).

No network: the SDK is a stub, and the real-API check is documented in
the PR (it needs a working CLAUDE_API_KEY).
"""

from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from apis.llm_catalog import CatalogError
from origin.search_engine import spend_recorder
from origin.search_engine.llm.types import CallUsage
from origin.tests.test_llm_catalog_yaml import GOOD, _load

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"


def _rates(p):
    return (p.input, p.output, p.cached_input, p.cache_write)


class ShippedPricesTests(SimpleTestCase):
    def setUp(self):
        self.cat = settings.LLM_CATALOG

    def test_the_claude_rungs_are_the_5_5_family_cheapest_first(self):
        self.assertEqual(self.cat.provider_models("claude"), [HAIKU, SONNET, OPUS])
        self.assertEqual(
            (
                self.cat.model_for_rung("claude", 0),
                self.cat.model_for_rung("claude", 1),
                self.cat.model_for_rung("claude", 2),
            ),
            (HAIKU, SONNET, OPUS),
        )

    def test_prices_match_anthropics_pricing_page(self):
        self.assertEqual(_rates(self.cat.price_for(HAIKU)), (0.10, 0.50, 0.01, 0.125))
        self.assertEqual(_rates(self.cat.price_for(SONNET)), (2.00, 10.00, 0.10, 2.50))
        self.assertEqual(_rates(self.cat.price_for(OPUS)), (4.00, 20.00, 0.20, 5.00))

    def test_haiku_has_the_over_100k_tier(self):
        p = self.cat.price_for(HAIKU)
        self.assertEqual(p.long_prompt_above, 100_000)
        self.assertEqual(_rates(p.long_prompt), (0.50, 2.50, 0.05, 0.625))

    def test_only_haiku_is_tiered(self):
        self.assertIsNone(self.cat.price_for(SONNET).long_prompt)
        self.assertIsNone(self.cat.price_for(OPUS).long_prompt)

    def test_the_old_ids_are_gone_so_no_stale_rung_is_pickable(self):
        listed = {e["model"] for e in self.cat.catalog}
        for old in ("claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"):
            self.assertNotIn(old, listed)

    def test_the_server_default_is_a_catalog_model(self):
        self.assertEqual(settings.SEARCH_ENGINE["CLAUDE_MODEL"], SONNET)

    def test_every_rung_declares_effort_and_rejects_temperature(self):
        for model, effort in ((HAIKU, "low"), (SONNET, "medium"), (OPUS, "medium")):
            with self.subTest(model=model):
                self.assertEqual(self.cat.request_effort(model), effort)
                self.assertFalse(self.cat.supports_temperature(model))


def _usage(model=HAIKU, **kw):
    u = CallUsage(provider="claude", model=model)
    for k, v in kw.items():
        setattr(u, k, v)
    return u


class TieredPricingTests(SimpleTestCase):
    def _usd(self, **kw):
        return spend_recorder.price_llm_usage(_usage(**kw))[0] / 1_000_000

    def test_a_short_prompt_uses_the_low_card(self):
        # 100,000 prompt tokens is NOT over 100,000.
        self.assertAlmostEqual(self._usd(prompt_tokens=100_000), 0.01, places=6)
        self.assertAlmostEqual(
            self._usd(prompt_tokens=100_000, output_tokens=10_000), 0.015, places=6
        )

    def test_one_token_over_flips_the_whole_request_to_the_high_card(self):
        self.assertAlmostEqual(self._usd(prompt_tokens=100_001), 100_001 * 0.50 / 1e6, delta=1e-6)
        self.assertAlmostEqual(
            self._usd(prompt_tokens=100_001, output_tokens=10_000),
            100_001 * 0.50 / 1e6 + 10_000 * 2.50 / 1e6,
            delta=1e-6,
        )

    def test_cache_reads_and_writes_count_toward_the_threshold(self):
        """Anthropic prices a mostly-cached 150K prompt on the long card."""
        usd = self._usd(prompt_tokens=10_000, cached_tokens=90_000, cache_write_tokens=1_000)
        self.assertAlmostEqual(usd, (10_000 * 0.50 + 90_000 * 0.05 + 1_000 * 0.625) / 1e6, places=6)

    def test_a_flat_model_never_tiers(self):
        flat = self._usd(model=SONNET, prompt_tokens=500_000)
        self.assertAlmostEqual(flat, 500_000 * 2.00 / 1e6, places=6)

    def test_the_run_metrics_estimator_tiers_too(self):
        from origin.search_engine.management.commands.agent_run_metrics import _cost_usd

        low = _cost_usd(HAIKU, 100_000, 0, 0, 0)
        high = _cost_usd(HAIKU, 100_001, 0, 0, 0)
        self.assertAlmostEqual(low, 0.01, places=6)
        self.assertAlmostEqual(high, 100_001 * 0.50 / 1e6, delta=1e-6)


_TIER = (
    "          long_prompt_price: {above_tokens: 100000, input: 3.0, output: 15.0, "
    "cached_input: 0.30, cache_write: 0}\n"
)
_G_MID_PRICE = "          price: {input: 1.50, output: 7.50, cached_input: 0.15, cache_write: 0}\n"


class LoaderTierAndEffortTests(SimpleTestCase):
    def assertRejects(self, text, fragment):
        with self.assertRaises(CatalogError) as cm:
            _load(text)
        self.assertIn(fragment, str(cm.exception))

    def _with(self, extra):
        return GOOD.replace(_G_MID_PRICE, _G_MID_PRICE + extra)

    def test_a_valid_tier_parses(self):
        p = _load(self._with(_TIER)).price_for("g-mid")
        self.assertEqual(p.long_prompt_above, 100_000)
        self.assertEqual(_rates(p.long_prompt), (3.0, 15.0, 0.30, 0))
        self.assertEqual(p.for_prompt(100_000), p)
        self.assertEqual(p.for_prompt(100_001), p.long_prompt)

    def test_a_tier_moves_the_rate_card_fingerprint(self):
        flat = _load(GOOD).rate_card.fingerprint
        tiered = _load(self._with(_TIER)).rate_card.fingerprint
        self.assertNotEqual(flat, tiered)

    def test_a_flat_catalog_fingerprints_exactly_as_before_tiers_existed(self):
        # Four-element rows only: no tier key leaks into flat models.
        from apis.llm_catalog import _fingerprint_row

        row = _fingerprint_row(_load(GOOD).price_for("g-mid"))
        self.assertEqual(row, [1.5, 7.5, 0.15, 0.0])

    def test_rejects_a_tier_cheaper_than_the_base_price(self):
        self.assertRejects(
            self._with(_TIER.replace("input: 3.0", "input: 0.5")), "CHEAPER than `price`"
        )

    def test_rejects_a_tier_missing_a_rate(self):
        self.assertRejects(self._with(_TIER.replace("cache_write: 0}", "}")), "cache_write")

    def test_rejects_a_bad_threshold(self):
        for bad in ("0", "-5", "1.5", "true", '"100000"'):
            with self.subTest(above_tokens=bad):
                self.assertRejects(
                    self._with(_TIER.replace("above_tokens: 100000", f"above_tokens: {bad}")),
                    "above_tokens",
                )

    def test_rejects_an_unknown_tier_key(self):
        self.assertRejects(self._with(_TIER.replace("input:", "inpt:", 1)), "long_prompt_price")

    def test_effort_is_read_back_and_defaults_to_none(self):
        cat = _load(self._with("          effort: low\n"))
        self.assertEqual(cat.request_effort("g-mid"), "low")
        self.assertIsNone(cat.request_effort("g-cheap"))
        self.assertIsNone(cat.request_effort("not-in-catalog"))

    def test_rejects_an_effort_the_api_does_not_know(self):
        self.assertRejects(self._with("          effort: ultra\n"), "`effort` must be one of")


def _stream_kwargs(model):
    from origin.search_engine.llm.claude_client import ClaudeClient

    stream_cm = MagicMock()
    stream_cm.__enter__.return_value = iter(())
    client = MagicMock()
    client.messages.stream.return_value = stream_cm
    cfg = dict(settings.SEARCH_ENGINE)
    cfg["CLAUDE_MODEL"] = model
    with (
        override_settings(SEARCH_ENGINE=cfg),
        patch("origin.search_engine.llm.claude_client._get_client", return_value=client),
    ):
        list(ClaudeClient().generate_step([], [], "sys"))
    return client.messages.stream.call_args.kwargs


class ClientRequestShapeTests(SimpleTestCase):
    def test_each_rung_pins_adaptive_thinking_and_its_effort(self):
        for model, effort in ((HAIKU, "low"), (SONNET, "medium"), (OPUS, "medium")):
            with self.subTest(model=model):
                kwargs = _stream_kwargs(model)
                self.assertEqual(kwargs["thinking"], {"type": "adaptive"})
                self.assertEqual(kwargs["output_config"], {"effort": effort})

    def test_no_rung_sends_temperature(self):
        """A non-default temperature is a hard 400 on all three."""
        for model in (HAIKU, SONNET, OPUS):
            with self.subTest(model=model):
                self.assertNotIn("temperature", _stream_kwargs(model))

    def test_a_model_outside_the_catalog_gets_the_request_it_always_got(self):
        """An operator pin (CLAUDE_MODEL=claude-sonnet-4-6 is exactly
        what production runs today) must not start receiving parameters
        its model may reject."""
        kwargs = _stream_kwargs("claude-sonnet-4-6")
        for key in ("thinking", "output_config"):
            self.assertNotIn(key, kwargs)
        self.assertEqual(kwargs["temperature"], 0.2)
