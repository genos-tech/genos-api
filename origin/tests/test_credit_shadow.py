"""The shadow decision — V2 layer 5, recorded while nothing enforces.

The single number Phase 1 exists to produce is `would_have_blocked`:
under 10/40/100/200 at ¥15/credit, who would have run out, and when.
These tests pin the machinery that produces it:

  1. The decision is written at request START — quote and
     balance-before are point-in-time facts the close cannot recover.
  2. The settled charge posts to the ledger exactly once, through the
     request lifecycle (`finish_request`), never through the rollup
     derivation (`close_request` — that path is `--rebuild`'s replay).
  3. Flag off = not a single ledger row and no shadow fields. Shadow
     writes are opt-in like every other control here.
  4. Failure paths post nothing: a failed request's shadow credits are
     0 by the eligibility rules, so the ledger never sees it.
"""

from __future__ import annotations

import uuid

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.test import TestCase, override_settings

from origin.search_engine import credit_ledger, spend_recorder
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage
from origin.search_engine.models import AiCreditEntry, AiRequestCost


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


SHADOW_ON = override_settings(
    SEARCH_ENGINE=_se(AI_COST_METER=True, AI_CREDITS_SHADOW=True)
)
METER_ONLY = override_settings(
    SEARCH_ENGINE=_se(AI_COST_METER=True, AI_CREDITS_SHADOW=False)
)


class _ShadowBase(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        original = spend._recorder
        self.addCleanup(spend.set_recorder, original)

    def _ctx(self, *, surface="ask", plan="pro", user="u1"):
        return spend.SpendContext(
            request_id=str(uuid.uuid4()),
            surface=surface,
            user_id=user,
            team_id="t1",
            plan=plan,
        )

    def _spend_and_finish(self, ctx, *, tokens=1_000_000, result=AiRequestCost.RESULT_SUCCESS):
        token = spend._context.set(ctx)
        try:
            usage = CallUsage(provider="gemini", model="gemini-3.6-flash")
            usage.prompt_tokens = tokens
            spend.record_llm_call(usage)
        finally:
            spend._context.reset(token)
        spend_recorder.finish_request(ctx, result=result)


class ShadowDecisionAtOpenTests(_ShadowBase):
    @SHADOW_ON
    def test_open_records_quote_balance_and_verdict(self):
        ctx = self._ctx(plan="pro")
        spend_recorder.open_request(ctx)
        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        policy = dj_settings.CREDIT_POLICY
        self.assertEqual(row.quoted_max_credits_milli, policy.request_max_credits_milli)
        self.assertEqual(
            row.balance_before_milli,
            policy.entitlements_milli["pro"],
            "the first read materializes the month's grant, and the fresh "
            "balance is the plan entitlement",
        )
        self.assertFalse(row.would_have_blocked)

    @SHADOW_ON
    def test_a_partial_balance_does_not_flag_would_have_blocked(self):
        """`would_have_blocked` has to mirror the REAL gate.

        It once asked "does the balance cover the quote", which was the
        gate at the time. The gate now refuses only an exhausted balance.
        Left as it was, this field would count every user under 5 credits
        as blocked — inflating the single number in `ai_credit_report`
        that the allowance decision gets made on, in the direction that
        argues for raising allowances.
        """
        credit_ledger.ensure_monthly_grant("u1", "free")
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()), user_id="u1", credits_milli=2_000
        )
        ctx = self._ctx(plan="free")
        spend_recorder.open_request(ctx)
        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        self.assertEqual(row.balance_before_milli, 3_000)
        self.assertFalse(row.would_have_blocked)

    @SHADOW_ON
    def test_an_exhausted_balance_flags_would_have_blocked(self):
        credit_ledger.ensure_monthly_grant("u1", "free")
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()), user_id="u1", credits_milli=5_000
        )
        ctx = self._ctx(plan="free")
        spend_recorder.open_request(ctx)
        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        self.assertEqual(row.balance_before_milli, 0)
        self.assertTrue(row.would_have_blocked)

    @SHADOW_ON
    def test_an_unlimited_plan_records_no_balance_and_never_blocks(self):
        ctx = self._ctx(plan="enterprise")
        spend_recorder.open_request(ctx)
        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        self.assertIsNone(row.balance_before_milli)
        self.assertFalse(row.would_have_blocked)
        self.assertEqual(AiCreditEntry.objects.count(), 0)

    @SHADOW_ON
    def test_a_non_billable_surface_gets_no_decision_and_no_grant(self):
        """Quoting the search path would add a balance query to a
        surface that can never be charged."""
        ctx = self._ctx(surface="search", plan="pro")
        spend_recorder.open_request(ctx)
        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        self.assertEqual(row.quoted_max_credits_milli, 0)
        self.assertIsNone(row.balance_before_milli)
        self.assertEqual(AiCreditEntry.objects.count(), 0, "no grant materialized")

    @METER_ONLY
    def test_meter_only_mode_writes_no_shadow_fields_and_no_ledger(self):
        ctx = self._ctx()
        spend_recorder.open_request(ctx)
        self._spend_and_finish(ctx)
        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        self.assertEqual(row.quoted_max_credits_milli, 0)
        self.assertIsNone(row.balance_before_milli)
        self.assertEqual(AiCreditEntry.objects.count(), 0)
        # The COST meter still worked in full.
        self.assertGreater(row.computed_jpy_milli, 0)
        self.assertGreater(row.shadow_credits_milli, 0)


