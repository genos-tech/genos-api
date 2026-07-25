"""The HTML cost dashboard.

The fixture is deliberately AWKWARD — unpriced rows, an unattributed
row, two rate cards, a failed request, a hostile string. A dashboard
tested against five clean priced rows would pass whether or not the
semantics it exists to preserve survived, which is the same shape of
vacuous test that had to be rewritten in the ceiling suite.
"""

from __future__ import annotations

import re
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.search_engine import cost_dashboard
from origin.search_engine.models import AiRequestCost, AiSpendEvent

_CARD_A = "2026-07-01+aaaaaaaaaaaa"
_CARD_B = "2026-07-20+bbbbbbbbbbbb"
_USER = "11111111-1111-4111-8111-111111111111"


class _Fixture(TestCase):
    """Six events over two requests, one of which failed, spanning two
    rate cards, plus an unattributed embedding."""

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.ok_request = "22222222-2222-4222-8222-222222222222"
        self.bad_request = "33333333-3333-4333-8333-333333333333"

        # A successful ask: two priced LLM calls + one unpriced embed.
        self._event(self.ok_request, purpose="loop", jpy=4000, usd=26_000, prompt=5000, out=100)
        self._event(self.ok_request, purpose="rewrite", jpy=1000, usd=6_600, prompt=250, out=20)
        self._event(
            self.ok_request,
            purpose="",
            provider="vertex",
            model="gemini-embedding-001",
            basis="unpriced",
            unit_kind="embed",
            units=1,
        )
        # A failed ask: the provider died before reporting usage.
        self._event(
            self.bad_request,
            purpose="loop",
            basis="incomplete",
            error="BadRequestError: 400 <script>alert(1)</script>",
            card=_CARD_B,
        )
        # Something paid with no request context at all — the tripwire.
        self._event(
            "44444444-4444-4444-8444-444444444444",
            surface="unattributed",
            purpose="",
            provider="vertex",
            model="gemini-embedding-001",
            basis="unpriced",
            unit_kind="embed",
            units=1,
            user_id="",
        )

        AiRequestCost.objects.create(
            request_id=self.ok_request,
            user_id=_USER,
            surface="ask",
            plan="core",
            result=AiRequestCost.RESULT_SUCCESS,
            computed_jpy_milli=5000,
            computed_usd_micro=32_600,
            charged_jpy_milli=5000,
            shadow_credits_milli=2500,
            call_count=3,
            has_unpriced=True,
            rate_card_version=_CARD_A,
            started_at=now,
            finished_at=now,
        )
        AiRequestCost.objects.create(
            request_id=self.bad_request,
            user_id=_USER,
            surface="ask",
            plan="core",
            result=AiRequestCost.RESULT_PROVIDER_FAILURE,
            computed_jpy_milli=900,
            charged_jpy_milli=0,
            absorbed_jpy_milli=900,
            call_count=1,
            rate_card_version=_CARD_B,
            started_at=now,
            finished_at=now,
        )

    def _event(self, request_id, **kw):
        kw.setdefault("surface", "ask")
        kw.setdefault("user_id", _USER)
        kw.setdefault("provider", "gemini")
        kw.setdefault("model", "gemini-3.5-flash-lite")
        kw.setdefault("card", _CARD_A)
        return AiSpendEvent.objects.create(
            request_id=request_id,
            surface=kw["surface"],
            purpose=kw.get("purpose", ""),
            user_id=kw["user_id"],
            provider=kw["provider"],
            model=kw["model"],
            prompt_tokens=kw.get("prompt", 0),
            output_tokens=kw.get("out", 0),
            cost_jpy_milli=kw.get("jpy", 0),
            cost_usd_micro=kw.get("usd", 0),
            cost_basis=kw.get("basis", "priced"),
            unit_kind=kw.get("unit_kind", ""),
            units=kw.get("units", 0),
            error=kw.get("error", ""),
            rate_card_version=kw["card"],
        )


class CollectTests(_Fixture):
    def test_totals_cover_every_event_and_request(self):
        d = cost_dashboard.collect(days=1)
        self.assertTrue(d["has_data"])
        self.assertEqual(d["totals"]["jpy_milli"], 5000)
        self.assertEqual(d["totals"]["calls"], 5)
        self.assertEqual(d["totals"]["requests"], 2)
        self.assertEqual(d["totals"]["successful_requests"], 1)

    def test_cost_per_request_divides_by_SUCCESSFUL_requests(self):
        """Dividing by all requests would make a provider outage look
        like an efficiency win."""
        d = cost_dashboard.collect(days=1)
        self.assertEqual(d["totals"]["jpy_milli_per_success"], 5000)

    def test_unpriced_calls_contribute_no_cost(self):
        """Their 0 is meaningless — the moment it becomes spend, a whole
        billing line silently reads as free."""
        d = cost_dashboard.collect(days=1)
        vertex = next(r for r in d["providers"] if r["key"] == "vertex")
        self.assertEqual(vertex["jpy_milli"], 0)
        self.assertEqual(vertex["calls"], 2)
        self.assertEqual(vertex["unsized_calls"], 2, "a ¥0 row must be able to say why")

    def test_coverage_names_all_three_ways_the_total_can_be_wrong(self):
        cov = cost_dashboard.collect(days=1)["coverage"]
        self.assertEqual(cov["unattributed_calls"], 1)
        self.assertEqual(cov["incomplete_calls"], 1)
        self.assertEqual(len(cov["unpriced"]), 1)
        self.assertEqual(cov["unpriced"][0]["units"], 2)
        self.assertEqual(cov["rate_cards"], [_CARD_A, _CARD_B])

    def test_days_with_no_spend_are_zero_rows_not_missing_rows(self):
        """A gap and a broken meter must not look the same."""
        d = cost_dashboard.collect(days=5)
        self.assertGreaterEqual(len(d["daily"]), 5)
        self.assertEqual(sum(1 for r in d["daily"] if r["jpy_milli"] == 0), len(d["daily"]) - 1)

    def test_the_window_excludes_older_rows(self):
        AiSpendEvent.objects.filter(purpose="loop").update(
            created_at=timezone.now() - timedelta(days=40)
        )
        self.assertEqual(cost_dashboard.collect(days=2)["totals"]["jpy_milli"], 1000)

    def test_by_user_is_opt_in(self):
        self.assertEqual(cost_dashboard.collect(days=1)["users"], [])
        self.assertTrue(cost_dashboard.collect(days=1, by_user=True)["users"])


