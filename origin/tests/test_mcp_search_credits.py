"""MCP `search_workspace` spends the caller's AI credits (MIP-1965).

Ken's decision (2026-11-22): the paid AI steps of a search from a connected app (query rewrite and
rerank) are charged to the caller's AI credits like an in-app question, under the billable surface
`mcp_search`. With no credits left the search still answers, as a plain search (no rewrite, no
rerank, no model call), with one short neutral line in the result. It is never refused for credits.
There is no separate daily cap on the tool (the call limits per minute and per day stay).

No model is used: `search()` is a stub that records one priced LLM call, the way the rewriter does.
"""

from __future__ import annotations

import uuid
from unittest import mock

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.test import override_settings

from origin.search_engine import credit_ledger, quota
from origin.search_engine.agent.tools import search_kb
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage
from origin.search_engine.models import AiCreditEntry, AiRequestCost
from origin.tests.spend_limit_support import over_the_monthly_budget
from origin.views.public.mcp import mcp_search_credits

from .test_mcp_endpoint import McpBase

RESULTS = {
    "results": [
        {
            "entity_type": "task",
            "entity_id": "task:1",
            "title": "T1",
            "snippet": "s1",
            "chunks": [{"chunk_id": "c1", "text": "body one"}],
            "task_id": "1",
            "project_id": "3",
        }
    ]
}


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


AUTHORITATIVE = dict(AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=True)
SHADOW_ONLY = dict(AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=False)


def _paid_search(calls):
    """A `search()` stand-in: records the kwargs, and spends like the rewrite does unless plain."""

    def fake(**kwargs):
        calls.append(kwargs)
        if kwargs.get("rewrite") and kwargs.get("overlays") is not False:
            usage = CallUsage(provider="gemini", model="gemini-3.6-flash")
            usage.prompt_tokens = 100_000
            spend.record_llm_call(usage)
        return RESULTS

    return fake


