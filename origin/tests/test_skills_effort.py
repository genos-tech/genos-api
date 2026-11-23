"""MIP-2198: a skill's effort level is selectable and is what the run actually uses.

What matters, in the order the task lists it:

  * ONE table: a level maps to the model tier, the step limit and the thinking budget the in-app
    agent already reads from the catalog (`efforts:` in llm_models.yaml), for the caller's own
    provider, and to its credit floor (`skill_effort:` in credit_policy.yaml). No model id is
    written in these tests: every expected value is read from the catalog.
  * A run binds the level: the model the agent calls, its effort profile (steps, thinking), the quota
    and cost-meter keys all see the level; a skill with no level leaves the caller's own setting.
  * The plan ceiling still clamps it.
  * Credits follow the level through the engine every ask uses: the same usage costs more credits
    at a higher level, to the CALLER.
  * At low credits the run starts one level lower, says so, and a spent balance is still refused by the
    ordinary gate; unlimited plans never step down.
  * A paused run continues at the level it started at.
  * `GENOS_SKILLS` off: the `skill` field is ignored, so nothing changes; `AGENT_EFFORT_LEVELS` off:
    the caller's own choice stands and the editor is told.
  * The model-visible prompt is the same as before, except for the one sentence of a stepped-down run.
"""

from __future__ import annotations

import threading
from unittest import mock

from django.conf import settings
from django.test import override_settings

from apis.llm_catalog import EFFORTS
from origin.models.skills.skill_models import Skill
from origin.search_engine import agent_views, credit_ledger, spend_recorder
from origin.search_engine.llm import spend
from origin.search_engine.llm.choice import active_effort_profile, get_llm_choice
from origin.search_engine.llm.types import CallUsage
from origin.search_engine.models import AiCreditEntry, AiRequestCost
from origin.services.skills import effort, run, service
from origin.tests.test_skills_run import ASK_URL, RunBase, authoritative

EFFORT_ON = override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, "AGENT_EFFORT_LEVELS": True})
LEVELS_URL = "/api/v2/skills/effort-levels/"


def effort_and(**more):
    return override_settings(
        SEARCH_ENGINE={**settings.SEARCH_ENGINE, "AGENT_EFFORT_LEVELS": True, **more}
    )


def policy_floor(level):
    return settings.CREDIT_POLICY.skill_effort_floor_milli[level]


class TableTests(RunBase):
    """One table: read from the catalog and the policy, never written twice."""

    def test_every_level_maps_to_the_catalogs_model_steps_and_thinking_budget(self):
        catalog = settings.LLM_CATALOG
        for provider in catalog.provider_order():
            rows = effort.table(provider)
            self.assertEqual([r.level for r in rows], list(EFFORTS))
            for row in rows:
                with self.subTest(provider=provider, level=row.level):
                    profile = catalog.efforts[row.level]
                    self.assertEqual(row.model, catalog.model_for_effort(provider, row.level))
                    self.assertIn(row.model, catalog.provider_models(provider))
                    self.assertEqual(row.max_steps, profile.max_steps)
                    self.assertEqual(row.thinking_budget, profile.thinking_budget)
                    self.assertEqual(row.degrade_below_milli, policy_floor(row.level))

    def test_the_relative_cost_starts_at_one_and_never_falls(self):
        for provider in settings.LLM_CATALOG.provider_order():
            costs = [r.relative_cost for r in effort.table(provider)]
            with self.subTest(provider=provider):
                self.assertEqual(costs[0], 1.0)
                self.assertTrue(all(c is not None for c in costs))
                self.assertEqual(costs, sorted(costs))

    def test_the_plan_ceiling_locks_the_levels_above_it(self):
        provider = settings.LLM_CATALOG.provider_order()[0]
        locked = {r.level: r.locked for r in effort.table(provider, "medium")}
        self.assertEqual(locked, {"low": False, "medium": False, "high": True})
        self.assertFalse(any(r.locked for r in effort.table(provider, "high")))

    def test_the_credit_floors_step_down_one_level_and_low_is_the_floor(self):
        floors = settings.CREDIT_POLICY.skill_effort_floor_milli
        self.assertEqual(floors["low"], 0)
        self.assertLess(floors["medium"], floors["high"])

    def test_a_malformed_floor_fails_the_boot_not_the_request(self):
        from apis.credit_policy import CreditPolicyError, _parse_skill_effort  # noqa: PLC0415

        good = {lvl: {"degrade_below_credits": 0} for lvl in EFFORTS}
        self.assertEqual(_parse_skill_effort({"skill_effort": good}), {lvl: 0 for lvl in EFFORTS})
        self.assertEqual(_parse_skill_effort({}), {})  # an older policy file: never degrade
        for bad in (
            {**good, "high": {"degrade_below_credits": -1}},
            {**good, "low": {"degrade_below_credits": 1}},
            {**good, "max": {"degrade_below_credits": 1}},
            {"low": good["low"], "medium": good["medium"]},
            {**good, "high": {"degrade_below_credits": "a lot"}},
            {**good, "high": {}},
        ):
            with self.subTest(bad=bad), self.assertRaises(CreditPolicyError):
                _parse_skill_effort({"skill_effort": bad})

    def test_only_a_known_level_is_kept_otherwise_my_setting(self):
        self.assertEqual([effort.clean(v) for v in ("low", "medium", "high")], list(EFFORTS))
        for value in ("", None, "auto", "extreme", "HIGH", 3):
            self.assertEqual(effort.clean(value), "")