class SettleTests(_ShadowBase):
    @SHADOW_ON
    def test_finish_posts_the_charge_and_the_balance_moves(self):
        ctx = self._ctx(plan="pro")
        spend_recorder.open_request(ctx)
        self._spend_and_finish(ctx)

        row = AiRequestCost.objects.get(request_id=ctx.request_id)
        charge = AiCreditEntry.objects.get(entry_type="charge")
        self.assertEqual(charge.credits_milli, -row.shadow_credits_milli)
        self.assertEqual(str(charge.request_id), str(ctx.request_id))
        self.assertEqual(charge.credit_policy_version, row.credit_policy_version)
        self.assertEqual(
            credit_ledger.balance_milli("u1", "pro"),
            dj_settings.CREDIT_POLICY.entitlements_milli["pro"] - row.shadow_credits_milli,
        )

    @SHADOW_ON
    def test_a_double_finish_posts_exactly_one_charge(self):
        """The resumed-leg / retried-close case: `finish_request` may
        legitimately run twice for one logical request."""
        ctx = self._ctx(plan="pro")
        spend_recorder.open_request(ctx)
        self._spend_and_finish(ctx)
        spend_recorder.finish_request(ctx, result=AiRequestCost.RESULT_SUCCESS)
        self.assertEqual(AiCreditEntry.objects.filter(entry_type="charge").count(), 1)

    @SHADOW_ON
    def test_a_failed_request_posts_nothing(self):
        ctx = self._ctx(plan="pro")
        spend_recorder.open_request(ctx)
        self._spend_and_finish(ctx, result=AiRequestCost.RESULT_PROVIDER_FAILURE)
        self.assertEqual(
            AiCreditEntry.objects.filter(entry_type="charge").count(),
            0,
            "shadow credits are 0 for a failure, and zero charges post nothing",
        )

    @SHADOW_ON
    def test_a_non_billable_surface_posts_nothing(self):
        ctx = self._ctx(surface="search")
        spend_recorder.open_request(ctx)
        self._spend_and_finish(ctx)
        self.assertEqual(AiCreditEntry.objects.filter(entry_type="charge").count(), 0)

    @SHADOW_ON
    def test_the_charge_is_stamped_with_the_requests_period(self):
        """A request that ran in June must charge June even if the
        settle happens after midnight on July 1 — the period comes from
        the rollup's started_at, not from now()."""
        from datetime import datetime  # noqa: PLC0415
        from datetime import timezone as dt_tz  # noqa: PLC0415

        ctx = self._ctx(plan="pro")
        spend_recorder.open_request(ctx)
        AiRequestCost.objects.filter(request_id=ctx.request_id).update(
            started_at=datetime(2026, 6, 30, 23, 59, tzinfo=dt_tz.utc)
        )
        self._spend_and_finish(ctx)
        # close_request preserves the row's started_at, so the charge
        # lands in June.
        charge = AiCreditEntry.objects.get(entry_type="charge")
        self.assertEqual(charge.period, "2026-06")

    @SHADOW_ON
    def test_rebuild_still_posts_nothing(self):
        """`--rebuild` replays `close_request`, which must stay
        structurally unable to settle — the PR-4 guard, re-pinned here
        now that settling exists for it to accidentally reach."""
        from django.core.management import call_command  # noqa: PLC0415

        ctx = self._ctx(plan="pro")
        spend_recorder.open_request(ctx)
        self._spend_and_finish(ctx)
        self.assertEqual(AiCreditEntry.objects.filter(entry_type="charge").count(), 1)

        call_command("ai_cost_report", "--rebuild", "--days", "7")
        self.assertEqual(
            AiCreditEntry.objects.filter(entry_type="charge").count(),
            1,
            "rebuild re-derived the rollup but must never re-post the charge",
        )


class MeteredSurfaceSettlementTests(_ShadowBase):
    """The non-streaming billable surfaces settle too.

    `metered.metered_request` wraps both summary endpoints, and they
    CHARGE the user a daily ask. If its close called the bare
    `close_request` it would roll their cost up and post no charge —
    they would consume quota and show as free in `ai_credit_report`,
    which is the same class of bug as leaving them unattributed.
    """

    @SHADOW_ON
    def test_a_metered_surface_posts_its_charge(self):
        from origin.search_engine import metered  # noqa: PLC0415

        with metered.metered_request(
            surface="thread_summary", user_id="u1", team_id="t1"
        ):
            usage = CallUsage(provider="gemini", model="gemini-3.6-flash")
            usage.prompt_tokens = 1_000_000
            spend.record_llm_call(usage)

        row = AiRequestCost.objects.get(surface="thread_summary")
        self.assertGreater(row.shadow_credits_milli, 0)
        charge = AiCreditEntry.objects.get(entry_type="charge")
        self.assertEqual(charge.credits_milli, -row.shadow_credits_milli)
        self.assertEqual(str(charge.request_id), str(row.request_id))

    @SHADOW_ON
    def test_a_non_billable_metered_surface_posts_nothing(self):
        from origin.search_engine import metered  # noqa: PLC0415

        with metered.metered_request(surface="search", user_id="u1"):
            usage = CallUsage(provider="gemini", model="gemini-3.6-flash")
            usage.prompt_tokens = 1_000_000
            spend.record_llm_call(usage)

        self.assertGreater(
            AiRequestCost.objects.get(surface="search").computed_jpy_milli,
            0,
            "the COST is still measured",
        )
        self.assertEqual(AiCreditEntry.objects.filter(entry_type="charge").count(), 0)
