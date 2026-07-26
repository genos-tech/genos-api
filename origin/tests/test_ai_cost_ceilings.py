"""The three AI safety ceilings, at three deliberate strengths.

  per-request cost  -> BLOCKS
  per-user daily    -> alerts only
  kill switch       -> refuses new asks

The asymmetry is the design, not an oversight. A runaway single request
is a bug we should stop mid-flight; an expensive user is information we
want, and the metering strategy is explicit that blocking an early user
costs more than their spend does. Everything fails OPEN — a check that
cannot run must never block a paying user.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from origin.search_engine.llm import spend
from origin.search_engine.models import AiSpendEvent


def _se(**overrides):
    from django.conf import settings as dj

    cfg = dict(dj.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class RequestCeilingAccountingTests(SimpleTestCase):
    def test_cost_accumulates_on_the_active_context(self):
        with spend.spend_context(surface="ask") as ctx:
            self.assertEqual(spend.request_cost_jpy_milli(), 0)
            ctx.cost_jpy_milli += 250
            self.assertEqual(spend.request_cost_jpy_milli(), 250)

    def test_reads_zero_with_no_context(self):
        # Fails open: an unbound path can never trip a ceiling.
        self.assertEqual(spend.request_cost_jpy_milli(), 0)

    def test_ceiling_hit_is_flagged_for_the_rollup(self):
        with spend.spend_context(surface="ask") as ctx:
            self.assertFalse(ctx.ceiling_hit)
            spend.mark_ceiling_hit()
            self.assertTrue(ctx.ceiling_hit)


class LoopCeilingTests(TestCase):
    """The blocking control, exercised through the real loop."""

    def _drive(self, **se):
        from origin.search_engine.agent.controller import _drive_loop
        from origin.search_engine.agent.tools import ToolContext

        events = []
        with override_settings(SEARCH_ENGINE=_se(**se)):
            return (
                _drive_loop(
                    messages=[],
                    ctx=ToolContext(team_id="t1", user_id="u1"),
                    emit=events.append,
                    run_id=None,
                    starting_step=0,
                    seen_sources_by_id={},
                ),
                events,
            )

    @staticmethod
    def _client_that_records(calls):
        """A fake ModelClient counting generate_step, NOT client
        construction. `get_model_client()` runs during loop setup — well
        before the per-step ceiling check — so asserting on it would pass
        whether or not the ceiling worked."""

        def _gen(*_a, **_k):
            calls.append(1)
            raise RuntimeError("stop here")

        return mock.Mock(generate_step=_gen)

    def test_stops_before_the_next_model_call_when_over_the_ceiling(self):
        calls: list[int] = []
        with spend.spend_context(surface="ask") as ctx:
            ctx.cost_jpy_milli = 5000
            with mock.patch(
                "origin.search_engine.agent.controller.get_model_client",
                return_value=self._client_that_records(calls),
            ):
                _, events = self._drive(AI_REQUEST_MAX_JPY_MILLI=1000)

        self.assertEqual(calls, [], "the loop must not call the model past its ceiling")
        self.assertTrue(ctx.ceiling_hit)
        self.assertEqual(events[-1]["type"], "error")

    def test_the_user_message_never_mentions_money(self):
        """The user did not choose a budget and cannot act on a yen
        figure. What they CAN act on is asking something narrower."""
        calls: list[int] = []
        with spend.spend_context(surface="ask") as ctx:
            ctx.cost_jpy_milli = 5000
            with mock.patch(
                "origin.search_engine.agent.controller.get_model_client",
                return_value=self._client_that_records(calls),
            ):
                _, events = self._drive(AI_REQUEST_MAX_JPY_MILLI=1000)

        msg = events[-1]["message"]
        self.assertNotIn("¥", msg)
        self.assertNotIn("cost", msg.lower())
        self.assertIn("narrowing", msg.lower())

    def test_ceiling_of_zero_is_off(self):
        """Default. Nothing is checked, and a request carrying spend
        proceeds to the model exactly as before."""
        calls: list[int] = []
        with spend.spend_context(surface="ask") as ctx:
            ctx.cost_jpy_milli = 999_999
            with mock.patch(
                "origin.search_engine.agent.controller.get_model_client",
                return_value=self._client_that_records(calls),
            ):
                self._drive(AI_REQUEST_MAX_JPY_MILLI=0)
        self.assertEqual(len(calls), 1, "the loop should have reached the model")

    def test_under_the_ceiling_proceeds(self):
        calls: list[int] = []
        with spend.spend_context(surface="ask") as ctx:
            ctx.cost_jpy_milli = 100
            with mock.patch(
                "origin.search_engine.agent.controller.get_model_client",
                return_value=self._client_that_records(calls),
            ):
                self._drive(AI_REQUEST_MAX_JPY_MILLI=1000)
        self.assertEqual(len(calls), 1)

    def test_cancellation_wins_over_the_ceiling(self):
        """Both stop the loop; a disconnect is checked first because it
        needs no settings lookup and is the commoner case."""
        ev = threading.Event()
        ev.set()
        from origin.search_engine.agent.controller import _drive_loop
        from origin.search_engine.agent.tools import ToolContext

        events = []
        with spend.spend_context(surface="ask") as ctx:
            ctx.cost_jpy_milli = 5000
            with override_settings(SEARCH_ENGINE=_se(AI_REQUEST_MAX_JPY_MILLI=1000)):
                _drive_loop(
                    messages=[],
                    ctx=ToolContext(team_id="t1", user_id="u1"),
                    emit=events.append,
                    run_id=None,
                    starting_step=0,
                    seen_sources_by_id={},
                    cancel_event=ev,
                )
        self.assertEqual(events, [], "a cancelled run emits nothing to a gone client")
        self.assertFalse(ctx.ceiling_hit)


class UserDailyAlertTests(TestCase):
    """Observation only. This must never block."""

    def _alert(self, user_id="u1", **se):
        from origin.search_engine.agent_views import _alert_if_user_spend_is_high

        with override_settings(SEARCH_ENGINE=_se(**se)):
            with self.assertLogs("origin.search_engine.agent_views", level="WARNING") as cm:
                _alert_if_user_spend_is_high(user_id)
                return cm.output
        return []

    def _spend(self, user_id="u1", jpy_milli=100_000):
        AiSpendEvent.objects.create(
            request_id="55555555-5555-5555-5555-555555555555",
            user_id=user_id,
            surface="ask",
            provider="gemini",
            model="gemini-3.6-flash",
            cost_jpy_milli=jpy_milli,
            cost_usd_micro=1000,
        )

    def test_warns_when_over_the_threshold(self):
        self._spend(jpy_milli=100_000)  # ¥100
        out = self._alert(AI_USER_DAILY_ALERT_JPY=50)
        self.assertTrue(any("over the" in line for line in out))
        self.assertTrue(any("Not blocked" in line for line in out), "must say it did not block")

    def test_threshold_zero_does_not_even_query(self):
        """Default. Free when unconfigured."""
        from origin.search_engine.agent_views import _alert_if_user_spend_is_high

        with override_settings(SEARCH_ENGINE=_se(AI_USER_DAILY_ALERT_JPY=0)):
            with mock.patch("origin.search_engine.models.AiSpendEvent.objects") as objs:
                _alert_if_user_spend_is_high("u1")
                objs.filter.assert_not_called()

    def test_under_threshold_is_silent(self):
        from origin.search_engine.agent_views import _alert_if_user_spend_is_high

        self._spend(jpy_milli=1_000)  # ¥1
        with override_settings(SEARCH_ENGINE=_se(AI_USER_DAILY_ALERT_JPY=50)):
            with mock.patch("origin.search_engine.agent_views.log") as logger:
                _alert_if_user_spend_is_high("u1")
                logger.warning.assert_not_called()

    def test_a_db_failure_never_propagates(self):
        """Fails open — a broken check must not break an ask."""
        from origin.search_engine.agent_views import _alert_if_user_spend_is_high

        with override_settings(SEARCH_ENGINE=_se(AI_USER_DAILY_ALERT_JPY=1)):
            with mock.patch(
                "origin.search_engine.models.AiSpendEvent.objects.filter",
                side_effect=RuntimeError("db down"),
            ):
                _alert_if_user_spend_is_high("u1")  # must not raise


class CeilingOutcomeClassificationTests(SimpleTestCase):
    def test_a_ceiling_stop_is_an_application_failure_not_a_provider_one(self):
        """We chose to stop. Blaming the provider for our own ceiling
        would make the report's failure attribution actively misleading —
        and the two strings live in one place so they cannot drift into
        doing exactly that."""
        from origin.search_engine.agent.controller import COST_CEILING_MESSAGE
        from origin.search_engine.agent_views import (
            COST_CEILING_MESSAGE as VIEW_COPY,
        )

        self.assertIs(COST_CEILING_MESSAGE, VIEW_COPY)
        self.assertNotIn("¥", COST_CEILING_MESSAGE)


# Real user ids are UUIDs; a non-UUID id raises ValidationError inside
# the quota lookups (user pk is a UUIDField), which the fail-open catch
# would silently swallow — turning a routing test into a no-op.
_UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0001"


class MonthlyCeilingTests(TestCase):
    """The V2 §3.7 financial circuit breaker: per-account monthly YEN
    ceilings from credit_policy.yaml, graded alert → route-cheapest →
    pause, both levers OFF by default. Runs on actual ledger yen,
    independent of anything credits say — a credit-policy bug cannot
    take the business with it."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.addCleanup(cache.clear)

    def _chosen(self, model="gemini-3.6-flash"):
        from origin.search_engine.llm.choice import LlmChoice

        return LlmChoice(provider="gemini", model=model)

    def _spend(self, user_id=_UID, jpy_milli=200_000, team_id=""):
        AiSpendEvent.objects.create(
            request_id="55555555-5555-5555-5555-555555555555",
            user_id=user_id,
            team_id=team_id or None,
            surface="ask",
            provider="gemini",
            model="gemini-3.6-flash",
            cost_jpy_milli=jpy_milli,
            cost_usd_micro=1000,
        )

    def _enforce(self, plan="free", team_id="", chosen=None, **se):
        from origin.search_engine.agent_views import _enforce_monthly_ceilings

        chosen = chosen or self._chosen()
        with override_settings(SEARCH_ENGINE=_se(**se)):
            return _enforce_monthly_ceilings(_UID, plan, team_id, chosen)

    def test_under_the_ceiling_does_nothing(self):
        self._spend(jpy_milli=1_000)  # ¥1 of a ¥150 free ceiling
        with mock.patch("origin.search_engine.agent_views.log") as logger:
            resp, note, chosen = self._enforce()
        self.assertIsNone(resp)
        self.assertIsNone(note)
        self.assertEqual(chosen.model, "gemini-3.6-flash")
        logger.warning.assert_not_called()

    def test_over_the_ceiling_defaults_to_alert_only(self):
        self._spend(jpy_milli=200_000)  # ¥200 > free's ¥150
        with self.assertLogs("origin.search_engine.agent_views", level="WARNING") as cm:
            resp, note, chosen = self._enforce()
        self.assertIsNone(resp, "both levers off — nothing may block")
        self.assertIsNone(note)
        self.assertEqual(chosen.model, "gemini-3.6-flash")
        self.assertTrue(any("monthly AI cost ceiling" in line for line in cm.output))

    def test_pause_lever_429s_with_a_money_free_message(self):
        self._spend(jpy_milli=200_000)
        resp, _note, _chosen = self._enforce(AI_CEILING_PAUSE=True)
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.data["category"], "cost_ceiling")
        self.assertNotIn("¥", resp.data["error"])
        self.assertNotIn("yen", resp.data["error"].lower())

    def test_route_cheapest_steps_down_with_a_fallback_note(self):
        self._spend(jpy_milli=200_000)
        resp, note, chosen = self._enforce(AI_CEILING_ROUTE_CHEAPEST=True)
        self.assertIsNone(resp, "routing serves the ask; it must not block")
        self.assertEqual(chosen.model, "gemini-3.5-flash-lite")
        self.assertEqual(
            note,
            {"requested_model": "gemini-3.6-flash", "used_model": "gemini-3.5-flash-lite"},
            "the same model_fallback note the quota fallback uses, so the "
            "frontend needs nothing new",
        )

    def test_route_cheapest_on_the_cheapest_rung_serves_as_chosen(self):
        self._spend(jpy_milli=200_000)
        resp, note, chosen = self._enforce(
            chosen=self._chosen("gemini-3.5-flash-lite"), AI_CEILING_ROUTE_CHEAPEST=True
        )
        self.assertIsNone(resp)
        self.assertIsNone(note)
        self.assertEqual(chosen.model, "gemini-3.5-flash-lite")

    def test_pause_beats_routing_when_both_are_on(self):
        self._spend(jpy_milli=200_000)
        resp, _note, _chosen = self._enforce(
            AI_CEILING_PAUSE=True, AI_CEILING_ROUTE_CHEAPEST=True
        )
        self.assertIsNotNone(resp, "the stronger lever wins")

    def test_enterprise_is_unlimited(self):
        self._spend(jpy_milli=10_000_000)  # ¥10,000
        with mock.patch("origin.search_engine.agent_views.log") as logger:
            resp, note, _chosen = self._enforce(plan="enterprise", AI_CEILING_PAUSE=True)
        self.assertIsNone(resp)
        self.assertIsNone(note)
        logger.warning.assert_not_called()

    def test_a_db_failure_fails_open(self):
        with override_settings(SEARCH_ENGINE=_se(AI_CEILING_PAUSE=True)):
            with mock.patch(
                "origin.search_engine.agent_views._month_spend_milli",
                side_effect=RuntimeError("db down"),
            ):
                from origin.search_engine.agent_views import _enforce_monthly_ceilings

                resp, note, chosen = _enforce_monthly_ceilings(
                    _UID, "free", "", self._chosen()
                )
        self.assertIsNone(resp, "a check that cannot run must never block")
        self.assertEqual(chosen.model, "gemini-3.6-flash")

    def test_team_ceiling_alerts_and_never_blocks(self):
        self._spend(jpy_milli=500_000, team_id="team-9")  # ¥500
        with self.assertLogs("origin.search_engine.agent_views", level="WARNING") as cm:
            resp, _note, _chosen = self._enforce(
                team_id="team-9",
                AI_TEAM_MONTHLY_CEILING_JPY=300,
                # user spend also over free's ceiling — but even with
                # NO user lever on, the team line must appear.
            )
        self.assertIsNone(resp)
        self.assertTrue(any("Team team-9" in line for line in cm.output))

    def test_team_ceiling_zero_is_off(self):
        self._spend(jpy_milli=1_000, team_id="team-9")
        with mock.patch("origin.search_engine.agent_views.log") as logger:
            self._enforce(team_id="team-9", AI_TEAM_MONTHLY_CEILING_JPY=0)
        logger.warning.assert_not_called()
