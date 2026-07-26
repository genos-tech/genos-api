"""`ai_credit_report`, `ai_cost_prune`, and the dashboard's shadow section.

The report is the ANALYSIS page — read-only over the rollups and the
ledger, never a writer. What must hold:

  1. The per-plan picture answers the Phase 1 question: milestones,
     would-have-blocked counts, and the DAY an allowance ran out.
  2. `--compare-policy` replays the pure functions under a candidate
     and touches nothing — dual calculation is a function call.
  3. Pruning refuses the three structural NEVERs: the reconciliation
     window, unrolled events, and charged requests' events.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.search_engine import credit_ledger
from origin.search_engine.cost_dashboard import collect, render_html
from origin.search_engine.models import AiCreditEntry, AiRequestCost, AiSpendEvent

_UID_A = "aaaaaaaa-0000-0000-0000-000000000001"
_UID_B = "aaaaaaaa-0000-0000-0000-000000000002"


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


METER_ON = override_settings(SEARCH_ENGINE=_se(AI_COST_METER=True))


def _report(*args) -> str:
    out = StringIO()
    call_command("ai_credit_report", *args, stdout=out)
    return out.getvalue()


def _rollup(
    *,
    user=_UID_A,
    plan="free",
    surface="ask",
    result=AiRequestCost.RESULT_SUCCESS,
    computed=15_000,
    eligible=None,
    credits_milli=None,
    blocked=False,
    quoted=5000,
    started=None,
):
    eligible = computed if eligible is None else eligible
    credits_milli = (
        int(round(eligible / dj_settings.CREDIT_POLICY.credit_jpy))
        if credits_milli is None
        else credits_milli
    )
    return AiRequestCost.objects.create(
        request_id=uuid.uuid4(),
        user_id=user,
        plan=plan,
        surface=surface,
        result=result,
        computed_jpy_milli=computed,
        eligible_jpy_milli=eligible,
        shadow_credits_milli=credits_milli,
        quoted_max_credits_milli=quoted,
        would_have_blocked=blocked,
        started_at=started or timezone.now(),
    )


def _charge(user, milli, *, day=None):
    entry = AiCreditEntry.objects.create(
        user_id=user,
        entry_type=AiCreditEntry.ENTRY_CHARGE,
        credits_milli=-milli,
        request_id=uuid.uuid4(),
        period=credit_ledger.period_for(),
        actor="system",
    )
    if day is not None:
        # created_at drives the exhaustion-day line; backdate directly
        # (update bypasses save() by design here — the append-only guard
        # protects the FIELDS' meaning, and this test IS the writer).
        AiCreditEntry.objects.filter(id=entry.id).update(
            created_at=timezone.now().replace(day=day, hour=1, minute=0)
        )
    return entry


class CreditReportTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_empty_period_names_the_flag(self):
        out = _report()
        self.assertIn("AI_CREDITS_SHADOW", out)
        self.assertIn("what WOULD have happened", out)

    def test_per_plan_milestones_blocked_and_exhaustion_day(self):
        # User A (free, 10cr): 6cr on day 2 + 5cr on day 3 -> crosses
        # 50/80/100, exhausted on day 3.
        credit_ledger.ensure_monthly_grant(_UID_A, "free")
        _charge(_UID_A, 6_000, day=2)
        _charge(_UID_A, 5_000, day=3)
        _rollup(user=_UID_A, plan="free")
        # User B (pro, 100cr): 30cr -> under every milestone.
        credit_ledger.ensure_monthly_grant(_UID_B, "pro")
        _charge(_UID_B, 30_000, day=5)
        _rollup(user=_UID_B, plan="pro")
        # One ask that opened against an empty balance.
        _rollup(user=_UID_A, plan="free", blocked=True)

        out = _report()
        self.assertIn("-- Plans", out)
        # free row: 1 user, over all three milestones, 1 blocked ask.
        free_line = next(line for line in out.splitlines() if line.strip().startswith("free"))
        self.assertRegex(free_line, r"free\s+1\s+11\.00cr\s+1\s+1\s+1\s+1")
        pro_line = next(line for line in out.splitlines() if line.strip().startswith("pro"))
        self.assertRegex(pro_line, r"pro\s+1\s+30\.00cr\s+0\s+0\s+0\s+0")
        self.assertIn("day 3", out, "the exhaustion line names the day the allowance died")

    def test_request_shape_percentiles_and_requests_per_100(self):
        for credits_milli in (1000, 2000, 3000, 4000):
            _rollup(credits_milli=credits_milli, computed=credits_milli * 15)
        out = _report()
        self.assertIn("credits/request", out)
        self.assertIn("max=4.00cr", out)
        # 4 requests / 10 credits total = 40 per 100 credits.
        self.assertIn("requests per 100 credits: 40", out)

    def test_divergence_reports_absorption(self):
        # ¥30 real, ¥15 eligible (a failure absorbed), 1cr credited.
        _rollup(computed=15_000, eligible=15_000, credits_milli=1000)
        _rollup(
            result=AiRequestCost.RESULT_PROVIDER_FAILURE,
            computed=15_000,
            eligible=0,
            credits_milli=0,
        )
        out = _report()
        self.assertIn("absorption ratio: 2.00", out)

    def test_compare_policy_replays_without_writing(self):
        _rollup(computed=15_000)  # 1cr under ¥15
        posted_entries = AiCreditEntry.objects.count()
        candidate = """