class AgreesWithTheTextReportTests(_Fixture):
    """The one guard against the two readers drifting apart.

    They are separate on purpose — the report's exit code is the budget
    alarm and must not depend on a rendering change — so something has
    to hold them to the same arithmetic.
    """

    def test_same_total_same_call_count_same_unattributed_count(self):
        out = StringIO()
        call_command("ai_cost_report", "--days", "1", stdout=out)
        report = out.getvalue()
        d = cost_dashboard.collect(days=1)

        self.assertIn(cost_dashboard.yen(d["totals"]["jpy_milli"]), report)
        self.assertIn(f"paid calls: {d['totals']['calls']}", report)
        self.assertIn(
            f"UNATTRIBUTED: {d['coverage']['unattributed_calls']} call(s)", report
        )


class RenderTests(_Fixture):
    def _html(self, **kw) -> str:
        return cost_dashboard.render_html(cost_dashboard.collect(days=1, **kw))

    def test_renders_a_self_contained_page(self):
        html = self._html()
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn("<style>", html)
        # A CDN reference would make the file useless offline, which is
        # most of the point of shipping one file.
        for external in ("http://", "https://", "<script"):
            self.assertNotIn(external, html)

    def test_escapes_a_provider_error_string(self):
        """`error` is 200 chars of provider text and `model` can be any
        operator env pin — neither is trusted markup."""
        AiSpendEvent.objects.filter(cost_basis="incomplete").update(
            model="<img src=x onerror=alert(1)>"
        )
        html = self._html()
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)

    def test_the_unattributed_tripwire_is_loud(self):
        html = self._html()
        self.assertIn("UNATTRIBUTED", html)
        self.assertIn("banner", html)

    def test_says_when_a_window_spans_two_rate_cards(self):
        html = self._html()
        self.assertIn("2 rate cards", html)
        self.assertIn(_CARD_A, html)
        self.assertIn(_CARD_B, html)

    def test_a_failed_request_costs_us_but_charges_zero(self):
        """The rule that has to survive every rewrite of this page: a
        request that failed cost us real money and the customer sees
        none of it."""
        html = self._html()
        row = next(
            tr for tr in re.findall(r"<tr>.*?</tr>", html) if self.bad_request[:8] in tr
        )
        self.assertIn("provider_failure", row)
        self.assertIn("¥0.90", row, "we still spent it")
        self.assertIn("¥0.00", row, "and still charged nothing for it")
        self.assertIn("shadow only", html)

    def test_states_that_it_does_not_read_agentllmcall(self):
        """Two tables describing the same calls; adding them double
        counts. The page says so rather than leaving the next reader to
        rediscover it."""
        html = self._html()
        self.assertIn("never reads", html)
        self.assertIn("double count", html)

    def test_markup_is_balanced(self):
        html = self._html()
        for tag in ("div", "table", "tbody", "tr", "td", "svg"):
            self.assertEqual(
                len(re.findall(rf"<{tag}[ >]", html)),
                len(re.findall(rf"</{tag}>", html)),
                f"unbalanced <{tag}>",
            )


class EmptyStateTests(TestCase):
    """Nothing recorded is the DEFAULT state — the flag ships off — so it
    is the first thing most people will see."""

    @override_settings(SEARCH_ENGINE={"AI_COST_METER": False})
    def test_an_empty_page_blames_the_flag_when_the_flag_is_off(self):
        html = cost_dashboard.render_html(cost_dashboard.collect(days=7))
        self.assertIn("AI_COST_METER", html)
        self.assertIn("jobs.tf", html, "the crons are the half that gets forgotten")

    @override_settings(SEARCH_ENGINE={"AI_COST_METER": True})
    def test_an_empty_page_does_not_blame_the_flag_when_it_is_on(self):
        html = cost_dashboard.render_html(cost_dashboard.collect(days=7))
        self.assertIn("this window is empty", html)


class CommandTests(_Fixture):
    def test_writes_a_file_and_summarises_to_stderr(self):
        import tempfile  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "nested" / "dash.html"
            err = StringIO()
            call_command("ai_cost_dashboard", "--days", "1", "-o", str(out), stderr=err)
            self.assertTrue(out.exists())
            self.assertIn("<!doctype html>", out.read_text(encoding="utf-8"))
            # The unattributed warning has to reach the terminal too: an
            # operator running this from a cron log will not open the file.
            self.assertIn("UNATTRIBUTED", err.getvalue())

    def test_stdout_mode_emits_only_the_document(self):
        out, err = StringIO(), StringIO()
        call_command("ai_cost_dashboard", "--days", "1", "--stdout", stdout=out, stderr=err)
        self.assertTrue(out.getvalue().startswith("<!doctype html>"))
        self.assertTrue(out.getvalue().rstrip().endswith("</html>"))
