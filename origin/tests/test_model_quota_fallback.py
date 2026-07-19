"""Quota-driven model fallback (SEARCH_ENGINE["MODEL_QUOTA_FALLBACK"]).

When a user's chosen model has exhausted its per-model daily cap
(`model_daily`) but the umbrella `llm_ask_daily` still has headroom, the
/ask/ view drops to the next-cheaper same-provider model that has room
instead of returning 429. Dark-shipped OFF by default.

Covers:
  - `cheaper_models_same_provider` — pure catalog walk, NEAREST-first,
    provider-isolated (no DB).
  - `_resolve_quota_fallback` — headroom-aware pick over rigged counters
    (skips exhausted / zero-cap rungs; None when nothing cheaper fits).
  - /ask/ integration: flag OFF → 429 unchanged; flag ON → 200 that
    SERVES + CHARGES the fallback model and surfaces `model_fallback` on
    the terminal `done` event; flag ON but nothing cheaper has room → 429.

No OpenSearch / LLM / network — `run_agent` is faked where the stream
runs, and the quota gate is reached with just query + team_id + auth.
"""

import json
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from origin.models.common.usage_models import ModelUsageCounter
from origin.search_engine import quota
from origin.search_engine.agent_views import _resolve_quota_fallback
from origin.search_engine.llm.choice import LlmChoice, cheaper_models_same_provider
from origin.tests.test_base import BaseAPITestCase

ASK_URL = "/api/v2/agent/ask/"

# Synthetic catalog: cheap→expensive within each provider (the curated
# ordering the fallback relies on). Synthetic ids keep the logic tests
# independent of the shipped model names.
TEST_CATALOG = [
    {"provider": "gemini", "model": "g-flash", "label": "G Flash"},
    {"provider": "gemini", "model": "g-pro", "label": "G Pro"},
    {"provider": "claude", "model": "c-haiku", "label": "C Haiku"},
    {"provider": "claude", "model": "c-sonnet", "label": "C Sonnet"},
    {"provider": "claude", "model": "c-opus", "label": "C Opus"},
]


def _se(*, model_daily=None, fallback=False, llm_ask_daily=20):
    """SEARCH_ENGINE with catalog / free-tier caps / flag replaced."""
    se = dict(settings.SEARCH_ENGINE)
    se["MODEL_CATALOG"] = TEST_CATALOG
    se["MODEL_QUOTA_FALLBACK"] = fallback
    tq = {k: dict(v) for k, v in se["TIER_QUOTAS"].items()}
    tq["free"] = {**tq["free"], "llm_ask_daily": llm_ask_daily, "model_daily": model_daily or {}}
    se["TIER_QUOTAS"] = tq
    return se


@override_settings(SEARCH_ENGINE=_se())
class CheaperModelsCatalogTests(SimpleTestCase):
    """Pure catalog walk — nearest-first, provider-isolated."""

    def test_nearest_first_order(self):
        # Opus steps down to sonnet FIRST (not straight to the cheapest).
        self.assertEqual(
            cheaper_models_same_provider(LlmChoice("claude", "c-opus")),
            ["c-sonnet", "c-haiku"],
        )

    def test_mid_model_drops_one_rung(self):
        self.assertEqual(
            cheaper_models_same_provider(LlmChoice("claude", "c-sonnet")),
            ["c-haiku"],
        )

    def test_cheapest_model_has_nothing_below(self):
        self.assertEqual(cheaper_models_same_provider(LlmChoice("claude", "c-haiku")), [])
        self.assertEqual(cheaper_models_same_provider(LlmChoice("gemini", "g-flash")), [])

    def test_provider_isolation(self):
        # Gemini's cheaper set is gemini-only — no claude bleed-through.
        self.assertEqual(
            cheaper_models_same_provider(LlmChoice("gemini", "g-pro")),
            ["g-flash"],
        )

    def test_unknown_model_is_empty(self):
        self.assertEqual(cheaper_models_same_provider(LlmChoice("claude", "nope")), [])


