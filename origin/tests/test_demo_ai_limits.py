"""MIP-2341: a demo visitor cannot spend real AI credits.

`DemoSignInView` makes a fresh `is_demo` user in a fresh Pro team on every call, so before this a
visitor had a new 70-credit allowance and an open model picker (Claude Opus). Pinned here:

  1. a demo user's whole credit budget is `DEMO_AI_CREDITS`, served by the ordinary credit gate;
  2. a demo user runs on the default Gemini model at the default effort, whatever they saved;
  3. all demo asks together, and one caller address, have a daily cap that holds when the counter
     store is down (fail closed) and never touches anyone who is not a demo user;
  4. a normal Pro user is unchanged.
"""

from __future__ import annotations

from unittest.mock import patch

from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine import credit_ledger
from origin.search_engine.agent_views import _credit_gate, _credits_block, _resolve_choice_for
from origin.search_engine.quota import get_auto_effort, get_max_effort
from origin.services import demo_ai_limits
from origin.tests.transaction_support import CheapFlushTransactionTestCase


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


AUTHORITATIVE = override_settings(
    SEARCH_ENGINE=_se(AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=True)
)
EFFORT_LEVELS_ON = override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=True))
EFFORT_LEVELS_OFF = override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=False))
AUTHORITATIVE_AND_EFFORT = override_settings(
    SEARCH_ENGINE=_se(
        AI_COST_METER=True,
        AI_CREDITS_SHADOW=True,
        AI_CREDITS_AUTHORITATIVE=True,
        AGENT_EFFORT_LEVELS=True,
    )
)


def _make_user(*, demo: bool, name: str, plan: str = "pro"):
    """A user in a team on `plan`, the shape `DemoSignInView` makes (a Pro team) or a normal Pro user."""
    user = get_user_model().objects.create_user(
        username=name, email=f"{name}@example.com", password="x", is_demo=demo
    )
    team = TeamMaster.objects.create(
        team_name=f"{name} team", team_email=f"{name}@team.com", owner=user, plan=plan, is_demo=demo
    )
    TeamMembers.objects.create(team=team, attendee=user)
    return user, team


def _request(ip: str = "10.0.0.1"):
    return APIRequestFactory().post("/api/v2/agent/ask/", {}, format="json", REMOTE_ADDR=ip)


