"""Phase 2 — credits as the CUSTOMER'S limit.

What flipping `AI_CREDITS_AUTHORITATIVE` must do, and must not do:

  1. The balance replaces the daily ask count on every billable
     surface. A user with credits is served even when the daily counter
     is exhausted; a user without credits is refused even when it isn't.
  2. Per-model daily caps and the web-search cap stop applying. They are
     cost-shaping devices credits subsume — enforcing both would refuse
     requests the customer has already paid for.
  3. Free keeps a daily circuit breaker as ABUSE protection, and its
     copy never mentions credits or upgrading (their balance is fine).
  4. The flag requires the shadow engine. Enforcing against a ledger
     nobody writes to would show every user as permanently full.
  5. Flag OFF is byte-identical to Phase 1 behaviour.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine import credit_ledger
from origin.search_engine.agent_views import _credit_gate, _credits_block
from origin.tests.test_base import BaseAPITestCase


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


AUTHORITATIVE = override_settings(
    SEARCH_ENGINE=_se(
        AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=True
    )
)
SHADOW_ONLY = override_settings(
    SEARCH_ENGINE=_se(
        AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=False
    )
)
# The dangerous configuration: authoritative WITHOUT the ledger.
NO_LEDGER = override_settings(
    SEARCH_ENGINE=_se(
        AI_COST_METER=True, AI_CREDITS_SHADOW=False, AI_CREDITS_AUTHORITATIVE=True
    )
)


class _CacheClearing(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


class CreditGateTests(_CacheClearing):
    """The gate itself — balance vs the request's quoted maximum."""

    UID = "aaaaaaaa-0000-0000-0000-00000000c0de"

    def _drain_to(self, plan: str, remaining_milli: int):
        entitlement = dj_settings.CREDIT_POLICY.entitlements_milli[plan]
        credit_ledger.ensure_monthly_grant(self.UID, plan)
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()),
            user_id=self.UID,
            credits_milli=entitlement - remaining_milli,
        )
        cache.clear()

    @SHADOW_ONLY
    def test_shadow_only_never_gates(self):
        self._drain_to("free", 0)
        self.assertIsNone(_credit_gate(self.UID, "free"))

    @NO_LEDGER
    def test_authoritative_without_the_ledger_never_gates(self):
        """Without AI_CREDITS_SHADOW no charge is ever posted, so every
        balance reads full forever. Enforcing on that is worse than not
        enforcing — it would look like it works and gate nobody."""
        self.assertIsNone(_credit_gate(self.UID, "free"))

    @AUTHORITATIVE
    def test_a_full_balance_passes(self):
        credit_ledger.ensure_monthly_grant(self.UID, "pro")
        self.assertIsNone(_credit_gate(self.UID, "pro"))

    @AUTHORITATIVE
    def test_a_balance_below_the_quote_is_refused(self):
        # Quote is 5 credits; leave 3.
        self._drain_to("free", 3_000)
        resp = _credit_gate(self.UID, "free")
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data["category"], "ai_credits")
        self.assertEqual(resp.data["credits_remaining"], 3.0)
        self.assertEqual(resp.data["credits_limit"], 10.0)

    @AUTHORITATIVE
    def test_the_refusal_speaks_credits_never_yen(self):
        """Credits are the unit the customer was sold. A yen figure is
        something they never agreed to and cannot act on."""
        self._drain_to("free", 0)
        resp = _credit_gate(self.UID, "free")
        self.assertNotIn("¥", resp.data["error"])
        self.assertNotIn("yen", resp.data["error"].lower())
        self.assertIn("credits", resp.data["error"])
        self.assertIn("reset", resp.data["error"], "say when they get more")

    @AUTHORITATIVE
    def test_an_unlimited_plan_is_never_gated(self):
        self.assertIsNone(_credit_gate(self.UID, "enterprise"))

    @AUTHORITATIVE
    def test_it_fails_open(self):
        with patch(
            "origin.search_engine.credit_ledger.balance_milli",
            side_effect=RuntimeError("db down"),
        ):
            self.assertIsNone(
                _credit_gate(self.UID, "free"),
                "a check that cannot run must never block a paying user",
            )