@override_settings(
    SEARCH_ENGINE=_se(model_daily={"c-opus": 1, "c-sonnet": 2, "c-haiku": 3, "g-pro": 2})
)
class ResolveQuotaFallbackTests(BaseAPITestCase):
    """Headroom-aware pick over rigged per-model counters (user = free tier)."""

    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([self.user.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])
        super().tearDown()

    def _use(self, model, count):
        ModelUsageCounter.objects.create(
            user=self.user,
            model_name=model,
            usage_date=timezone.now().date(),
            count=count,
        )

    def test_picks_nearest_cheaper_with_headroom(self):
        self._use("c-opus", 1)  # opus at its cap (1) → exhausted
        got = _resolve_quota_fallback(self.user.id, LlmChoice("claude", "c-opus"))
        self.assertEqual(got, LlmChoice("claude", "c-sonnet"))

    def test_skips_exhausted_nearer_model(self):
        self._use("c-opus", 1)
        self._use("c-sonnet", 2)  # sonnet also exhausted → step past it
        got = _resolve_quota_fallback(self.user.id, LlmChoice("claude", "c-opus"))
        self.assertEqual(got, LlmChoice("claude", "c-haiku"))

    def test_none_when_nothing_cheaper_has_room(self):
        self._use("c-opus", 1)
        self._use("c-sonnet", 2)
        self._use("c-haiku", 3)
        self.assertIsNone(_resolve_quota_fallback(self.user.id, LlmChoice("claude", "c-opus")))

    def test_cheapest_model_has_no_fallback(self):
        self._use("c-haiku", 3)
        self.assertIsNone(_resolve_quota_fallback(self.user.id, LlmChoice("claude", "c-haiku")))

    def test_unlimited_cheaper_model_always_has_room(self):
        # c-haiku is absent from model_daily here → unlimited → picked.
        with override_settings(SEARCH_ENGINE=_se(model_daily={"c-sonnet": 1})):
            self._use("c-sonnet", 1)
            got = _resolve_quota_fallback(self.user.id, LlmChoice("claude", "c-sonnet"))
            self.assertEqual(got, LlmChoice("claude", "c-haiku"))

    def test_zero_cap_cheaper_model_is_never_picked(self):
        # A model deliberately disabled at this tier (cap 0) reports no
        # headroom, so the fallback must not silently re-enable it.
        with override_settings(SEARCH_ENGINE=_se(model_daily={"c-sonnet": 1, "c-haiku": 0})):
            self._use("c-sonnet", 1)
            self.assertIsNone(
                _resolve_quota_fallback(self.user.id, LlmChoice("claude", "c-sonnet"))
            )


class _AskFallbackBase(BaseAPITestCase):
    """Shared /ask/ fixtures: user prefers a claude model, free tier."""

    PREFERRED_MODEL = "c-opus"

    def setUp(self):
        super().setUp()
        self.user.preferred_llm_provider = "claude"
        self.user.preferred_llm_model = self.PREFERRED_MODEL
        self.user.save(update_fields=["preferred_llm_provider", "preferred_llm_model"])
        quota.invalidate_effective_tier([self.user.id])
        self.authenticate()

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])
        super().tearDown()

    def _use(self, model, count):
        ModelUsageCounter.objects.create(
            user=self.user,
            model_name=model,
            usage_date=timezone.now().date(),
            count=count,
        )

    def _ask(self):
        return self.client.post(
            ASK_URL,
            {"query": "hello", "team_id": str(self.team.team_id)},
            format="json",
        )


@override_settings(
    SEARCH_ENGINE=_se(model_daily={"c-opus": 1, "c-sonnet": 2, "c-haiku": 3}, fallback=False)
)
class AskFallbackFlagOffTests(_AskFallbackBase):
    def test_exhausted_model_still_429s_when_flag_off(self):
        self._use("c-opus", 1)  # opus exhausted; sonnet has room but flag OFF
        resp = self._ask()
        self.assertEqual(resp.status_code, 429)
        body = resp.json()
        self.assertEqual(body["category"], "model")
        self.assertEqual(body["model"], "c-opus")


@override_settings(
    SEARCH_ENGINE=_se(model_daily={"c-opus": 1, "c-sonnet": 2, "c-haiku": 3}, fallback=True)
)
class AskFallbackFlagOnTests(_AskFallbackBase):
    def _consume(self, resp):
        raw = b"".join(resp.streaming_content).decode("utf-8")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def test_serves_and_charges_fallback_model(self):
        self._use("c-opus", 1)  # opus exhausted; sonnet (cap 2) has room

        def fake_run_agent(query, ctx, emit, **kwargs):
            emit({"type": "answer_delta", "text": "hi"})
            emit({"type": "done"})
            return None

        with patch(
            "origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent
        ):
            resp = self._ask()
            self.assertEqual(resp.status_code, 200)
            events = self._consume(resp)

        # The downgrade is surfaced on the terminal `done` event (no new
        # NDJSON event type — stays within the frozen vocabulary).
        done = next(e for e in events if e.get("type") == "done")
        self.assertEqual(
            done["model_fallback"], {"requested_model": "c-opus", "used_model": "c-sonnet"}
        )

        # Billing correctness: we charge the model we SERVED (sonnet) and
        # the umbrella LLM-ask counter — never the rejected opus.
        self.assertEqual(quota.get_used_today(self.user.id, "c-sonnet"), 1)
        self.assertEqual(quota.get_used_today(self.user.id, quota.LLM_ASK_KEY), 1)
        self.assertEqual(quota.get_used_today(self.user.id, "c-opus"), 1)  # seed, untouched

    def test_429_when_flag_on_but_nothing_cheaper_has_room(self):
        # Every claude rung exhausted → no fallback → the existing 429.
        self._use("c-opus", 1)
        self._use("c-sonnet", 2)
        self._use("c-haiku", 3)
        resp = self._ask()
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["category"], "model")


@override_settings(
    SEARCH_ENGINE=_se(model_daily={"c-haiku": 1}, fallback=True)
)
class AskFallbackCheapestModelTests(_AskFallbackBase):
    PREFERRED_MODEL = "c-haiku"  # already the cheapest — nothing below it

    def test_cheapest_model_exhaustion_429s_even_with_flag_on(self):
        self._use("c-haiku", 1)
        resp = self._ask()
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["model"], "c-haiku")