class _Base(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.demo, self.demo_team = _make_user(demo=True, name="visitor")
        self.pro, self.pro_team = _make_user(demo=False, name="payer")


class DemoCreditBudgetTests(_Base):
    @AUTHORITATIVE
    def test_a_demo_user_gets_the_small_fixed_budget_and_a_pro_user_keeps_pro(self):
        uid, pid = str(self.demo.id), str(self.pro.id)
        self.assertEqual(
            credit_ledger.balance_milli(uid, "pro"), demo_ai_limits.DEMO_AI_CREDITS * 1000
        )
        self.assertEqual(
            credit_ledger.balance_milli(pid, "pro"),
            dj_settings.CREDIT_POLICY.entitlements_milli["pro"],
        )

    @AUTHORITATIVE
    def test_the_demo_grant_row_is_labelled_demo(self):
        from origin.search_engine.models import AiCreditEntry

        credit_ledger.balance_milli(str(self.demo.id), "pro")
        row = AiCreditEntry.objects.get(user_id=str(self.demo.id))
        self.assertEqual(row.plan, credit_ledger.DEMO_PLAN)
        self.assertEqual(row.credits_milli, demo_ai_limits.DEMO_AI_CREDITS * 1000)

    @AUTHORITATIVE
    def test_the_same_gate_refuses_a_demo_user_once_the_budget_is_spent(self):
        uid = str(self.demo.id)
        self.assertIsNone(_credit_gate(uid, "pro"))
        credit_ledger.post_charge(
            request_id="11111111-1111-1111-1111-111111111111",
            user_id=uid,
            credits_milli=demo_ai_limits.DEMO_AI_CREDITS * 1000,
        )
        cache.clear()
        refusal = _credit_gate(uid, "pro")
        self.assertIsNotNone(refusal)
        self.assertEqual(refusal.status_code, 429)
        self.assertEqual(refusal.data["category"], "ai_credits")
        self.assertEqual(refusal.data["credits_limit"], float(demo_ai_limits.DEMO_AI_CREDITS))
        self.assertIn("free account", refusal.data["error"])

    @AUTHORITATIVE
    def test_a_pro_user_is_not_refused_after_spending_what_the_demo_budget_would_cover(self):
        pid = str(self.pro.id)
        credit_ledger.post_charge(
            request_id="22222222-2222-2222-2222-222222222222",
            user_id=pid,
            credits_milli=demo_ai_limits.DEMO_AI_CREDITS * 1000,
        )
        cache.clear()
        self.assertIsNone(_credit_gate(pid, "pro"))

    @AUTHORITATIVE
    def test_the_credits_block_shows_the_demo_budget(self):
        block = _credits_block(str(self.demo.id), "pro")
        self.assertEqual(block["limit"], float(demo_ai_limits.DEMO_AI_CREDITS))
        self.assertEqual(block["balance"], float(demo_ai_limits.DEMO_AI_CREDITS))
        pro_block = _credits_block(str(self.pro.id), "pro")
        self.assertEqual(pro_block["limit"], 70.0)


class DemoModelPinTests(_Base):
    def _saved(self, user, provider="claude", effort="high", model="claude-opus"):
        user.preferred_llm_provider = provider
        user.preferred_llm_effort = effort
        user.preferred_llm_model = model
        user.save()
        return user

    @EFFORT_LEVELS_ON
    def test_a_demo_user_cannot_run_claude_or_high_effort_with_effort_levels_on(self):
        choice = _resolve_choice_for(self._saved(self.demo))
        self.assertEqual(choice.provider, "gemini")
        self.assertEqual(choice.effort, "low")

    @EFFORT_LEVELS_OFF
    def test_a_demo_user_cannot_run_claude_with_effort_levels_off(self):
        choice = _resolve_choice_for(self._saved(self.demo))
        self.assertEqual(choice.provider, "gemini")
        self.assertEqual(choice.model, dj_settings.SEARCH_ENGINE["GEMINI_MODEL"])
        self.assertNotIn("claude", choice.model)

    @EFFORT_LEVELS_ON
    def test_a_normal_pro_user_keeps_their_choice(self):
        choice = _resolve_choice_for(self._saved(self.pro))
        self.assertEqual(choice.provider, "claude")
        self.assertEqual(choice.effort, "high")

    def test_the_ceiling_and_auto_routing_are_closed_for_a_demo_user_only(self):
        self.assertEqual(get_max_effort(str(self.demo.id)), demo_ai_limits.DEMO_AI_MAX_EFFORT)
        self.assertFalse(get_auto_effort(str(self.demo.id)))
        self.assertEqual(get_max_effort(str(self.pro.id)), "high")
        self.assertTrue(get_auto_effort(str(self.pro.id)))

    def test_saving_a_preference_as_a_demo_user_stores_nothing(self):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.demo).access_token}"
        )
        resp = client.patch(
            "/api/v2/user/preferences/llm-model/",
            {"provider": "claude", "effort": "high"},
            format="json",
            HTTP_HOST="localhost",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.demo.refresh_from_db()
        self.assertEqual(self.demo.preferred_llm_provider, "")
        self.assertEqual(self.demo.preferred_llm_effort, "")

    def test_saving_a_preference_as_a_normal_user_still_works(self):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.pro).access_token}"
        )
        resp = client.patch(
            "/api/v2/user/preferences/llm-model/",
            {"provider": "claude", "effort": "high"},
            format="json",
            HTTP_HOST="localhost",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.pro.refresh_from_db()
        self.assertEqual(self.pro.preferred_llm_provider, "claude")
        self.assertEqual(self.pro.preferred_llm_effort, "high")


class DemoDailyCapTests(_Base):
    def test_one_address_is_stopped_after_its_cap(self):
        with patch.object(demo_ai_limits, "DEMO_AI_PER_IP_DAILY_ASKS", 2):
            self.assertIsNone(demo_ai_limits.check_demo_ask(_request(), self.demo))
            self.assertIsNone(demo_ai_limits.check_demo_ask(_request(), self.demo))
            refusal = demo_ai_limits.check_demo_ask(_request(), self.demo)
            # Another address is not held by the first one's count.
            other = demo_ai_limits.check_demo_ask(_request("10.0.0.2"), self.demo)
        self.assertEqual(refusal.status_code, 429)
        self.assertEqual(refusal.data["category"], "demo_limit")
        self.assertEqual(refusal.data["reason"], "ip")
        self.assertIn("Retry-After", refusal)
        self.assertIsNone(other)

    def test_the_global_cap_stops_every_demo_ask_whatever_the_address(self):
        with patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 2):
            self.assertIsNone(demo_ai_limits.check_demo_ask(_request("10.0.0.1"), self.demo))
            self.assertIsNone(demo_ai_limits.check_demo_ask(_request("10.0.0.2"), self.demo))
            refusal = demo_ai_limits.check_demo_ask(_request("10.0.0.3"), self.demo)
        self.assertEqual(refusal.status_code, 429)
        self.assertEqual(refusal.data["reason"], "global")

    def test_a_normal_user_is_never_counted_or_stopped(self):
        with (
            patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0),
            patch.object(demo_ai_limits, "DEMO_AI_PER_IP_DAILY_ASKS", 0),
            patch("origin.services.throttles.throttle_cache", side_effect=OSError("down")),
        ):
            self.assertIsNone(demo_ai_limits.check_demo_ask(_request(), self.pro))

    def test_a_demo_ask_is_refused_when_the_counter_store_is_down(self):
        with patch("origin.services.throttles.throttle_cache", side_effect=OSError("down")):
            refusal = demo_ai_limits.check_demo_ask(_request(), self.demo)
        self.assertEqual(refusal.status_code, 429)
        self.assertEqual(refusal.data["reason"], "unavailable")

    def test_no_raw_address_is_kept_in_the_counter_key(self):
        demo_ai_limits.check_demo_ask(_request("203.0.113.9"), self.demo)
        keys = [k for k in getattr(cache, "_cache", {}) if "demo_ai" in str(k)]
        for key in keys:
            self.assertNotIn("203.0.113.9", str(key))


