"""Phase 2 — credits as the CUSTOMER'S limit.

What flipping `AI_CREDITS_AUTHORITATIVE` must do, and must not do:

  1. The balance replaces the daily ask count on every billable
     surface. A user with credits is served even when the daily counter
     is exhausted; a user without credits is refused even when it isn't.
  2. Per-model daily caps and the web-search cap stop applying. They are
     cost-shaping devices credits subsume — enforcing both would refuse
     requests the customer has already paid for.
  3. Free keeps a daily circuit breaker as ABUSE protection — OPT-IN
     via AI_FREE_DAILY_BREAKER since 2026-07-28 (off by default: a user
     with balance left must be able to spend it). When enabled, its
     copy never mentions credits or upgrading (their balance is fine).
  4. The flag requires the shadow engine. Enforcing against a ledger
     nobody writes to would show every user as permanently full.
  5. Flag OFF is byte-identical to Phase 1 behaviour.

The gate is deliberately WEAK — any positive balance may start a
request — because the strong version (refuse unless the balance covers
the quoted maximum) made the last `request_max_credits` of every plan
unspendable. The enforcement that replaces it happens mid-run, and
brings its own rule: a run stopped for credits is BILLABLE, because the
provider really performed the work. `CreditBudgetTests` and
`MidRunTerminationTests` below cover the two halves.
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
from origin.search_engine import credit_ledger, credits, metered
from origin.search_engine.agent_views import (
    _credit_budget_usd_micro,
    _credit_gate,
    _credits_block,
)
from origin.search_engine.llm import spend
from origin.search_engine.models import AiCreditEntry, AiRequestCost
from origin.tests.test_base import BaseAPITestCase


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


AUTHORITATIVE = override_settings(
    SEARCH_ENGINE=_se(AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=True)
)
SHADOW_ONLY = override_settings(
    SEARCH_ENGINE=_se(AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=False)
)
# The dangerous configuration: authoritative WITHOUT the ledger.
NO_LEDGER = override_settings(
    SEARCH_ENGINE=_se(AI_COST_METER=True, AI_CREDITS_SHADOW=False, AI_CREDITS_AUTHORITATIVE=True)
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
    def test_a_balance_below_the_quote_still_passes(self):
        """The whole allowance must be spendable.

        Refusing anything under the 5-credit quote made the last 5
        credits of every plan unreachable — half of Free. A user with 3
        credits gets to ask; the loop stops them mid-run if they really
        do run out (`request_budget_jpy_milli`).
        """
        self._drain_to("free", 3_000)
        self.assertIsNone(_credit_gate(self.UID, "free"))

    @AUTHORITATIVE
    def test_only_an_exhausted_balance_is_refused(self):
        self._drain_to("free", 0)
        resp = _credit_gate(self.UID, "free")
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data["category"], "ai_credits")
        self.assertEqual(resp.data["credits_remaining"], 0.0)
        self.assertEqual(resp.data["credits_limit"], 5.0)

    @AUTHORITATIVE
    def test_even_a_sliver_of_a_credit_gets_through(self):
        """0.01 credits is a real balance. Rounding it away at the gate
        would refuse a user who still has something to spend."""
        self._drain_to("free", 10)
        self.assertIsNone(_credit_gate(self.UID, "free"))

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


class CreditBudgetTests(_CacheClearing):
    """The reservation the weak gate depends on.

    Every one of these is a way the mid-run stop could silently never
    fire, leaving a permissive gate with nothing behind it — which would
    be strictly worse than the strict gate it replaced.
    """

    UID = "aaaaaaaa-0000-0000-0000-00000000bbbb"

    @AUTHORITATIVE
    def test_budget_is_the_balance_when_the_balance_is_the_smaller(self):
        credit_ledger.ensure_monthly_grant(self.UID, "free")
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()), user_id=self.UID, credits_milli=2_000
        )
        cache.clear()
        # 3 credits left, $0.10/credit -> $0.30 -> 300_000 micro-USD.
        self.assertEqual(_credit_budget_usd_micro(self.UID, "free"), 300_000)

    @AUTHORITATIVE
    def test_a_large_balance_is_not_clipped_to_the_per_request_cap(self):
        """The cap governs what a request is CHARGED, not how far it
        runs — cost above it is `absorbed`, by design.

        Clipping the budget to it would truncate a long request from a
        Max user with 145 credits still in hand, and tell them they had
        run out of credits. Bounding request LENGTH is
        `AI_REQUEST_MAX_JPY_MILLI`'s job, and a separate decision.
        """
        credit_ledger.ensure_monthly_grant(self.UID, "max")
        cache.clear()
        # 150 credits at $0.10 -> $15, NOT the 5-credit ($0.50) cap.
        self.assertEqual(_credit_budget_usd_micro(self.UID, "max"), 15_000_000)

    @AUTHORITATIVE
    def test_unlimited_plan_gets_no_budget(self):
        self.assertEqual(_credit_budget_usd_micro(self.UID, "enterprise"), 0)

    @SHADOW_ONLY
    def test_shadow_only_gets_no_budget(self):
        """Shadow must not stop anyone mid-run — that is the entire
        meaning of shadow."""
        credit_ledger.ensure_monthly_grant(self.UID, "free")
        self.assertEqual(_credit_budget_usd_micro(self.UID, "free"), 0)

    @AUTHORITATIVE
    def test_it_fails_open(self):
        with patch(
            "origin.search_engine.credit_ledger.balance_milli",
            side_effect=RuntimeError("db down"),
        ):
            self.assertEqual(_credit_budget_usd_micro(self.UID, "free"), 0)

    def test_zero_budget_makes_the_mid_run_check_inert(self):
        """0 has to mean "do not enforce" — it is what every non-credit
        surface passes, and a `>=` against 0 would stop every run before
        its first step."""
        with spend.spend_context(surface="ask", user_id=self.UID):
            self.assertFalse(spend.credit_budget_exhausted())

    def test_the_check_fires_only_once_spend_reaches_the_budget(self):
        with spend.spend_context(surface="ask", user_id=self.UID, credit_budget_usd_micro=300_000):
            ctx = spend.current_context()
            self.assertFalse(spend.credit_budget_exhausted())
            ctx.cost_usd_micro = 299_999
            self.assertFalse(spend.credit_budget_exhausted())
            ctx.cost_usd_micro = 300_000
            self.assertTrue(spend.credit_budget_exhausted())

    def test_the_budget_survives_into_a_rebound_context(self):
        """The agent loop runs on a worker thread that rebuilds its
        context from `spend_kwargs`. If the budget did not travel in
        those kwargs the loop would read 0 and never stop — the failure
        that makes the permissive gate unsafe."""
        kwargs = metered.spend_kwargs_for(
            "ask", self.UID, None, None, credit_budget_usd_micro=300_000
        )
        with spend.spend_context(**kwargs):
            self.assertEqual(spend.current_context().credit_budget_usd_micro, 300_000)
        # And the rollup path builds a SpendContext from the same dict.
        self.assertEqual(spend.SpendContext(**kwargs).credit_budget_usd_micro, 300_000)


class MidRunTerminationTests(_CacheClearing):
    """A run the balance cut short is CHARGED.

    This is the rule that keeps the permissive gate from being an
    exploit: if a credits-exhausted run billed 0, the cheapest way to
    use Genos would be to spend down to zero and keep asking, every
    request stopping early and costing nothing.
    """

    def _policy(self):
        return dj_settings.CREDIT_POLICY

    def test_credits_exhausted_is_billable(self):
        eligible = credits.eligible_usd_micro(
            result=AiRequestCost.RESULT_CREDITS_EXHAUSTED,
            surface="ask",
            computed_usd_micro=300_000,
            policy=self._policy(),
        )
        self.assertEqual(eligible, 300_000)

    def test_genuine_failures_stay_free(self):
        for result in (
            AiRequestCost.RESULT_PROVIDER_FAILURE,
            AiRequestCost.RESULT_APPLICATION_FAILURE,
            AiRequestCost.RESULT_USER_CANCELLATION,
            AiRequestCost.RESULT_SAFETY_REFUSAL,
        ):
            with self.subTest(result=result):
                self.assertEqual(
                    credits.eligible_usd_micro(
                        result=result,
                        surface="ask",
                        computed_usd_micro=300_000,
                        policy=self._policy(),
                    ),
                    0,
                    "work the provider did not perform is never the customer's",
                )

    def test_a_stopped_run_is_still_capped_at_the_per_request_maximum(self):
        """Overshoot past the budget is OURS. The mid-run check is
        between steps, so an in-flight call always completes and a run
        can end over budget; the customer must not pay for that."""
        policy = self._policy()
        eligible = credits.eligible_usd_micro(
            result=AiRequestCost.RESULT_CREDITS_EXHAUSTED,
            surface="ask",
            computed_usd_micro=9_990_000,
            policy=policy,
        )
        self.assertEqual(eligible, policy.request_max_usd_micro())

    def test_the_result_constants_agree_across_the_django_boundary(self):
        """`credits.py` restates these as literals so it stays importable
        without Django configured."""
        self.assertEqual(credits.RESULT_SUCCESS, AiRequestCost.RESULT_SUCCESS)
        self.assertEqual(credits.RESULT_CREDITS_EXHAUSTED, AiRequestCost.RESULT_CREDITS_EXHAUSTED)
        self.assertEqual(
            set(AiRequestCost.RESULTS_WORK_PERFORMED),
            {AiRequestCost.RESULT_SUCCESS, AiRequestCost.RESULT_CREDITS_EXHAUSTED},
        )

    def test_the_stop_message_is_one_string_shared_by_loop_and_classifier(self):
        """`agent_views` decides the run's RESULT by comparing against
        this copy. Two copies would drift and start scoring a
        credits-exhausted run as a provider failure — i.e. silently stop
        charging for it, which is the exploit this class exists to
        prevent."""
        from origin.search_engine.agent.controller import CREDITS_EXHAUSTED_MESSAGE
        from origin.search_engine.agent_views import (
            CREDITS_EXHAUSTED_MESSAGE as VIEW_COPY,
        )

        self.assertIs(CREDITS_EXHAUSTED_MESSAGE, VIEW_COPY)

    def test_the_stop_message_tells_the_user_what_to_do(self):
        """Unlike the cost ceiling — our own cap, deliberately silent
        about money — this one IS the user's limit, so it has to name it
        and say when it comes back."""
        from origin.search_engine.agent.controller import CREDITS_EXHAUSTED_MESSAGE

        lowered = CREDITS_EXHAUSTED_MESSAGE.lower()
        self.assertIn("credits", lowered)
        self.assertIn("stopped", lowered)
        self.assertNotIn("¥", CREDITS_EXHAUSTED_MESSAGE)
        self.assertNotIn("yen", lowered)

    def test_the_shipped_policy_bills_exactly_the_work_performed_results(self):
        """Guards the live file, not a fixture: widening
        `billable_results` to a real failure would start charging users
        for work they never received."""
        self.assertEqual(
            set(self._policy().billable_results),
            set(AiRequestCost.RESULTS_WORK_PERFORMED),
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
        self.assertEqual(block["limit"], 70.0)
        self.assertEqual(block["balance"], 70.0)
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
        self.assertEqual(block["balance"], 3.75)
        self.assertEqual(block["used"], 1.25)

    @AUTHORITATIVE
    def test_a_mid_period_downgrade_does_not_render_as_more_than_the_limit(self):
        """The bug this shipped with: granted 150 on max, downgraded to
        free, and the meter read "150 of 5 AI credits left" — dividing
        what they hold by what their NEW plan grants."""
        AiCreditEntry.objects.create(
            user_id=self.UID,
            entry_type=AiCreditEntry.ENTRY_GRANT,
            kind=AiCreditEntry.KIND_MONTHLY,
            credits_milli=150_000,
            period=credit_ledger.period_for(),
            plan="max",
            actor="system",
        )
        cache.clear()
        block = _credits_block(self.UID, "free")
        self.assertEqual(block["balance"], 150.0)
        self.assertEqual(block["limit"], 150.0, "the denominator is what the period granted")
        self.assertEqual(block["used"], 0.0)
        self.assertLessEqual(
            block["balance"], block["limit"], "a meter can never read X of fewer-than-X"
        )

    @AUTHORITATIVE
    def test_purchased_credits_ride_alongside_rather_than_inside_the_balance(self):
        """A pack must not be folded into `used`/`limit`. The plan still
        advertises 70; the extra 100 is a separate possession, and the
        client shows them under different reset copy because only one of
        them resets."""
        credit_ledger.ensure_monthly_grant(self.UID, "pro")
        AiCreditEntry.objects.create(
            user_id=self.UID,
            entry_type=AiCreditEntry.ENTRY_GRANT,
            kind=AiCreditEntry.KIND_PURCHASED,
            credits_milli=100_000,
            period=credit_ledger.period_for(),
            plan="pro",
            actor="stripe",
        )
        cache.clear()
        block = _credits_block(self.UID, "pro")
        self.assertEqual(block["limit"], 70.0, "the PLAN's allowance is unchanged")
        self.assertEqual(block["purchased_balance"], 100.0)
        self.assertEqual(block["balance"], 170.0, "spendable is both buckets")
        self.assertEqual(
            block["used"],
            0.0,
            "used counts the monthly allowance only — deriving it from the "
            "total went NEGATIVE the moment a pack was bought",
        )

    @AUTHORITATIVE
    def test_used_tracks_the_allowance_while_a_pack_is_held(self):
        credit_ledger.ensure_monthly_grant(self.UID, "pro")
        AiCreditEntry.objects.create(
            user_id=self.UID,
            entry_type=AiCreditEntry.ENTRY_GRANT,
            kind=AiCreditEntry.KIND_PURCHASED,
            credits_milli=100_000,
            period=credit_ledger.period_for(),
            plan="pro",
            actor="stripe",
        )
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()), user_id=self.UID, credits_milli=20_000
        )
        cache.clear()
        block = _credits_block(self.UID, "pro")
        self.assertEqual(block["used"], 20.0)
        self.assertEqual(block["purchased_balance"], 100.0, "the pack is untouched")

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
        self.assertEqual(resp.data["credits"]["limit"], 5.0)  # free
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
            patch("origin.search_engine.ingestion.ingest_conversation_run", return_value=False),
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
    def test_the_free_breaker_is_off_by_default(self):
        """Product decision 2026-07-28: no pre-flight hard stop. A free
        user with balance left is served no matter how many asks the
        daily counter recorded — the only customer-facing stop is the
        mid-run credits-exhausted stop."""
        credit_ledger.ensure_monthly_grant(str(self.user.id), "free")
        self._exhaust_daily_asks()
        cache.clear()
        resp = self._ask()
        self.assertEqual(
            resp.status_code,
            200,
            "with credits left, the exhausted daily counter must not block",
        )

    def test_the_opt_in_free_breaker_fires_and_does_not_mention_credits(self):
        """When explicitly enabled (active abuse): abuse protection, not
        a plan limit — telling a user with a healthy balance to
        'upgrade' would be wrong advice."""
        credit_ledger.ensure_monthly_grant(str(self.user.id), "free")
        self._exhaust_daily_asks()
        cache.clear()
        with override_settings(
            SEARCH_ENGINE=_se(
                AI_COST_METER=True,
                AI_CREDITS_SHADOW=True,
                AI_CREDITS_AUTHORITATIVE=True,
                AI_FREE_DAILY_BREAKER=True,
            )
        ):
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
            # "u1" is not a real user -> effective tier free, whose
            # integrations now EXCLUDE web (the reach gate) — pin the
            # gate open so this class keeps testing the CAP predicate.
            patch.object(web_search, "get_integrations", return_value=["web"]),
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