class CreditsBlockTests(_CacheClearing):
    """`/agent/features/`'s `credits` block — presence is the client's
    render switch, exactly like `efforts[]`."""

    UID = "aaaaaaaa-0000-0000-0000-00000000beef"

    @SHADOW_ONLY
    def test_absent_until_authoritative(self):
        self.assertIsNone(
            _credits_block(self.UID, "pro"),
            "a client showing credits while the server enforces ask counts "
            "would be lying about what limits the user",
        )

    @AUTHORITATIVE
    def test_present_with_balance_limit_used_and_reset(self):
        credit_ledger.ensure_monthly_grant(self.UID, "pro")
        block = _credits_block(self.UID, "pro")
        self.assertEqual(block["limit"], 100.0)
        self.assertEqual(block["balance"], 100.0)
        self.assertEqual(block["used"], 0.0)
        self.assertFalse(block["unlimited"])
        self.assertEqual(block["per_request_max"], 5.0)
        self.assertTrue(block["period_end_iso"].startswith("20"))

    @AUTHORITATIVE
    def test_fractional_balances_survive(self):
        """A request can cost 0.11 credits. Rounding the balance to
        whole numbers would show '0 left' to someone who can still ask."""
        credit_ledger.ensure_monthly_grant(self.UID, "free")
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()), user_id=self.UID, credits_milli=1_250
        )
        cache.clear()
        block = _credits_block(self.UID, "free")
        self.assertEqual(block["balance"], 8.75)
        self.assertEqual(block["used"], 1.25)

    @AUTHORITATIVE
    def test_unlimited_plan_says_so_rather_than_omitting_the_block(self):
        block = _credits_block(self.UID, "enterprise")
        self.assertTrue(block["unlimited"])
        self.assertIsNone(block["balance"])


class FeaturesEndpointTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)

    def _get(self):
        self.authenticate()
        return self.client.get("/api/v2/agent/features/", HTTP_HOST="localhost")

    @SHADOW_ONLY
    def test_no_credits_key_before_the_flip(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("credits", resp.data)
        self.assertIn("llm_ask", resp.data)

    @AUTHORITATIVE
    def test_credits_key_appears_and_legacy_keys_remain(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("credits", resp.data)
        self.assertEqual(resp.data["credits"]["limit"], 10.0)  # free
        # Old clients keep working, and Free's breaker still needs the
        # ask counter — so the legacy keys stay.
        self.assertIn("llm_ask", resp.data)
        self.assertIn("web_search", resp.data)


class _StreamingBase(TransactionTestCase):
    """Ask-path tests: the fake agent runs on the real worker thread."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient()
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="creduser", email="cred@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="Cred Team", team_email="cred@team.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def _ask(self):
        def fake_run_agent(query, ctx, emit, **kwargs):
            emit({"type": "answer_delta", "text": "hi"})
            emit({"type": "done"})
            from django.db import connections  # noqa: PLC0415

            connections.close_all()
            return None

        with (
            patch("origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent),
            patch(
                "origin.search_engine.ingestion.ingest_conversation_run", return_value=False
            ),
        ):
            resp = self.client.post(
                "/api/v2/agent/ask/",
                {"query": "q", "team_id": str(self.team.pk)},
                format="json",
                HTTP_HOST="localhost",
            )
            if resp.status_code == 200:
                b"".join(resp.streaming_content)
            return resp

    def _exhaust_daily_asks(self):
        from origin.search_engine.quota import LLM_ASK_KEY, increment_usage

        limit = dj_settings.SEARCH_ENGINE["TIER_QUOTAS"]["free"]["llm_ask_daily"]
        for _ in range(limit):
            increment_usage(str(self.user.id), LLM_ASK_KEY)


class AskPathEnforcementTests(_StreamingBase):
    @SHADOW_ONLY
    def test_daily_cap_still_rules_before_the_flip(self):
        self._exhaust_daily_asks()
        resp = self._ask()
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["category"], "llm_ask")

    @AUTHORITATIVE
    def test_credits_replace_the_daily_cap(self):
        """The headline: daily counter exhausted, credits available —
        the user is SERVED. This is what 'no ask-count anywhere' means."""
        self._exhaust_daily_asks()
        credit_ledger.ensure_monthly_grant(str(self.user.id), "free")
        cache.clear()
        # Free keeps an abuse breaker on the same counter, so raise it
        # well clear to isolate the plan-limit behaviour.
        with override_settings(
            SEARCH_ENGINE=_se(
                AI_COST_METER=True,
                AI_CREDITS_SHADOW=True,
                AI_CREDITS_AUTHORITATIVE=True,
                TIER_QUOTAS={
                    **dj_settings.SEARCH_ENGINE["TIER_QUOTAS"],
                    "free": {
                        **dj_settings.SEARCH_ENGINE["TIER_QUOTAS"]["free"],
                        "llm_ask_daily": 10_000,
                    },
                },
            )
        ):
            resp = self._ask()
        self.assertEqual(resp.status_code, 200)

    @AUTHORITATIVE
    def test_an_empty_balance_refuses_with_the_credit_category(self):
        entitlement = dj_settings.CREDIT_POLICY.entitlements_milli["free"]
        credit_ledger.ensure_monthly_grant(str(self.user.id), "free")
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()), user_id=str(self.user.id), credits_milli=entitlement
        )
        cache.clear()
        resp = self._ask()
        self.assertEqual(resp.status_code, 429)
        body = resp.json()
        self.assertEqual(body["category"], "ai_credits")
        self.assertTrue(body["limit_reached"])

    @AUTHORITATIVE
    def test_the_free_breaker_still_fires_and_does_not_mention_credits(self):
        """Abuse protection, not a plan limit — telling a user with a
        healthy balance to 'upgrade' would be wrong advice."""
        credit_ledger.ensure_monthly_grant(str(self.user.id), "free")
        self._exhaust_daily_asks()
        cache.clear()
        resp = self._ask()
        self.assertEqual(resp.status_code, 429)
        body = resp.json()
        self.assertEqual(body["category"], "rate_limit")
        self.assertNotIn("credit", body["error"].lower())
        self.assertNotIn("upgrade", body["error"].lower())

    @AUTHORITATIVE
    def test_per_model_caps_stop_applying(self):
        """A model capped at 0/day for free must still serve when the
        user has credits — the cap is cost-shaping, and credits already
        bound the cost."""
        from origin.search_engine.quota import increment_usage

        credit_ledger.ensure_monthly_grant(str(self.user.id), "free")
        self.user.preferred_llm_provider = "claude"
        self.user.preferred_llm_model = "claude-opus-5"  # highend: 0/day on free
        self.user.save()
        increment_usage(str(self.user.id), "claude-opus-5")
        cache.clear()
        resp = self._ask()
        self.assertEqual(
            resp.status_code,
            200,
            "a capped model must still serve when the user has credits",
        )


class WebSearchCapTests(TestCase):
    """The web-search daily cap folds into credits (user's decision):
    a search is priced into the request since #174, so a separate
    allowance would charge for it twice."""

    def _run_search(self):
        from origin.search_engine.agent.tools import web_search

        with (
            patch.object(web_search, "check_remaining") as check,
            patch.object(web_search, "increment_usage"),
            patch.dict(dj_settings.SEARCH_ENGINE, {"TAVILY_API_KEY": ""}, clear=False),
        ):
            check.return_value = (False, 10, 10)  # cap exhausted
            from origin.search_engine.agent.tools.base import ToolError

            ctx = type("C", (), {"user_id": "u1", "team_id": "t1"})()
            try:
                web_search._run({"query": "x"}, ctx)
            except ToolError as e:
                return str(e), check.called
            return "", check.called

    @SHADOW_ONLY
    def test_cap_applies_before_the_flip(self):
        message, checked = self._run_search()
        self.assertTrue(checked)
        self.assertIn("web searches for today", message)

    @AUTHORITATIVE
    def test_cap_is_skipped_when_credits_rule(self):
        message, checked = self._run_search()
        self.assertFalse(checked, "the daily cap must not be consulted at all")
        # Falls through to the missing-key error instead — i.e. it got
        # PAST the quota gate.
        self.assertIn("not configured", message)