class DemoAskEndToEndTests(CheapFlushTransactionTestCase):
    """Through `/agent/ask/`: the cap stops a demo ask before anything runs; a Pro user is served."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.demo, self.demo_team = _make_user(demo=True, name="visitor")
        self.pro, self.pro_team = _make_user(demo=False, name="payer")

    def _ask(self, user, team):
        ran = []

        def fake_run_agent(query, ctx, emit, **kwargs):
            ran.append(query)
            emit({"type": "answer_delta", "text": "hi"})
            emit({"type": "done"})
            from django.db import connections  # noqa: PLC0415

            connections.close_all()

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
        with (
            patch("origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent),
            patch("origin.search_engine.ingestion.ingest_conversation_run", return_value=False),
        ):
            resp = client.post(
                "/api/v2/agent/ask/",
                {"query": "q", "team_id": str(team.pk)},
                format="json",
                HTTP_HOST="localhost",
            )
            if resp.status_code == 200:
                b"".join(resp.streaming_content)
        return resp, ran

    def test_the_global_cap_stops_a_demo_ask_before_the_agent_runs(self):
        with patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0):
            resp, ran = self._ask(self.demo, self.demo_team)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["category"], "demo_limit")
        self.assertEqual(ran, [])

    def test_a_pro_user_is_served_while_the_demo_cap_is_zero(self):
        with (
            patch.object(demo_ai_limits, "DEMO_AI_GLOBAL_DAILY_ASKS", 0),
            patch.object(demo_ai_limits, "DEMO_AI_PER_IP_DAILY_ASKS", 0),
        ):
            resp, ran = self._ask(self.pro, self.pro_team)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ran, ["q"])

    def test_a_demo_ask_within_the_caps_is_served(self):
        resp, ran = self._ask(self.demo, self.demo_team)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ran, ["q"])

    @AUTHORITATIVE_AND_EFFORT
    def test_the_agent_runs_on_gemini_low_for_a_demo_user_who_saved_claude_high(self):
        from origin.search_engine.llm.choice import get_llm_choice

        self.demo.preferred_llm_provider = "claude"
        self.demo.preferred_llm_effort = "high"
        self.demo.save()
        seen = []
        original = get_llm_choice

        def spy_run_agent(query, ctx, emit, **kwargs):
            seen.append(original())
            emit({"type": "done"})
            from django.db import connections  # noqa: PLC0415

            connections.close_all()

        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.demo).access_token}"
        )
        with (
            patch("origin.search_engine.agent_views.run_agent", side_effect=spy_run_agent),
            patch("origin.search_engine.ingestion.ingest_conversation_run", return_value=False),
        ):
            resp = client.post(
                "/api/v2/agent/ask/",
                {"query": "q", "team_id": str(self.demo_team.pk)},
                format="json",
                HTTP_HOST="localhost",
            )
            self.assertEqual(resp.status_code, 200)
            b"".join(resp.streaming_content)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].provider, "gemini")
        self.assertEqual(seen[0].effort, "low")
