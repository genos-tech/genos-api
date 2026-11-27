"""MIP-2364: a demo visitor's other AI surfaces count in the demo daily caps.

`check_demo_ask` (MIP-2341) counted only `/agent/ask/`. Every other place where a demo user's action
makes a paid model call now counts too, on the cache-miss path only (a cached answer makes no call):

  * thread and note summaries (`/agent/thread-summary/`, `/agent/note-summary/`): a 429 with the demo
    message, as the surfaces already answer a spent quota; the panel stays usable;
  * scheduled reports (narrative, per-task sentences) and scheduled skill runs: no call, the report
    files plain and says why (`demo_limit`); they have no request, so only the global cap applies;
  * MCP `search_workspace`: still answers, plain (no rewrite, no rerank).

`/agent/decide/` resumes a run an ask already counted, and Spotlight typeahead only embeds a cached
query, so neither counts. A normal user is never counted, and costs no counter access.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework_simplejwt.tokens import RefreshToken

from origin.search_engine.quota import resolve_effective_tier
from origin.services import demo_ai_limits
from origin.services.report_ai import llm
from origin.services.schedule_jobs import run_skill
from origin.tests.spend_limit_support import over_the_monthly_budget, over_the_team_ceiling
from origin.tests.test_demo_ai_limits import _make_user
from origin.views.public.mcp import views as mcp_views

NOTE_URL = "/api/v2/agent/note-summary/"
THREAD_URL = "/api/v2/agent/thread-summary/"


class _Base(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.demo, self.demo_team = _make_user(demo=True, name="visitor")
        self.pro, self.pro_team = _make_user(demo=False, name="payer")

    def client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
        return client


def _count(day=None):
    """The global demo counter today (0 when nothing was counted)."""
    return cache.get(f"demo_ai:global:{day or demo_ai_limits._day()}") or 0


class RequestlessCallTests(_Base):
    def test_a_demo_call_with_no_request_counts_only_the_global_cap(self):
        with patch.object(demo_ai_limits, "DEMO_AI_PER_IP_DAILY_ASKS", 0):
            # An address cap of 0 would stop everything; with no address it is not consulted.
            self.assertFalse(demo_ai_limits.demo_call_blocked(str(self.demo.id)))
        self.assertEqual(_count(), 1)

    def test_the_global_cap_stops_it(self):
        with patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 1):
            self.assertFalse(demo_ai_limits.demo_call_blocked(str(self.demo.id)))
            self.assertTrue(demo_ai_limits.demo_call_blocked(str(self.demo.id)))

    def test_it_is_stopped_when_the_counter_store_is_down(self):
        with patch("origin.services.throttles.throttle_cache", side_effect=OSError("down")):
            self.assertTrue(demo_ai_limits.demo_call_blocked(str(self.demo.id)))

    def test_a_normal_user_is_never_counted_or_stopped_and_costs_no_query(self):
        uid = str(self.pro.id)
        resolve_effective_tier(uid)  # the tier is read (and cached) by the caller anyway
        with (
            patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0),
            patch("origin.services.throttles.throttle_cache", side_effect=OSError("down")),
            self.assertNumQueries(0),
        ):
            self.assertFalse(demo_ai_limits.demo_call_blocked(uid))
            self.assertEqual(llm.demo_block(uid), "")

    def test_an_unknown_user_is_not_a_demo_user(self):
        self.assertFalse(demo_ai_limits.demo_call_blocked(None))
        self.assertEqual(_count(), 0)


class ReportAndSkillTests(_Base):
    def test_the_report_skip_code_is_demo_limit_for_a_capped_demo_user_only(self):
        with patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0):
            self.assertEqual(llm.demo_block(str(self.demo.id)), llm.SKIP_DEMO_LIMIT)
            self.assertEqual(llm.demo_block(str(self.pro.id)), "")
        self.assertEqual(llm.SKIP_DEMO_LIMIT, "demo_limit")

    def test_a_scheduled_skill_run_is_skipped_for_a_capped_demo_user(self):
        settings_ok = SimpleNamespace(provider="gemini")
        with (
            patch.object(llm, "configured", return_value=True),
            patch.object(llm, "credit_block", return_value=""),
            patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0),
            patch.dict("django.conf.settings.SEARCH_ENGINE", {"AI_COST_METER": True}),
        ):
            self.assertEqual(
                run_skill._skip_code(str(self.demo.id), settings_ok), llm.SKIP_DEMO_LIMIT
            )
            self.assertEqual(run_skill._skip_code(str(self.pro.id), settings_ok), "")
        self.assertIn("free account", run_skill._SKIPPED["demo_limit"])

    def test_a_demo_user_within_the_cap_runs(self):
        with (
            patch.object(llm, "configured", return_value=True),
            patch.object(llm, "credit_block", return_value=""),
            patch.dict("django.conf.settings.SEARCH_ENGINE", {"AI_COST_METER": True}),
        ):
            code = run_skill._skip_code(str(self.demo.id), SimpleNamespace(provider="gemini"))
        self.assertEqual(code, "")
        self.assertEqual(_count(), 1)


class McpSearchTests(_Base):
    def _request(self, user, ip="10.0.0.1"):
        request = APIRequestFactory().post("/api/public/v1/mcp", {}, REMOTE_ADDR=ip)
        request.user = user
        return request

    def test_a_capped_demo_search_runs_plain(self):
        with patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0):
            self.assertTrue(mcp_views._demo_search_capped(self._request(self.demo)))

    def test_a_demo_search_within_the_cap_keeps_its_model_steps_and_counts_once(self):
        self.assertFalse(mcp_views._demo_search_capped(self._request(self.demo)))
        self.assertEqual(_count(), 1)

    def test_a_normal_user_and_a_keyless_request_are_never_capped(self):
        with patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0):
            self.assertFalse(mcp_views._demo_search_capped(self._request(self.pro)))
            self.assertFalse(mcp_views._demo_search_capped(SimpleNamespace(user=None)))
        self.assertEqual(_count(), 0)


class SummaryViewTests(_Base):
    """The summary endpoints, with the cache and the model stubbed."""

    def _note(self, user, team, *, cached):
        record = SimpleNamespace(title="T", body_text="body")
        hit = (
            SimpleNamespace(
                summary="s",
                last_updated=MagicMock(isoformat=lambda: "x"),
                body_length=4,
                fingerprint="f",
            )
            if cached
            else None
        )
        regen = MagicMock(
            return_value=SimpleNamespace(
                summary="fresh",
                last_updated=MagicMock(isoformat=lambda: "x"),
                body_length=4,
                fingerprint="f",
            )
        )
        with (
            patch(
                "origin.search_engine.agent_views.peek_cached_note_summary",
                return_value=(hit, record, "f"),
            ),
            patch("origin.search_engine.agent_views.regenerate_note_summary", regen),
            patch("origin.search_engine.agent_views._note_session_payload", return_value={}),
        ):
            resp = self.client_for(user).post(
                NOTE_URL,
                {"team_id": str(team.pk), "note_type": 1, "note_id": 1},
                format="json",
                HTTP_HOST="localhost",
            )
        return resp, regen

    def _thread(self, user, team, *, cached):
        hit = (
            SimpleNamespace(
                summary="s",
                last_updated=MagicMock(isoformat=lambda: "x"),
                message_count=2,
                fingerprint="f",
            )
            if cached
            else None
        )
        regen = MagicMock(
            return_value=SimpleNamespace(
                summary="fresh",
                last_updated=MagicMock(isoformat=lambda: "x"),
                message_count=2,
                fingerprint="f",
            )
        )
        with (
            patch(
                "origin.search_engine.agent_views.peek_cached_summary",
                return_value=(hit, [object()], "f"),
            ),
            patch("origin.search_engine.agent_views.regenerate_summary", regen),
            patch("origin.search_engine.agent_views._thread_session_payload", return_value={}),
        ):
            resp = self.client_for(user).post(
                THREAD_URL,
                {"team_id": str(team.pk), "chat_type": 1, "chat_id": "c", "thread_id": "t"},
                format="json",
                HTTP_HOST="localhost",
            )
        return resp, regen

    def test_a_capped_demo_user_gets_the_demo_message_and_no_model_call(self):
        for call in (self._note, self._thread):
            with patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0):
                resp, regen = call(self.demo, self.demo_team, cached=False)
            self.assertEqual(resp.status_code, 429, call.__name__)
            self.assertEqual(resp.json()["category"], "demo_limit")
            regen.assert_not_called()

    def test_a_cache_hit_is_free_and_never_counted_even_when_capped(self):
        for call in (self._note, self._thread):
            with patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0):
                resp, regen = call(self.demo, self.demo_team, cached=True)
            self.assertEqual(resp.status_code, 200, call.__name__)
            self.assertIs(resp.json()["generated"], False)
            regen.assert_not_called()
        self.assertEqual(_count(), 0)

    def test_a_regeneration_within_the_cap_is_served_and_counted_once(self):
        for n, call in enumerate((self._note, self._thread), start=1):
            resp, regen = call(self.demo, self.demo_team, cached=False)
            self.assertEqual(resp.status_code, 200, call.__name__)
            self.assertIs(resp.json()["generated"], True)
            regen.assert_called_once()
            self.assertEqual(_count(), n)

    def test_a_reached_monthly_limit_refuses_a_new_summary_and_keeps_the_cached_one(self):
        """MIP-2363: the zero-credit way out for note and thread summaries."""
        for call in (self._note, self._thread):
            with over_the_monthly_budget():
                resp, regen = call(self.pro, self.pro_team, cached=False)
                kept, _ = call(self.pro, self.pro_team, cached=True)
            self.assertEqual(resp.status_code, 429, call.__name__)
            self.assertEqual(resp.json()["category"], "ai_monthly_limit")
            regen.assert_not_called()
            self.assertEqual(kept.status_code, 200, call.__name__)

    def test_the_team_ceiling_reaches_both_summaries_of_that_team_only(self):
        for call in (self._note, self._thread):
            with over_the_team_ceiling(self.pro_team.pk):
                resp, regen = call(self.pro, self.pro_team, cached=False)
            self.assertEqual(resp.json()["category"], "ai_monthly_limit", call.__name__)
            regen.assert_not_called()
            with over_the_team_ceiling(self.demo_team.pk):
                resp, _ = call(self.pro, self.pro_team, cached=False)
            self.assertEqual(resp.status_code, 200, call.__name__)

    def test_a_normal_user_is_served_while_the_demo_cap_is_zero_and_never_counted(self):
        with (
            patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0),
            patch.object(demo_ai_limits, "DEMO_AI_PER_IP_DAILY_ASKS", 0),
        ):
            for call in (self._note, self._thread):
                resp, regen = call(self.pro, self.pro_team, cached=False)
                self.assertEqual(resp.status_code, 200, call.__name__)
                regen.assert_called_once()
        self.assertEqual(_count(), 0)