class AskBase(RunBase):
    """The real ask view with the model faked; records what the run was bound to."""

    def ask(self, skill, *, query="/report for the Q3 plan", user=None, usage=None):
        self.as_user(user or self.user)
        got: dict = {}

        def fake_stream(worker, **kwargs):
            got["worker"], got["stream"] = worker, kwargs
            return iter([b""])

        def fake_run_agent(q, ctx, emit, **kwargs):
            # The worker has bound the choice and the spend context by now: what the loop reads.
            got["choice"] = get_llm_choice()
            got["profile"] = active_effort_profile()
            got["kwargs"] = kwargs
            if usage:
                call = CallUsage(provider=got["choice"].provider, model=got["choice"].model)
                call.prompt_tokens, call.completion_tokens = usage
                spend.record_llm_call(call)
            emit({"type": "done"})

        body = {"query": query, "team_id": self.team_id}
        if skill is not None:
            body["skill"] = {"id": str(skill.id)}
        with (
            mock.patch("origin.search_engine.agent_views._stream_ndjson", side_effect=fake_stream),
            mock.patch("origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent),
            mock.patch(
                "origin.search_engine.agent_views.check_remaining", return_value=(True, 0, None)
            ),
        ):
            resp = self.client.post(ASK_URL, body, format="json")
            if "worker" in got:
                got["worker"](lambda event: None, threading.Event())
        return resp, got

    def skill_at(self, level, **over):
        self._n = getattr(self, "_n", 0) + 1
        over.setdefault("slug", f"s-{level or 'own'}-{self._n}")
        return self.skill(self.user, effort=level, **over)

    def model_of(self, level, provider=None):
        provider = provider or settings.LLM_CATALOG.provider_order()[0]
        return settings.LLM_CATALOG.model_for_effort(provider, level)


@EFFORT_ON
class RunUsesTheLevelTests(AskBase):
    def test_each_level_binds_its_model_tier_step_limit_and_thinking_budget(self):
        for level in EFFORTS:
            with self.subTest(level=level):
                resp, got = self.ask(self.skill_at(level))
                self.assertEqual(resp.status_code, 200, getattr(resp, "content", b""))
                choice, profile = got["choice"], got["profile"]
                self.assertEqual(choice.effort, level)
                self.assertEqual(
                    choice.model, settings.LLM_CATALOG.model_for_effort(choice.provider, level)
                )
                catalog_profile = settings.LLM_CATALOG.efforts[level]
                self.assertEqual(profile.name, level)
                self.assertEqual(profile.max_steps, catalog_profile.max_steps)
                self.assertEqual(profile.thinking_budget, catalog_profile.thinking_budget)
                # Sent to the cost meter, so the ledger rows carry the level.
                self.assertEqual(got["stream"]["spend_kwargs"]["effort"], level)
                # And to the per-model quota keys: the model that is served is the model that counts.
                self.assertIn(choice.model, got["stream"]["quota_keys"])

    def test_the_levels_are_three_different_runs(self):
        seen = {}
        for level in EFFORTS:
            _resp, got = self.ask(self.skill_at(level, slug=f"r-{level}"), query=f"/r-{level}")
            seen[level] = (got["choice"].model, got["profile"].max_steps)
        self.assertEqual(len({m for m, _ in seen.values()}), len(EFFORTS))  # one model per rung
        self.assertLess(seen["low"][1], seen["medium"][1])  # a low run has the fewest steps

    def test_a_skill_with_no_level_runs_on_the_callers_own_setting(self):
        self.user.preferred_llm_effort = "high"
        self.user.save(update_fields=["preferred_llm_effort"])
        _resp, got = self.ask(self.skill_at(""))
        self.assertEqual(got["choice"].effort, "high")
        _resp, plain = self.ask(None, query="what is due?")
        self.assertEqual(plain["choice"], got["choice"])

    def test_a_low_skill_is_not_raised_by_a_high_caller(self):
        self.user.preferred_llm_effort = "high"
        self.user.save(update_fields=["preferred_llm_effort"])
        _resp, got = self.ask(self.skill_at("low"))
        self.assertEqual(got["choice"].effort, "low")

    def test_the_plan_ceiling_still_clamps_the_level(self):
        with mock.patch("origin.search_engine.quota.get_max_effort", return_value="medium"):
            _resp, got = self.ask(self.skill_at("high"))
        self.assertEqual(got["choice"].effort, "medium")
        self.assertEqual(got["choice"].model, self.model_of("medium", got["choice"].provider))

    def test_a_teammates_skill_runs_at_its_published_level(self):
        skill = self.skill(self.user2, "team", effort="high")
        _resp, got = self.ask(skill)
        self.assertEqual(got["choice"].effort, "high")
        # The owner's unpublished edit never reaches a teammate: the version's level counts.
        Skill.objects.filter(pk=skill.pk).update(effort="low")
        _resp, got = self.ask(skill)
        self.assertEqual(got["choice"].effort, "high")

    def test_the_prompt_the_model_reads_is_the_same_whatever_the_level(self):
        extras = {}
        for level in ("", "low", "high"):
            _resp, got = self.ask(self.skill_at(level, slug=f"p-{level or 'none'}"))
            extras[level] = got["kwargs"]["system_extra"].replace(f"p-{level or 'none'}", "NAME")
        self.assertEqual(len(set(extras.values())), 1)
        self.assertNotIn("effort", extras["high"].lower())


class SwitchTests(AskBase):
    def test_without_effort_levels_the_callers_own_choice_stands(self):
        with override_settings(
            SEARCH_ENGINE={**settings.SEARCH_ENGINE, "AGENT_EFFORT_LEVELS": False}
        ):
            _resp, got = self.ask(self.skill_at("high"))
            _resp, plain = self.ask(None, query="what is due?")
        self.assertEqual(got["choice"], plain["choice"])
        self.assertEqual(got["choice"].effort, "")
        self.assertIsNone(got["profile"])

    @EFFORT_ON
    def test_with_genos_skills_off_the_skill_field_and_its_level_are_ignored(self):
        skill = self.skill_at("high")
        with override_settings(GENOS_SKILLS=False):
            _resp, got = self.ask(skill)
            _resp, plain = self.ask(None, query="what is due?")
        self.assertEqual(got["choice"], plain["choice"])
        self.assertNotIn("SKILL RUN", got["kwargs"]["system_extra"] or "")

    @EFFORT_ON
    def test_a_plain_ask_is_untouched_by_the_feature(self):
        with mock.patch.object(agent_views.skill_effort, "apply") as applied:
            _resp, _got = self.ask(None, query="what is due?")
        applied.assert_not_called()

    @EFFORT_ON
    def test_the_levels_endpoint_is_a_404_while_the_switch_is_off(self):
        with override_settings(GENOS_SKILLS=False):
            self.as_user(self.user)
            self.assertEqual(self.client.get(LEVELS_URL).status_code, 404)


@EFFORT_ON
class CreditsFollowTheLevelTests(AskBase):
    """The same work costs more credits at a higher level, charged to the caller, by the one engine."""

    def charged(self, level):
        """The credits one run at `level` posts for a fixed usage, through the real lifecycle."""
        with authoritative():
            with mock.patch.object(agent_views, "get_effective_tier", return_value="pro"):
                resp, got = self.ask(
                    self.skill_at(level, slug=f"c-{level}"),
                    query=f"/c-{level}",
                    usage=(200_000, 20_000),
                )
            self.assertEqual(resp.status_code, 200, getattr(resp, "content", b""))
            ctx = spend.SpendContext(**got["stream"]["spend_kwargs"])
            spend_recorder.finish_request(ctx, result=AiRequestCost.RESULT_SUCCESS)
            row = AiRequestCost.objects.get(request_id=ctx.request_id)
            entry = AiCreditEntry.objects.get(entry_type="charge", request_id=ctx.request_id)
        self.assertEqual(entry.user_id, str(self.user.pk))
        self.assertEqual(row.effort, level)
        self.assertEqual(entry.credits_milli, -row.shadow_credits_milli)
        return row.shadow_credits_milli

    def test_a_higher_level_posts_a_larger_charge_to_the_caller(self):
        by_level = {level: self.charged(level) for level in EFFORTS}
        self.assertGreater(by_level["low"], 0)
        self.assertLess(by_level["low"], by_level["medium"])
        self.assertLess(by_level["medium"], by_level["high"])

    def test_the_author_is_never_charged(self):
        skill = self.skill(self.user2, "team", effort="high")
        with authoritative():
            _resp, got = self.ask(skill, usage=(1000, 100))
        self.assertEqual(got["stream"]["spend_kwargs"]["user_id"], str(self.user.pk))


@EFFORT_ON
class DegradeAtLowCreditsTests(AskBase):
    def ask_with_balance(self, skill, balance_milli, **kw):
        with (
            authoritative(),
            mock.patch.object(credit_ledger, "balance_milli", return_value=balance_milli),
        ):
            return self.ask(skill, **kw)

    def test_a_balance_under_the_floor_starts_one_level_lower_and_says_so(self):
        cases = (("high", "medium"), ("medium", "low"))
        for level, lower in cases:
            with self.subTest(level=level):
                skill = self.skill_at(level, slug=f"d-{level}")
                resp, got = self.ask_with_balance(
                    skill, policy_floor(level) - 1, query=f"/d-{level}"
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(got["choice"].effort, lower)
                self.assertEqual(got["choice"].model, self.model_of(lower, got["choice"].provider))
                self.assertEqual(got["profile"].name, lower)
                self.assertEqual(got["stream"]["spend_kwargs"]["effort"], lower)
                # The person is told, before the answer, in one sentence naming both levels.
                extra = got["kwargs"]["system_extra"]
                self.assertIn(f"set to {level} effort", extra)
                self.assertIn(f"ran at {lower} effort", extra)
                # And the client can say it too, on the `done` event's fallback note.
                self.assertEqual(
                    got["stream"]["fallback_note"],
                    {"requested_effort": level, "used_effort": lower, "reason": "credits"},
                )

    def test_a_balance_at_or_over_the_floor_does_not_step_down(self):
        skill = self.skill_at("high")
        for balance in (policy_floor("high"), policy_floor("high") + 5_000):
            with self.subTest(balance=balance):
                _resp, got = self.ask_with_balance(skill, balance)
                self.assertEqual(got["choice"].effort, "high")
                self.assertIsNone(got["stream"]["fallback_note"])
                self.assertNotIn("effort, but your AI credits", got["kwargs"]["system_extra"])

    def test_it_steps_down_exactly_one_level_and_low_is_the_floor(self):
        _resp, got = self.ask_with_balance(self.skill_at("high"), 1)  # almost nothing left
        self.assertEqual(got["choice"].effort, "medium")
        _resp, got = self.ask_with_balance(self.skill_at("low", slug="lowest"), 1, query="/lowest")
        self.assertEqual(got["choice"].effort, "low")
        self.assertIsNone(got["stream"]["fallback_note"])

    def test_a_spent_balance_is_refused_by_the_ordinary_gate_not_degraded(self):
        resp, got = self.ask_with_balance(self.skill_at("high"), 0)
        self.assertEqual(resp.status_code, 429)
        self.assertEqual(resp.json()["category"], "ai_credits")
        self.assertNotIn("choice", got)

    def test_an_unlimited_plan_never_steps_down(self):
        _resp, got = self.ask_with_balance(self.skill_at("high"), None)
        self.assertEqual(got["choice"].effort, "high")

    def test_credits_not_authoritative_never_steps_down(self):
        with mock.patch.object(credit_ledger, "balance_milli", return_value=1):
            _resp, got = self.ask(self.skill_at("high"))
        self.assertEqual(got["choice"].effort, "high")

    def test_a_ledger_that_cannot_be_read_runs_the_skill_at_its_level(self):
        with (
            authoritative(),
            mock.patch.object(credit_ledger, "balance_milli", side_effect=RuntimeError("down")),
        ):
            resp, got = self.ask(self.skill_at("high"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(got["choice"].effort, "high")

    def test_the_stepped_down_level_is_what_a_paused_run_keeps(self):
        skill = self.skill_at("high")
        _resp, got = self.ask_with_balance(skill, 1)
        self.assertEqual(got["stream"]["resume_context"]["skill"]["effort"], "medium")


@EFFORT_ON
class PausedRunKeepsTheLevelTests(AskBase):
    def test_the_reference_holds_the_level_and_never_the_text(self):
        skill = self.skill_at("high", body_markdown="STEP: SECRET-SKILL-WORDS")
        _resp, got = self.ask(skill)
        kept = got["stream"]["resume_context"]["skill"]
        self.assertEqual(kept["effort"], "high")
        self.assertNotIn("SECRET-SKILL-WORDS", repr(kept))

    def test_a_skill_with_no_level_keeps_nothing_extra(self):
        _resp, got = self.ask(self.skill_at(""))
        self.assertNotIn("effort", got["stream"]["resume_context"]["skill"])

    def test_the_resumed_leg_is_bound_to_the_first_legs_level_not_the_callers_now(self):
        provider = settings.LLM_CATALOG.provider_order()[0]
        caller_now = effort.bind_kept(
            agent_views.LlmChoice(provider=provider, model="x", effort="low"),
            "high",
            user_id=str(self.user.pk),
        )
        self.assertEqual(caller_now.effort, "high")
        self.assertEqual(caller_now.model, self.model_of("high", provider))
        # An unreadable or absent level leaves the caller's own choice.
        own = agent_views.LlmChoice(provider=provider, model="x", effort="low")
        for kept in ("", "bogus", None):
            self.assertIs(effort.bind_kept(own, kept, user_id=str(self.user.pk)), own)
        self.assertEqual(run.effort_for_kept({"id": "a", "effort": "medium"}), "medium")
        self.assertEqual(run.effort_for_kept({"id": "a", "effort": "turbo"}), "")

    def test_the_ceiling_of_today_still_clamps_a_resumed_leg(self):
        provider = settings.LLM_CATALOG.provider_order()[0]
        own = agent_views.LlmChoice(provider=provider, model="x", effort="low")
        with mock.patch("origin.search_engine.quota.get_max_effort", return_value="medium"):
            bound = effort.bind_kept(own, "high", user_id=str(self.user.pk))
        self.assertEqual(bound.effort, "medium")


class LevelsEndpointTests(AskBase):
    def get(self, user=None):
        self.as_user(user or self.user)
        return self.client.get(LEVELS_URL)

    @EFFORT_ON
    def test_it_lists_the_levels_with_the_relative_cost_for_the_callers_plan(self):
        resp = self.get()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["enabled"])
        self.assertEqual([lv["level"] for lv in body["levels"]], list(EFFORTS))
        self.assertEqual(body["levels"][0]["relativeCost"], 1.0)
        costs = [lv["relativeCost"] for lv in body["levels"]]
        self.assertEqual(costs, sorted(costs))
        self.assertFalse(any(lv["locked"] for lv in body["levels"]))
        for lv in body["levels"]:
            self.assertEqual(lv["maxSteps"], settings.LLM_CATALOG.efforts[lv["level"]].max_steps)
            self.assertTrue(lv["modelLabel"])

    @EFFORT_ON
    def test_a_lower_plan_sees_the_levels_above_its_ceiling_locked(self):
        with mock.patch("origin.search_engine.quota.get_max_effort", return_value="low"):
            body = self.get().json()
        self.assertEqual(
            {lv["level"]: lv["locked"] for lv in body["levels"]},
            {
                "low": False,
                "medium": True,
                "high": True,
            },
        )
        self.assertEqual(body["maxEffort"], "low")

    def test_it_says_so_while_the_agent_is_not_on_effort_levels(self):
        with override_settings(
            SEARCH_ENGINE={**settings.SEARCH_ENGINE, "AGENT_EFFORT_LEVELS": False}
        ):
            body = self.get().json()
        self.assertEqual(body, {"enabled": False, "levels": []})

    def test_anonymous_is_refused(self):
        self.client.credentials()
        self.assertIn(self.client.get(LEVELS_URL).status_code, (401, 403))


class OwnerPicksTheLevelTests(RunBase):
    """The editor's field already saved `effort`; the level survives save, publish and copy."""

    def test_the_level_is_saved_published_and_copied(self):
        skill = self.skill(self.user, "team", effort="high")
        self.assertEqual(skill.effort, "high")
        service.update(self.user, skill.id, service.Fields(effort="medium"))
        skill.refresh_from_db()
        self.assertEqual(skill.effort, "medium")
        resolved = run.resolve(self.user, self.team.team_id, {"id": str(skill.id)})
        self.assertEqual(resolved.effort, "medium")  # the owner's draft
        copy = service.copy(self.user2, skill.id, self.team.team_id)
        self.assertEqual(copy.effort, skill.effort)

    def test_an_unknown_level_is_refused_at_save(self):
        with self.assertRaises(service.SkillError):
            self.skill(self.user, effort="extreme")