policy:
    label: "cp-candidate"
    credit_jpy: 7.5
    request_max_credits: 5
    billable_surfaces: [ask, thread_summary, note_summary]
    billable_results: [success, credits_exhausted]
    excluded_purposes: []
entitlements:
    free: 10
    core: 40
    pro: 100
    max: 200
    enterprise: unlimited
monthly_ceiling_jpy:
    free: 150
    core: 600
    pro: 1500
    max: 3000
    enterprise: unlimited
"""
        with TemporaryDirectory() as d:
            p = Path(d) / "candidate.yaml"
            p.write_text(candidate, encoding="utf-8")
            out = _report("--compare-policy", str(p))
        self.assertIn("cp-candidate", out)
        # Halving the yen-per-credit doubles the charge: 1cr -> 2cr.
        self.assertIn("posted 1.00cr  →  candidate 2.00cr", out)
        self.assertIn("(+100%)", out)
        self.assertEqual(
            AiCreditEntry.objects.count(),
            posted_entries,
            "dual calculation must be read-only",
        )

    def test_bad_period_is_a_command_error(self):
        with self.assertRaises(CommandError):
            _report("--period", "July-2026")


class PruneTests(TestCase):
    def _event(self, *, request_id, age_days):
        e = AiSpendEvent.objects.create(
            request_id=request_id,
            user_id=_UID_A,
            surface="ask",
            provider="gemini",
            model="gemini-3.6-flash",
            cost_jpy_milli=100,
            cost_usd_micro=700,
        )
        AiSpendEvent.objects.filter(id=e.id).update(
            created_at=timezone.now() - timedelta(days=age_days)
        )
        return e

    def test_refuses_the_reconciliation_window(self):
        with self.assertRaises(CommandError):
            call_command("ai_cost_prune", days=7)

    def test_prunes_only_rolled_uncharged_old_events(self):
        prunable_rid = uuid.uuid4()
        self._event(request_id=prunable_rid, age_days=120)
        AiRequestCost.objects.create(
            request_id=prunable_rid, surface="ask", started_at=timezone.now()
        )

        unrolled_rid = uuid.uuid4()
        self._event(request_id=unrolled_rid, age_days=120)

        charged_rid = uuid.uuid4()
        self._event(request_id=charged_rid, age_days=120)
        AiRequestCost.objects.create(
            request_id=charged_rid, surface="ask", started_at=timezone.now()
        )
        AiCreditEntry.objects.create(
            user_id=_UID_A,
            entry_type=AiCreditEntry.ENTRY_CHARGE,
            credits_milli=-1000,
            request_id=charged_rid,
            period=credit_ledger.period_for(),
            actor="system",
        )

        fresh_rid = uuid.uuid4()
        self._event(request_id=fresh_rid, age_days=1)
        AiRequestCost.objects.create(
            request_id=fresh_rid, surface="ask", started_at=timezone.now()
        )

        out = StringIO()
        call_command("ai_cost_prune", days=90, stdout=out)
        remaining = set(AiSpendEvent.objects.values_list("request_id", flat=True))
        self.assertNotIn(prunable_rid, remaining, "old + rolled + uncharged: pruned")
        self.assertIn(unrolled_rid, remaining, "no rollup — the only record it happened")
        self.assertIn(charged_rid, remaining, "a posted charge pins its events")
        self.assertIn(fresh_rid, remaining, "inside the window")
        self.assertIn("deleted 1 event row(s)", out.getvalue())

    def test_dry_run_deletes_nothing(self):
        rid = uuid.uuid4()
        self._event(request_id=rid, age_days=120)
        AiRequestCost.objects.create(request_id=rid, surface="ask", started_at=timezone.now())
        out = StringIO()
        call_command("ai_cost_prune", days=90, dry_run=True, stdout=out)
        self.assertEqual(AiSpendEvent.objects.count(), 1)
        self.assertIn("nothing deleted", out.getvalue())


class DashboardShadowSectionTests(TestCase):
    def _fixture(self):
        AiSpendEvent.objects.create(
            request_id=uuid.uuid4(),
            user_id=_UID_A,
            surface="ask",
            provider="gemini",
            model="gemini-3.6-flash",
            cost_jpy_milli=100,
            cost_usd_micro=700,
        )

    def test_inactive_without_shadow_rows(self):
        self._fixture()
        _rollup(quoted=0)  # meter-only rollup: no shadow decision
        data = collect(days=7)
        self.assertFalse(data["shadow"]["active"])
        # The KPI tile is Phase 0's "Shadow credits" counter and always
        # renders; the per-plan SECTION must not.
        self.assertNotIn("nothing enforced", render_html(data))

    def test_per_plan_rows_and_blocked_note(self):
        self._fixture()
        _rollup(plan="free", credits_milli=2000, blocked=True)
        _rollup(plan="pro", credits_milli=1000)
        data = collect(days=7)
        self.assertTrue(data["shadow"]["active"])
        self.assertEqual(data["shadow"]["blocked_total"], 1)
        html = render_html(data)
        self.assertIn("Shadow credits", html)
        self.assertIn("nothing enforced", html)
        self.assertIn("ai_credit_report", html)
