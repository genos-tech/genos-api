"""The monthly AI spend limits as a soft stop (MIP-2363).

`spend_limits.monthly_limit_reached` is tested here. What each AI path does with a yes is tested
beside its zero-credit tests: `test_credit_enforcement` (ask), `test_demo_ai_caps_surfaces` (note
and thread summary), `test_report_ai_narrative`, `test_tier_plan_v2_caps` (What to do today and the
inbox card), `test_schedule_run_skill`, `test_ai_dashboard_cost_guard`, `test_mcp_search_credits`,
`test_ai_cost_report`.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.search_engine import metered, spend_limits
from origin.search_engine.agent_views import _credit_gate
from origin.search_engine.models import AiSpendEvent
from origin.services.report_ai import llm
from origin.tests.spend_limit_support import spend_row

TEAM = str(uuid.uuid4())
OTHER_TEAM = str(uuid.uuid4())


def limits(*, budget_jpy=0.0, team_usd=0.0, **extra):
    return override_settings(
        SEARCH_ENGINE={
            **settings.SEARCH_ENGINE,
            "AI_MONTHLY_BUDGET_JPY": budget_jpy,
            "AI_TEAM_MONTHLY_CEILING_USD": team_usd,
            **extra,
        }
    )


class _Base(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


class LimitTests(_Base):
    def test_off_by_default_and_free_when_off(self):
        spend_row(team_id=TEAM, jpy_milli=10**12, usd_micro=10**12)
        with limits(), self.assertNumQueries(0):
            self.assertFalse(spend_limits.monthly_limit_reached(TEAM))

    def test_the_budget_counts_every_surface_and_team(self):
        spend_row(team_id=TEAM, jpy_milli=600_000)
        spend_row(team_id=OTHER_TEAM, jpy_milli=399_999, surface="index")
        with limits(budget_jpy=1000):
            self.assertFalse(spend_limits.monthly_limit_reached(TEAM))
            cache.clear()
            spend_row(jpy_milli=1, surface="eval")
            self.assertTrue(spend_limits.monthly_limit_reached(TEAM))
            self.assertTrue(spend_limits.monthly_limit_reached(""))

    def test_the_team_ceiling_stops_only_that_team(self):
        spend_row(team_id=TEAM, usd_micro=2_000_000)
        with limits(team_usd=2.0):
            self.assertTrue(spend_limits.monthly_limit_reached(TEAM))
            self.assertTrue(spend_limits.monthly_limit_reached(uuid.UUID(TEAM)))
            self.assertFalse(spend_limits.monthly_limit_reached(OTHER_TEAM))
            self.assertFalse(spend_limits.monthly_limit_reached(""))
            self.assertFalse(spend_limits.monthly_limit_reached(None))

    def test_any_spelling_of_the_team_id_is_the_same_team(self):
        """MIP-2450: a view takes the team id from the request body."""
        spellings = (TEAM.upper(), TEAM.replace("-", ""), "{" + TEAM + "}", uuid.UUID(TEAM))
        for spelling in spellings:
            # What the ledger is written with, for an ask that sent this spelling.
            self.assertEqual(metered.spend_kwargs_for("ask", team_id=spelling)["team_id"], TEAM)
        spend_row(team_id=TEAM, usd_micro=2_000_000)
        with limits(team_usd=2.0):
            for spelling in spellings:
                cache.clear()
                self.assertTrue(spend_limits.monthly_limit_reached(str(spelling)), spelling)
        for nothing in ("", None):
            self.assertEqual(metered.spend_kwargs_for("ask", team_id=nothing)["team_id"], "")
        self.assertEqual(spend_limits.canonical_team_id("not-a-uuid"), "not-a-uuid")

    def test_last_month_does_not_count(self):
        row = spend_row(team_id=TEAM, jpy_milli=10**9, usd_micro=10**9)
        first = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        AiSpendEvent.objects.filter(pk=row.pk).update(created_at=first - timedelta(seconds=1))
        with limits(budget_jpy=1, team_usd=1):
            self.assertFalse(spend_limits.monthly_limit_reached(TEAM))

    def test_the_totals_are_cached(self):
        with limits(budget_jpy=1000, team_usd=1000):
            self.assertFalse(spend_limits.monthly_limit_reached(TEAM))
            with self.assertNumQueries(0):
                self.assertFalse(spend_limits.monthly_limit_reached(TEAM))

    def test_it_fails_open(self):
        with (
            limits(budget_jpy=1),
            mock.patch.object(spend_limits, "month_spend_jpy_milli", side_effect=RuntimeError),
        ):
            self.assertFalse(spend_limits.monthly_limit_reached(TEAM))

    def test_the_alert_is_a_warning_once_not_an_error_per_call(self):
        spend_row(jpy_milli=2_000_000)
        with limits(budget_jpy=1000), self.assertLogs(spend_limits.log, "WARNING") as logs:
            for _ in range(3):
                self.assertTrue(spend_limits.monthly_limit_reached(TEAM))
        self.assertEqual([r.levelname for r in logs.records], ["WARNING"])


class ChecksTests(_Base):
    """The two zero-credit checks that stand in front of a model call (the MCP one is with its
    tests): each says "monthly limit", whatever the credits settings and the plan are."""

    UID = str(uuid.uuid4())

    def test_the_ask_and_summary_gate(self):
        spend_row(team_id=TEAM, usd_micro=2_000_000)
        for authoritative in (True, False):
            with limits(team_usd=1.0, AI_CREDITS_AUTHORITATIVE=authoritative):
                for plan in ("free", "pro", "enterprise"):
                    resp = _credit_gate(self.UID, plan, TEAM)
                    self.assertEqual(resp.status_code, 429, plan)
                    self.assertEqual(resp.data["category"], "ai_monthly_limit")
                    self.assertTrue(resp.data["limit_reached"])
                    self.assertEqual(resp.data["error"], spend_limits.MONTHLY_LIMIT_MESSAGE)
                    self.assertNotIn("credits_remaining", resp.data)
                self.assertIsNone(_credit_gate(self.UID, "enterprise", OTHER_TEAM))

    def test_the_message_names_no_money_and_sells_nothing(self):
        for word in ("¥", "$", "yen", "budget", "buy", "upgrade", "credit"):
            self.assertNotIn(word, spend_limits.MONTHLY_LIMIT_MESSAGE.lower())

    def test_the_report_and_skill_check(self):
        spend_row(team_id=TEAM, usd_micro=2_000_000)
        with limits(team_usd=1.0, AI_CREDITS_AUTHORITATIVE=False):
            self.assertEqual(llm.credit_block(self.UID, TEAM), llm.SKIP_MONTHLY_LIMIT)
            self.assertEqual(llm.credit_block(self.UID, OTHER_TEAM), "")
