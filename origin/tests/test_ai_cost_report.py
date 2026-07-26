"""`ai_cost_report` — the spend report and the only cross-provider alarm.

The report's job is not to produce a number; it is to produce a number
you can TRUST. Most of these tests are about the ways it must refuse to
mislead: unpriced units excluded from totals but named, unattributed
spend called out, a mid-window price change flagged rather than summed
through.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.search_engine.models import AiRequestCost, AiSpendEvent

REQ = "44444444-4444-4444-4444-444444444444"


def _se(**overrides):
    from django.conf import settings as dj

    cfg = dict(dj.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


def _event(**kw):
    kw.setdefault("request_id", REQ)
    kw.setdefault("surface", "ask")
    kw.setdefault("purpose", "loop")
    kw.setdefault("provider", "gemini")
    kw.setdefault("model", "gemini-3.6-flash")
    kw.setdefault("cost_basis", "priced")
    kw.setdefault("rate_card_version", "card-a")
    kw.setdefault("user_id", "u1")
    return AiSpendEvent.objects.create(**kw)


def _report(*args):
    out = StringIO()
    call_command("ai_cost_report", *args, stdout=out)
    return out.getvalue()


class ReportTests(TestCase):
    def test_empty_window_says_the_meter_may_be_off(self):
        text = _report("--days", "7")
        self.assertIn("No spend recorded", text)
        self.assertIn("AI_COST_METER", text, "the likeliest cause should be named")

    def test_totals_and_provider_reconciliation(self):
        _event(cost_jpy_milli=1500, cost_usd_micro=10_000)
        _event(provider="claude", model="claude-sonnet-5", cost_jpy_milli=4500, cost_usd_micro=30_000)
        text = _report("--days", "7")
        self.assertIn("-- Providers", text)
        self.assertIn("gemini", text)
        self.assertIn("claude", text)
        # Reconciliation happens in USD — that is what invoices are in.
        self.assertIn("$0.0400", text)
        self.assertIn("¥6.00", text)

    def test_names_the_out_of_gcp_billing_trap(self):
        """Whoever reconciles this must know two of three providers are
        invisible in the GCP console."""
        _event(cost_jpy_milli=100, cost_usd_micro=1000)
        self.assertIn("OUTSIDE GCP", _report("--days", "7"))

    def test_purposes_show_what_a_request_is_made_of(self):
        _event(purpose="loop", cost_jpy_milli=800, cost_usd_micro=5000)
        _event(purpose="rerank", cost_jpy_milli=200, cost_usd_micro=1000)
        text = _report("--days", "7")
        self.assertIn("rerank", text)
        self.assertIn("80.0%", text)

    def test_unattributed_spend_is_flagged_loudly(self):
        """The tripwire for the next uninstrumented entry point."""
        _event(surface="unattributed", purpose="judge", cost_jpy_milli=50, cost_usd_micro=300)
        text = _report("--days", "7")
        self.assertIn("UNATTRIBUTED", text)
        self.assertIn("spend_context()", text, "say how to fix it, not just that it happened")

    def test_clean_coverage_says_so_explicitly(self):
        _event(cost_jpy_milli=100, cost_usd_micro=500)
        self.assertIn("unattributed: none", _report("--days", "7"))

    def test_unpriced_units_are_named_and_excluded_from_the_total(self):
        """A 0 folded into a total is how a whole provider vanishes from
        a figure that still adds up."""
        _event(cost_jpy_milli=1000, cost_usd_micro=6000)
        _event(provider="tavily", model="advanced", unit_kind="search", units=2,
               cost_basis="unpriced", cost_jpy_milli=0, cost_usd_micro=0)
        text = _report("--days", "7")
        self.assertIn("unpriced", text)
        self.assertIn("tavily", text)
        self.assertIn("2 units", text)
        self.assertIn("¥1.00", text, "the total counts only the priced row")

    def test_a_mid_window_price_change_is_flagged_not_summed_through(self):
        _event(cost_jpy_milli=100, cost_usd_micro=500, rate_card_version="card-a")
        _event(cost_jpy_milli=100, cost_usd_micro=500, rate_card_version="card-b")
        text = _report("--days", "7")
        self.assertIn("RATE CARDS", text)
        self.assertIn("two regimes", text)

    def test_incomplete_calls_are_reported(self):
        _event(cost_basis="incomplete", error="RuntimeError: reset")
        self.assertIn("incomplete", _report("--days", "7"))

    def test_cost_per_successful_request(self):
        now = timezone.now()
        AiRequestCost.objects.create(
            request_id=REQ, surface="ask", user_id="u1",
            result=AiRequestCost.RESULT_SUCCESS, started_at=now,
        )
        _event(cost_jpy_milli=3000, cost_usd_micro=20_000)
        self.assertIn("cost per SUCCESSFUL request", _report("--days", "7"))

    def test_absorbed_cost_is_surfaced(self):
        """What we deliberately ate: failed requests and over-quote."""
        AiRequestCost.objects.create(
            request_id=REQ, surface="ask", user_id="u1",
            result=AiRequestCost.RESULT_PROVIDER_FAILURE,
            computed_jpy_milli=2000, charged_jpy_milli=0, absorbed_jpy_milli=2000,
            started_at=timezone.now(),
        )
        _event(cost_jpy_milli=2000, cost_usd_micro=13_000)
        self.assertIn("absorbed by us", _report("--days", "7"))

    def test_by_user_breakdown_is_opt_in(self):
        _event(user_id="alice", cost_jpy_milli=100, cost_usd_micro=500)
        self.assertNotIn("-- By user --", _report("--days", "7"))
        self.assertIn("alice", _report("--days", "7", "--by-user"))

    def test_window_excludes_older_rows(self):
        old = _event(cost_jpy_milli=99_000, cost_usd_micro=600_000)
        AiSpendEvent.objects.filter(pk=old.pk).update(
            created_at=timezone.now() - timedelta(days=40)
        )
        _event(cost_jpy_milli=1000, cost_usd_micro=6000)
        self.assertIn("¥1.00", _report("--days", "7"))

    def test_untagged_unit_rows_are_named_by_kind_not_lumped_together(self):
        """Tavily, query embeds and Cohere all used to fold into one
        `(untagged)` line — three different call types hiding as one.
        Rows written before purpose tagging landed must still separate."""
        _event(purpose="", unit_kind="search", provider="tavily", units=2,
               cost_jpy_milli=300, cost_usd_micro=2000)
        _event(purpose="", unit_kind="embed", provider="openai", units=4,
               cost_jpy_milli=10, cost_usd_micro=60)
        text = _report("--days", "7")
        self.assertIn("(search:tavily)", text)
        self.assertIn("(embed:openai)", text)
        self.assertNotIn("(untagged)", text)


class EffortAnatomyTests(TestCase):
    """`--by-effort` — the table the cost-optimization work steers by."""

    def _request(self, effort: str, request_id: str = REQ):
        return AiRequestCost.objects.create(
            request_id=request_id, surface="ask", user_id="u1", effort=effort,
            result=AiRequestCost.RESULT_SUCCESS, started_at=timezone.now(),
        )

    def test_by_effort_is_opt_in(self):
        _event(effort="low", cost_jpy_milli=100, cost_usd_micro=1000)
        self.assertNotIn("By effort", _report("--days", "7"))

    def test_anatomy_gives_per_request_means_per_purpose(self):
        """Two low-effort requests: the loop line must average across
        them, and the per-effort header must carry the USD mean — the
        figure the <$0.02 target is checked against."""
        other = "55555555-5555-5555-5555-555555555555"
        self._request("low", REQ)
        self._request("low", other)
        for req in (REQ, other):
            _event(request_id=req, effort="low", purpose="loop",
                   cost_jpy_milli=1200, cost_usd_micro=8000)
            _event(request_id=req, effort="low", purpose="rewrite",
                   cost_jpy_milli=300, cost_usd_micro=2000)
        text = _report("--days", "7", "--by-effort")
        self.assertIn("== low — 2 request(s)", text)
        self.assertIn("$0.0100", text, "the USD mean per request is the headline")
        self.assertIn("loop", text)
        self.assertIn("rewrite", text)
        self.assertIn("¥1.20", text, "loop JPY per request, not the sum")

    def test_cache_share_is_cached_over_total_input(self):
        """`prompt_tokens` is the UNCACHED remainder on every provider,
        so the cache share must be cached/(prompt+cached) — computing
        cached/prompt would report 400% and nobody could trust the rest."""
        self._request("medium")
        _event(effort="medium", cost_jpy_milli=100, cost_usd_micro=700,
               prompt_tokens=200, cached_tokens=800, output_tokens=50)
        text = _report("--days", "7", "--by-effort")
        self.assertIn("80%", text)

    def test_unit_rows_show_units_in_place_of_token_buckets(self):
        """A Tavily row has no token anatomy; printing 0-token columns
        would read as 'free input', so it shows its unit count instead."""
        self._request("low")
        _event(effort="low", purpose="web_search", unit_kind="search",
               provider="tavily", units=2, cost_jpy_milli=240, cost_usd_micro=1600)
        text = _report("--days", "7", "--by-effort")
        self.assertIn("[2 unit(s)]", text)

    def test_efforts_sort_low_medium_high_not_alphabetically(self):
        for eff in ("high", "low", "medium"):
            self._request(eff, request_id=f"66666666-6666-6666-6666-66666666666{len(eff)}")
            _event(effort=eff, cost_jpy_milli=100, cost_usd_micro=700)
        text = _report("--days", "7", "--by-effort")
        self.assertLess(text.index("== low"), text.index("== medium"))
        self.assertLess(text.index("== medium"), text.index("== high"))


class BudgetAlarmTests(TestCase):
    """The alarm. This is the only cross-provider budget check that can
    exist — a GCP billing budget cannot see Anthropic or OpenAI."""

    def test_no_budget_configured_alerts_on_nothing(self):
        """Opt-in: a threshold guessed before any measurement would
        either cry wolf on day one or never fire."""
        _event(cost_jpy_milli=999_000_000, cost_usd_micro=999_000)
        with override_settings(SEARCH_ENGINE=_se(AI_MONTHLY_BUDGET_JPY=0)):
            self.assertIn("no AI_MONTHLY_BUDGET_JPY", _report("--days", "7", "--alert"))

    def test_under_budget_passes(self):
        _event(cost_jpy_milli=1000, cost_usd_micro=6000)
        with override_settings(SEARCH_ENGINE=_se(AI_MONTHLY_BUDGET_JPY=50_000)):
            self.assertIn("-- Budget --", _report("--days", "7", "--alert"))

    def test_over_budget_fails_the_cron_run(self):
        """CronCommand turns a logged ERROR into a non-zero exit, which
        is what makes the Cloud Run job go red."""
        # 30-day budget ¥300 -> ~¥70 pro-rated over 7 days. Spend ¥500.
        _event(cost_jpy_milli=500_000, cost_usd_micro=3_000_000)
        with override_settings(SEARCH_ENGINE=_se(AI_MONTHLY_BUDGET_JPY=300)):
            with self.assertRaises(CommandError):
                _report("--days", "7", "--alert")

    def test_budget_is_pro_rated_to_the_window(self):
        # ¥30,000/month is ¥1,000/day; ¥900 over 1 day stays under.
        _event(cost_jpy_milli=900_000, cost_usd_micro=6_000_000)
        with override_settings(SEARCH_ENGINE=_se(AI_MONTHLY_BUDGET_JPY=30_000)):
            self.assertIn("-- Budget --", _report("--days", "1", "--alert"))

    def test_fifty_percent_early_warning_keeps_the_cron_green(self):
        """V2 §3.7's 50/80/100 ladder. The first sign of a hot month is
        a WARNING line, not the alarm — the cron must stay green below
        100%, or the red run stops meaning 'over budget'."""
        # ¥1,000/day pro-rated; ¥600 spent = 60% -> over the 50% line.
        _event(cost_jpy_milli=600_000, cost_usd_micro=4_000_000)
        with override_settings(SEARCH_ENGINE=_se(AI_MONTHLY_BUDGET_JPY=30_000)):
            out = _report("--days", "1", "--alert")  # must NOT raise
        self.assertIn("50% early-warning", out)

    def test_eighty_percent_names_its_own_line(self):
        _event(cost_jpy_milli=900_000, cost_usd_micro=6_000_000)  # 90%
        with override_settings(SEARCH_ENGINE=_se(AI_MONTHLY_BUDGET_JPY=30_000)):
            out = _report("--days", "1", "--alert")
        self.assertIn("80% early-warning", out)

    def test_under_fifty_percent_stays_quiet(self):
        _event(cost_jpy_milli=100_000, cost_usd_micro=700_000)  # 10%
        with override_settings(SEARCH_ENGINE=_se(AI_MONTHLY_BUDGET_JPY=30_000)):
            out = _report("--days", "1", "--alert")
        self.assertNotIn("early-warning", out)


class RebuildTests(TestCase):
    def test_rebuild_re_derives_rollups_from_events(self):
        """Events are ground truth; the rollup is derived. This is what
        makes a pricing bug fixable after the fact rather than baked in."""
        AiRequestCost.objects.create(
            request_id=REQ, surface="ask", user_id="u1",
            result=AiRequestCost.RESULT_SUCCESS,
            computed_jpy_milli=0, call_count=0, started_at=timezone.now(),
        )
        _event(cost_jpy_milli=1234, cost_usd_micro=8000)
        _event(cost_jpy_milli=766, cost_usd_micro=5000)

        with override_settings(SEARCH_ENGINE=_se(AI_COST_METER=True)):
            text = _report("--days", "7", "--rebuild")

        self.assertIn("Rebuilt 1 request rollup", text)
        row = AiRequestCost.objects.get(request_id=REQ)
        self.assertEqual(row.computed_jpy_milli, 2000)
        self.assertEqual(row.call_count, 2)