class _Base(McpBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self._key()
        self.uid = str(self.user.id)

    def drain(self):
        quota.invalidate_effective_tier([self.uid])
        plan = quota.get_effective_tier(self.uid)
        credit_ledger.ensure_monthly_grant(self.uid, plan)
        total = credit_ledger.balance_milli(self.uid, plan)
        credit_ledger.post_charge(
            request_id=str(uuid.uuid4()), user_id=self.uid, credits_milli=total
        )
        cache.clear()

    def search(self, calls, **args):
        with mock.patch.object(search_kb, "search", side_effect=_paid_search(calls)):
            return self._tool_text(
                self._call("search_workspace", {"query": "login", "rewrite": True, **args})
            )


class ChargedTests(_Base):
    def test_a_search_is_charged_once_to_the_ledger_under_mcp_search(self):
        with override_settings(SEARCH_ENGINE=_se(**AUTHORITATIVE)):
            calls: list = []
            text, is_error = self.search(calls)
        self.assertFalse(is_error)
        self.assertTrue(calls[0]["rewrite"])
        row = AiRequestCost.objects.get(surface="mcp_search")
        self.assertGreater(row.shadow_credits_milli, 0)
        charge = AiCreditEntry.objects.get(entry_type="charge")
        self.assertEqual(charge.credits_milli, -row.shadow_credits_milli)
        self.assertEqual(str(charge.request_id), str(row.request_id))
        self.assertNotIn("No AI credits", text)

    def test_two_searches_are_two_charges_and_never_more(self):
        with override_settings(SEARCH_ENGINE=_se(**AUTHORITATIVE)):
            self.search([])
            self.search([])
        self.assertEqual(AiCreditEntry.objects.filter(entry_type="charge").count(), 2)

    def test_no_charge_when_the_rewrite_is_off(self):
        """`rewrite: false` and no rerank in CI: nothing was spent, so nothing is charged."""
        with override_settings(SEARCH_ENGINE=_se(**AUTHORITATIVE)):
            self.search([], rewrite=False)
        self.assertEqual(AiCreditEntry.objects.filter(entry_type="charge").count(), 0)

    def test_another_tool_is_not_billed(self):
        with override_settings(SEARCH_ENGINE=_se(**AUTHORITATIVE)):
            self._tool_text(self._call("get_current_user"))
        self.assertFalse(AiRequestCost.objects.filter(surface="mcp_search").exists())
        self.assertEqual(AiCreditEntry.objects.filter(entry_type="charge").count(), 0)


class NoCreditsTests(_Base):
    def test_out_of_credits_runs_a_plain_search_with_a_notice_and_posts_nothing(self):
        with override_settings(SEARCH_ENGINE=_se(**AUTHORITATIVE)):
            self.drain()
            before = AiCreditEntry.objects.filter(entry_type="charge").count()
            calls: list = []
            text, is_error = self.search(calls)
        self.assertFalse(is_error, "never refused for credits")
        self.assertIs(calls[0]["rewrite"], False)
        self.assertIs(calls[0]["overlays"], False)
        self.assertIn(mcp_search_credits.NOTICE, text)
        self.assertEqual(text.count(mcp_search_credits.NOTICE), 1)
        self.assertEqual(AiCreditEntry.objects.filter(entry_type="charge").count(), before)
        self.assertFalse(AiRequestCost.objects.filter(surface="mcp_search").exists())
        self.assertIn("T1", text, "the matches still come back")

    def test_a_reached_monthly_limit_runs_a_plain_search_with_its_own_notice(self):
        for credits_mode in (AUTHORITATIVE, SHADOW_ONLY):
            calls: list = []
            with override_settings(SEARCH_ENGINE=_se(**credits_mode)), over_the_monthly_budget():
                text, is_error = self.search(calls)
            self.assertFalse(is_error)
            self.assertIs(calls[0]["rewrite"], False)
            self.assertIs(calls[0]["overlays"], False)
            self.assertIn(mcp_search_credits.MONTHLY_LIMIT_NOTICE, text)
            self.assertNotIn(mcp_search_credits.NOTICE, text)
            self.assertIn("T1", text, "the matches still come back")
        self.assertEqual(AiCreditEntry.objects.filter(entry_type="charge").count(), 0)
        self.assertFalse(AiRequestCost.objects.filter(surface="mcp_search").exists())

    def test_with_credits_the_default_call_is_untouched(self):
        with override_settings(SEARCH_ENGINE=_se(**AUTHORITATIVE)):
            calls: list = []
            self.search(calls)
        self.assertNotIn("overlays", calls[0])

    def test_shadow_only_never_degrades(self):
        with override_settings(SEARCH_ENGINE=_se(**SHADOW_ONLY)):
            self.drain()
            calls: list = []
            text, _ = self.search(calls)
        self.assertTrue(calls[0]["rewrite"])
        self.assertNotIn(mcp_search_credits.NOTICE, text)

    def test_a_ledger_failure_falls_back_to_the_full_search(self):
        with override_settings(SEARCH_ENGINE=_se(**AUTHORITATIVE)):
            with mock.patch.object(credit_ledger, "balance_milli", side_effect=RuntimeError):
                calls: list = []
                text, is_error = self.search(calls)
        self.assertFalse(is_error)
        self.assertTrue(calls[0]["rewrite"])
        self.assertNotIn(mcp_search_credits.NOTICE, text)

    def test_unlimited_plan_is_never_degraded(self):
        with override_settings(SEARCH_ENGINE=_se(**AUTHORITATIVE)):
            with mock.patch.object(credit_ledger, "balance_milli", return_value=None):
                calls: list = []
                self.search(calls)
        self.assertTrue(calls[0]["rewrite"])


class UsageLineTests(_Base):
    def test_the_credits_block_reports_what_searches_spent_apart_from_asks(self):
        from origin.search_engine.agent_views import _credits_block  # noqa: PLC0415

        with override_settings(SEARCH_ENGINE=_se(**AUTHORITATIVE)):
            self.search([])
            cache.clear()
            plan = quota.get_effective_tier(self.uid)
            block = _credits_block(self.uid, plan)
        spent = -AiCreditEntry.objects.get(entry_type="charge").credits_milli
        self.assertAlmostEqual(block["mcp_search_used"], round(spent / 1000, 2))
        self.assertGreater(block["used"], 0)
