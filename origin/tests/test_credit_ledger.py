"""The immutable credit ledger — V2 layer 6.

Ranked by expense-if-wrong:

  1. IMMUTABILITY. A posted entry can be neither updated nor deleted —
     §3.6's "an already-posted request charge must not be recalculated"
     is a property of the table, not a convention. Corrections are
     reversal entries.
  2. `--rebuild` MUST NOT touch the ledger. The rollup is derived and
     re-derivable under the current policy; a rebuild that re-posted
     charges would restate history under a newer policy — the exact
     failure the third table exists to prevent.
  3. Exactly-once posting by CONSTRAINT: one charge per request, one
     monthly grant per (user, period, plan). Races lose to the
     database, not to a lock someone forgot.
  4. Balance = Σ entries, fail-open to "unlimited" — a ledger hiccup
     must never block a request.
"""

from __future__ import annotations

import uuid

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.search_engine import credit_ledger, spend_recorder
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage
from origin.search_engine.models import AiCreditEntry, AiRequestCost


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


METER_ON = override_settings(SEARCH_ENGINE=_se(AI_COST_METER=True))


class _ClearsBalanceCache(TestCase):
    """`balance_milli` caches per (user, period) in the shared locmem
    backend, which OUTLIVES a test — the DB rolls back, the cache does
    not, and a stale balance from one test reads as a wrong answer in
    the next. Same class of bug the cache exists to risk in prod
    (60s staleness, accepted); in tests it must be flushed."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


def _grant(user="u1", milli=10_000, **kw):
    defaults = dict(
        user_id=user,
        entry_type=AiCreditEntry.ENTRY_GRANT,
        kind=AiCreditEntry.KIND_MONTHLY,
        credits_milli=milli,
        period=credit_ledger.period_for(),
        plan="free",
        actor="system",
    )
    defaults.update(kw)
    return AiCreditEntry.objects.create(**defaults)


class ImmutabilityTests(_ClearsBalanceCache):
    def test_update_raises(self):
        entry = _grant()
        entry.credits_milli = 999_999
        with self.assertRaises(TypeError):
            entry.save()
        entry.refresh_from_db()
        self.assertEqual(entry.credits_milli, 10_000, "the row must be untouched")

    def test_delete_raises(self):
        entry = _grant()
        with self.assertRaises(TypeError):
            entry.delete()
        self.assertTrue(AiCreditEntry.objects.filter(id=entry.id).exists())

    def test_correction_is_a_reversal_not_an_edit(self):
        # The typical op case: a fat-fingered MANUAL grant. (Reversing
        # the MONTHLY grant itself also works, but leaves its
        # plan-marker row so the month is deliberately not re-granted.)
        entry_id = credit_ledger.post_manual(
            user_id="u1", credits_milli=20_000, reason="typo", actor="op"
        )
        rev_id = credit_ledger.reverse_entry(entry_id, reason="fat-fingered", actor="op")
        rev = AiCreditEntry.objects.get(id=rev_id)
        self.assertEqual(rev.credits_milli, -20_000)
        self.assertEqual(rev.ref_id, entry_id)
        self.assertEqual(rev.entry_type, AiCreditEntry.ENTRY_REVERSAL)
        # The pair nets to zero and both rows survive; what remains is
        # the lazily-granted monthly entitlement.
        self.assertEqual(
            credit_ledger.balance_milli("u1", "free"),
            dj_settings.CREDIT_POLICY.entitlements_milli["free"],
        )
        self.assertEqual(AiCreditEntry.objects.count(), 3, "manual + reversal + monthly grant")

    def test_a_reversal_cannot_be_reversed_and_cannot_double(self):
        entry = _grant()
        rev_id = credit_ledger.reverse_entry(entry.id, reason="r", actor="op")
        with self.assertRaises(ValueError):
            credit_ledger.reverse_entry(entry.id, reason="again", actor="op")
        with self.assertRaises(ValueError):
            credit_ledger.reverse_entry(rev_id, reason="undo the undo", actor="op")


class ExactlyOnceTests(_ClearsBalanceCache):
    def test_one_charge_per_request_by_constraint(self):
        rid = str(uuid.uuid4())
        first = credit_ledger.post_charge(
            request_id=rid, user_id="u1", credits_milli=1500
        )
        second = credit_ledger.post_charge(
            request_id=rid, user_id="u1", credits_milli=1500
        )
        self.assertTrue(first)
        self.assertFalse(second, "a re-close or resumed leg must not double-bill")
        self.assertEqual(
            AiCreditEntry.objects.filter(entry_type="charge", request_id=rid).count(), 1
        )

    def test_a_charge_is_negative_in_the_ledger(self):
        rid = str(uuid.uuid4())
        credit_ledger.post_charge(request_id=rid, user_id="u1", credits_milli=1500)
        self.assertEqual(
            AiCreditEntry.objects.get(request_id=rid).credits_milli,
            -1500,
            "the sign lives in the data so the balance is a bare SUM",
        )

    def test_zero_and_negative_amounts_post_nothing(self):
        self.assertFalse(
            credit_ledger.post_charge(request_id=str(uuid.uuid4()), user_id="u1", credits_milli=0)
        )
        self.assertFalse(
            credit_ledger.post_charge(request_id=str(uuid.uuid4()), user_id="u1", credits_milli=-5)
        )
        self.assertEqual(AiCreditEntry.objects.count(), 0)

    def test_monthly_grant_posts_once_under_repeated_calls(self):
        for _ in range(3):
            credit_ledger.ensure_monthly_grant("u1", "pro")
        grants = AiCreditEntry.objects.filter(entry_type="grant", kind="monthly")
        self.assertEqual(grants.count(), 1)
        self.assertEqual(grants.get().credits_milli, 100_000)  # pro = 100 credits


class EntitlementGrantTests(_ClearsBalanceCache):
    def test_an_upgrade_posts_a_delta_grant_not_an_edit(self):
        credit_ledger.ensure_monthly_grant("u1", "core")  # 40
        credit_ledger.ensure_monthly_grant("u1", "pro")  # -> +60 delta
        grants = list(
            AiCreditEntry.objects.filter(entry_type="grant").order_by("created_at")
        )
        self.assertEqual([g.credits_milli for g in grants], [40_000, 60_000])
        self.assertEqual([g.plan for g in grants], ["core", "pro"])
        self.assertEqual(credit_ledger.balance_milli("u1", "pro"), 100_000)

    def test_a_downgrade_grants_nothing_and_takes_nothing(self):
        credit_ledger.ensure_monthly_grant("u1", "max")  # 200
        credit_ledger.ensure_monthly_grant("u1", "free")
        self.assertEqual(
            AiCreditEntry.objects.filter(entry_type="grant").count(), 1
        )
        self.assertEqual(credit_ledger.balance_milli("u1", "free"), 200_000)

    def test_enterprise_is_unlimited_no_accounting(self):
        credit_ledger.ensure_monthly_grant("u1", "enterprise")
        self.assertEqual(AiCreditEntry.objects.count(), 0)
        self.assertIsNone(credit_ledger.balance_milli("u1", "enterprise"))

    def test_an_unknown_plan_gets_frees_entitlement_not_unlimited(self):
        """A typo'd plan silently unlimited would be the model_daily
        uncapping bug all over again, in the commercial layer."""
        credit_ledger.ensure_monthly_grant("u1", "platnium")
        self.assertEqual(
            AiCreditEntry.objects.get(entry_type="grant").credits_milli,
            dj_settings.CREDIT_POLICY.entitlements_milli["free"],
        )


class BalanceTests(_ClearsBalanceCache):
    def test_balance_is_grants_minus_charges(self):
        credit_ledger.ensure_monthly_grant("u1", "core")  # 40_000
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()), user_id="u1", credits_milli=1_500
        )
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()), user_id="u1", credits_milli=2_500
        )
        self.assertEqual(credit_ledger.balance_milli("u1", "core"), 36_000)

    def test_periods_do_not_mix(self):
        _grant(period="2026-06", milli=99_000)
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()), user_id="u1", credits_milli=1_000, period="2026-06"
        )
        # This period: only the lazy grant.
        self.assertEqual(
            credit_ledger.balance_milli("u1", "free"),
            dj_settings.CREDIT_POLICY.entitlements_milli["free"],
        )

    def test_the_first_read_materializes_the_grant(self):
        self.assertEqual(AiCreditEntry.objects.count(), 0)
        balance = credit_ledger.balance_milli("u9", "max")
        self.assertEqual(balance, 200_000)
        self.assertEqual(
            AiCreditEntry.objects.filter(user_id="u9", entry_type="grant").count(), 1
        )


class ManualCommandTests(_ClearsBalanceCache):
    def test_grant_and_reverse_round_trip(self):
        call_command(
            "ai_credit_grant",
            user="u1",
            credits=20.0,
            reason="early adopter ran out",
            actor="kentaro",
        )
        entry = AiCreditEntry.objects.get(entry_type="grant", kind="manual")
        self.assertEqual(entry.credits_milli, 20_000)
        self.assertEqual(entry.actor, "kentaro")

        call_command(
            "ai_credit_grant", reverse=entry.id, reason="wrong user", actor="kentaro"
        )
        self.assertEqual(
            AiCreditEntry.objects.filter(entry_type="reversal", ref_id=entry.id).count(), 1
        )

    def test_a_write_without_reason_or_actor_is_refused(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("ai_credit_grant", user="u1", credits=5.0, actor="k")
        with self.assertRaises(CommandError):
            call_command("ai_credit_grant", user="u1", credits=5.0, reason="r")
        self.assertEqual(AiCreditEntry.objects.count(), 0)


class _RestoresRecorder:
    def setUp(self):
        super().setUp()
        original = spend._recorder
        self.addCleanup(spend.set_recorder, original)


class RebuildGuardTests(_RestoresRecorder, _ClearsBalanceCache):
    """`ai_cost_report --rebuild` re-derives ROLLUPS. It must be
    structurally incapable of touching the credit ledger — a rebuild
    under policy v2 that re-posted charges would restate history, the
    exact thing §3.6 forbids and the exact trap the shipped `_rebuild`
    (which calls `close_request` per row) would fall into if posting
    ever moved inside `close_request`. Charge-posting must stay in the
    request lifecycle, never in the rollup derivation."""

    @METER_ON
    def test_rebuild_rederives_rollups_but_never_the_ledger(self):
        # A closed, charged request from "last policy regime".
        rid = str(uuid.uuid4())
        with spend.spend_context(surface="ask", user_id="u1", request_id=rid) as ctx:
            usage = CallUsage(provider="gemini", model="gemini-3.6-flash")
            usage.prompt_tokens = 1_000_000
            spend.record_llm_call(usage)
            spend_recorder.close_request(ctx, result=AiRequestCost.RESULT_SUCCESS)
        row = AiRequestCost.objects.get(request_id=rid)
        credit_ledger.post_charge(
            request_id=rid,
            user_id="u1",
            credits_milli=row.shadow_credits_milli,
            credit_policy_version="cp-OLD+deadbeef",
        )

        before = list(
            AiCreditEntry.objects.values(
                "id", "credits_milli", "credit_policy_version", "created_at"
            )
        )

        call_command("ai_cost_report", "--rebuild", "--days", "7")

        after = list(
            AiCreditEntry.objects.values(
                "id", "credits_milli", "credit_policy_version", "created_at"
            )
        )
        self.assertEqual(before, after, "rebuild must not add, remove or restate any entry")
        # And the rollup WAS re-derived (same policy, same numbers —
        # but freshly written).
        self.assertGreater(
            AiRequestCost.objects.get(request_id=rid).finished_at, row.finished_at
        )

    @METER_ON
    def test_the_ledger_survives_a_rollup_reclose_under_a_new_policy(self):
        """The charge keeps its posted version; the rollup moves to the
        current one. That divergence is CORRECT — it is what 'old
        requests remain charged under v1' looks like in data."""
        rid = str(uuid.uuid4())
        with spend.spend_context(surface="ask", user_id="u1", request_id=rid) as ctx:
            usage = CallUsage(provider="gemini", model="gemini-3.6-flash")
            usage.prompt_tokens = 1_000_000
            spend.record_llm_call(usage)
            spend_recorder.close_request(ctx, result=AiRequestCost.RESULT_SUCCESS)
        credit_ledger.post_charge(
            request_id=rid,
            user_id="u1",
            credits_milli=500,
            credit_policy_version="cp-OLD+deadbeef",
        )

        call_command("ai_cost_report", "--rebuild", "--days", "7")

        charge = AiCreditEntry.objects.get(request_id=rid)
        rollup = AiRequestCost.objects.get(request_id=rid)
        self.assertEqual(charge.credit_policy_version, "cp-OLD+deadbeef")
        self.assertEqual(rollup.credit_policy_version, dj_settings.CREDIT_POLICY.version)


class LedgerHygieneTests(_ClearsBalanceCache):
    def test_period_is_stamped_not_derived(self):
        entry = _grant(period="2026-06")
        # created_at is NOW (July); the entry still belongs to June —
        # only the writer knows which month a request ran in.
        self.assertEqual(entry.period, "2026-06")
        self.assertEqual(credit_ledger.period_for(timezone.now()), timezone.now().strftime("%Y-%m"))

    def test_manual_entries_require_reason_and_actor(self):
        with self.assertRaises(ValueError):
            credit_ledger.post_manual(
                user_id="u1", credits_milli=1000, reason="  ", actor="op"
            )
        with self.assertRaises(ValueError):
            credit_ledger.post_manual(
                user_id="u1", credits_milli=1000, reason="r", actor=""
            )
