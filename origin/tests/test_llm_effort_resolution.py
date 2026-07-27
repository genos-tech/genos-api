"""Effort-level resolution + wiring (AGENT_EFFORT_LEVELS).

Three layers, matching the failure modes that matter:

  * `resolve_user_effort` — the (provider, effort, legacy model) →
    LlmChoice mapping, incl. the read-time legacy migration.
  * `active_effort_profile` / `subprocess_model_override` — the two
    read seams every consumer goes through; their None conventions ARE
    the flag-off byte-identity story.
  * The loop/search wiring — profile-driven max_steps / rewrite
    variants / reranker gate, asserted against a scripted client.

Flag-off assertions are as load-bearing as flag-on ones: the flip must
be a no-op until a request carries an effort.
"""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from origin.search_engine.llm.choice import (
    LlmChoice,
    active_effort_profile,
    reset_llm_choice,
    resolve_user_effort,
    set_llm_choice,
    subprocess_model_override,
)

User = get_user_model()

PREF_URL = "/api/v2/user/preferences/llm-model/"


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class ResolveUserEffortTests(SimpleTestCase):
    """Pure resolution against the SHIPPED catalog (no rigs — the rung
    indices are the contract under test)."""

    def test_explicit_effort_maps_to_the_rung(self):
        for effort, expected in (
            ("low", "claude-haiku-4-5"),
            ("medium", "claude-sonnet-5"),
            ("high", "claude-opus-5"),
        ):
            choice = resolve_user_effort("claude", effort, "")
            self.assertEqual(choice, LlmChoice("claude", expected, effort))

    def test_legacy_model_derives_its_rungs_effort(self):
        """The read-time migration: a saved model resolves to ITS OWN
        rung's effort — same provider, same model, nobody notices."""
        for model, effort in (
            ("gpt-5.6-luna", "low"),
            ("gpt-5.6-terra", "medium"),
            ("gpt-5.6-sol", "high"),
        ):
            choice = resolve_user_effort("openai", "", model)
            self.assertEqual(choice.effort, effort)
            self.assertEqual(choice.model, model, "derivation must not change the model")

    def test_explicit_effort_beats_the_legacy_model(self):
        choice = resolve_user_effort("claude", "low", "claude-opus-5")
        self.assertEqual(choice.model, "claude-haiku-4-5")

    def test_stale_legacy_model_falls_back_to_default_effort(self):
        choice = resolve_user_effort("claude", "", "claude-retired-model")
        self.assertEqual(choice.effort, "medium")
        self.assertEqual(choice.model, "claude-sonnet-5")

    def test_no_preference_at_all_is_medium_on_the_default_provider(self):
        """Medium == today's default experience (the medium invariant),
        so this cohort — everyone who never opened the picker — is
        untouched by the flip."""
        with override_settings(
            SEARCH_ENGINE=_se(LLM_PROVIDER="gemini", GEMINI_MODEL="gemini-3.6-flash")
        ):
            choice = resolve_user_effort("", "", "")
        self.assertEqual(choice, LlmChoice("gemini", "gemini-3.6-flash", "medium"))

    def test_unknown_provider_falls_back_with_warning(self):
        with self.assertLogs("origin.search_engine.llm.choice", level="WARNING"):
            choice = resolve_user_effort("mistral", "high", "")
        self.assertEqual(choice.provider, "gemini")
        self.assertEqual(choice.effort, "high")

    def test_invalid_effort_string_is_treated_as_unset(self):
        choice = resolve_user_effort("claude", "maximum", "")
        self.assertEqual(choice.effort, "medium")


class EffortSeamsTests(SimpleTestCase):
    """`active_effort_profile` and `subprocess_model_override` — the
    None conventions that keep flag-off byte-identical."""

    def _bound(self, choice):
        token = set_llm_choice(choice)
        self.addCleanup(reset_llm_choice, token)

    def test_profile_is_none_when_flag_off(self):
        self._bound(LlmChoice("claude", "claude-opus-5", "high"))
        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=False)):
            self.assertIsNone(active_effort_profile())

    def test_profile_is_none_without_an_effort_carrying_choice(self):
        # Legacy resolve path / evals / crons: choice has effort="".
        self._bound(LlmChoice("claude", "claude-opus-5"))
        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=True)):
            self.assertIsNone(active_effort_profile())

    def test_profile_resolves_when_flag_on_and_effort_set(self):
        self._bound(LlmChoice("claude", "claude-haiku-4-5", "low"))
        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=True)):
            profile = active_effort_profile()
        self.assertIsNotNone(profile)
        self.assertEqual(profile.name, "low")
        self.assertEqual(profile.max_steps, 6)

    def test_pin_is_none_when_flag_off(self):
        choice = LlmChoice("claude", "claude-opus-5", "high")
        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=False)):
            self.assertIsNone(subprocess_model_override("rerank", choice))

    def test_pin_is_the_providers_cheapest_rung_when_flag_on(self):
        choice = LlmChoice("claude", "claude-opus-5", "high")
        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=True)):
            self.assertEqual(
                subprocess_model_override("rerank", choice), "claude-haiku-4-5"
            )
            self.assertEqual(
                subprocess_model_override("summaries", choice), "claude-haiku-4-5"
            )

    def test_pin_is_same_provider_for_every_provider(self):
        """The whole cross-provider-safety story: the pin can never
        hand provider A's id to provider B's adapter."""
        from django.conf import settings as dj

        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=True)):
            for provider in ("gemini", "claude", "openai"):
                choice = LlmChoice(provider, "anything", "high")
                pin = subprocess_model_override("rewrite", choice)
                self.assertIn(pin, dj.LLM_CATALOG.provider_models(provider))

    def test_env_override_makes_the_pin_stand_down(self):
        """RAG_*_MODEL env is the operator rollback lever — when set,
        the pin returns None so the caller's own env logic applies,
        exactly as before effort levels existed."""
        choice = LlmChoice("claude", "claude-opus-5", "high")
        with override_settings(
            SEARCH_ENGINE=_se(
                AGENT_EFFORT_LEVELS=True, RAG_RERANKER_MODEL="claude-sonnet-5"
            )
        ):
            self.assertIsNone(subprocess_model_override("rerank", choice))

    def test_pin_without_a_choice_is_none(self):
        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=True)):
            self.assertIsNone(subprocess_model_override("rerank", None))


class PreferenceEndpointEffortTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="effort-user",
            email="effort@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client.force_authenticate(user=self.user)

    def test_get_includes_effort(self):
        resp = self.client.get(PREF_URL)
        self.assertEqual(resp.json(), {"provider": "", "model": "", "effort": ""})

    def test_patch_effort_round_trips_without_touching_the_model(self):
        self.user.preferred_llm_model = "claude-opus-5"
        self.user.save(update_fields=["preferred_llm_model"])
        resp = self.client.patch(
            PREF_URL, {"provider": "claude", "effort": "low"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferred_llm_effort, "low")
        # The legacy field is the rollback substrate — never cleared by
        # an effort write.
        self.assertEqual(self.user.preferred_llm_model, "claude-opus-5")

    def test_patch_rejects_an_unknown_effort(self):
        resp = self.client.patch(
            PREF_URL, {"provider": "claude", "effort": "maximum"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_legacy_patch_without_effort_key_resets_the_effort(self):
        """An old client writing (provider, model) is the user's newest
        intent — a stale explicit effort must not keep overriding it.
        Reset to '' = derive-from-model = exactly what that client
        expects to happen."""
        self.user.preferred_llm_effort = "high"
        self.user.save(update_fields=["preferred_llm_effort"])
        resp = self.client.patch(
            PREF_URL, {"provider": "claude", "model": "claude-haiku-4-5"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferred_llm_effort, "")
        self.assertEqual(self.user.preferred_llm_model, "claude-haiku-4-5")


@override_settings(SEARCH_ENGINE_PATCHED=None)
class LoopWiringTests(SimpleTestCase):
    """Profile-driven loop params via a scripted client."""

    def _run(self, *, flag: bool, choice: LlmChoice, script_len: int = 12):
        from origin.search_engine.agent.controller import _drive_loop
        from origin.search_engine.agent.tools import ToolContext
        from origin.search_engine.llm.types import AgentMessage, FunctionCall

        calls = {"n": 0}

        class _EndlessToolClient:
            def generate_step(self, messages, tools, system_instruction, **kw):
                calls["n"] += 1
                yield ("thinking. ", None)
                yield (None, FunctionCall(name="not_a_real_tool", args={}))

        events: list[dict] = []
        token = set_llm_choice(choice)
        try:
            with (
                override_settings(
                    SEARCH_ENGINE=_se(
                        AGENT_EFFORT_LEVELS=flag,
                        AGENT_MAX_STEPS=10,
                        # These tests count LOOP calls at the cap. The
                        # step-cap wrap-up would add its +1 tool-less
                        # synthesis call and swallow the error event —
                        # it has its own suite
                        # (test_agent_step_cap_wrapup); keep this one
                        # about effort-profile wiring only.
                        AGENT_STEP_CAP_WRAPUP=False,
                    )
                ),
                patch(
                    "origin.search_engine.agent.controller.get_model_client",
                    return_value=_EndlessToolClient(),
                ),
            ):
                _drive_loop(
                    messages=[AgentMessage(role="user", text="q")],
                    ctx=ToolContext(team_id="t", user_id="u"),
                    emit=events.append,
                    run_id=None,
                    starting_step=0,
                    seen_sources_by_id={},
                )
        finally:
            reset_llm_choice(token)
        return calls["n"], events

    def test_low_effort_caps_the_loop_at_its_profile_steps(self):
        n, events = self._run(
            flag=True, choice=LlmChoice("claude", "claude-haiku-4-5", "low")
        )
        self.assertEqual(n, 6)  # low profile max_steps
        self.assertTrue(any(e.get("type") == "error" for e in events))

    def test_flag_off_keeps_the_env_step_cap(self):
        n, _ = self._run(flag=False, choice=LlmChoice("claude", "claude-haiku-4-5", "low"))
        self.assertEqual(n, 10)  # AGENT_MAX_STEPS

    def test_effortless_choice_keeps_the_env_step_cap_even_with_flag_on(self):
        n, _ = self._run(flag=True, choice=LlmChoice("claude", "claude-haiku-4-5"))
        self.assertEqual(n, 10)


class ModelsEndpointEffortTests(TestCase):
    """`/agent/models/` under the flag — additive payload contract."""

    URL = "/api/v2/agent/models/"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="effort-models-user",
            email="effort-models@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client.force_authenticate(user=self.user)

    def test_flag_off_payload_has_no_effort_fields(self):
        """The absence of `efforts` IS the frontend's legacy-UI switch."""
        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=False)):
            data = self.client.get(self.URL).json()
        self.assertNotIn("efforts", data)
        self.assertNotIn("effort", data["current"])

    def test_flag_on_adds_efforts_and_keeps_models_verbatim(self):
        from django.conf import settings as dj

        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=False)):
            legacy = self.client.get(self.URL).json()
        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=True)):
            data = self.client.get(self.URL).json()
        # Additive: everything the legacy payload had is unchanged.
        self.assertEqual(data["models"], legacy["models"])
        self.assertEqual(data["limits"], legacy["limits"])
        self.assertEqual(data["current"]["provider"], legacy["current"]["provider"])
        self.assertIn(data["current"]["effort"], ("low", "medium", "high"))
        # 3 providers x 3 efforts, each row mapping to a real model with
        # that model's own quota numbers.
        self.assertEqual(len(data["efforts"]), 9)
        model_daily = dj.SEARCH_ENGINE["TIER_QUOTAS"][data["tier"]]["model_daily"]
        for row in data["efforts"]:
            self.assertEqual(
                set(row),
                {"provider", "effort", "model", "model_label", "daily_limit", "used_today"},
            )
            self.assertEqual(row["daily_limit"], model_daily.get(row["model"]))

    def test_saved_effort_is_reflected_in_current(self):
        self.user.preferred_llm_provider = "openai"
        self.user.preferred_llm_effort = "high"
        self.user.save(update_fields=["preferred_llm_provider", "preferred_llm_effort"])
        with override_settings(SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=True)):
            data = self.client.get(self.URL).json()
        self.assertEqual(data["current"]["effort"], "high")
        self.assertEqual(data["current"]["model"], "gpt-5.6-sol")


class SubprocessPinWiringTests(SimpleTestCase):
    """The rewriter reads the pin through the model_override seam; env
    overrides keep winning; flag off inherits. (The reranker shares the
    identical precedence expression — pinned at the seam level by
    EffortSeamsTests.)"""

    def _capture_override(self, num_variants=1):
        from origin.search_engine import query_rewriter

        captured = {}

        class _Client:
            def generate_step(self, messages, tools, system_instruction, **kw):
                captured["override"] = kw.get("model_override")
                yield ('["variant one"]', None)

        with patch(
            "origin.search_engine.query_rewriter.get_model_client",
            return_value=_Client(),
        ):
            query_rewriter.rewrite_query("q", num_variants=num_variants)
        return captured.get("override")

    def test_rewriter_uses_the_pin_when_flag_on(self):
        token = set_llm_choice(LlmChoice("claude", "claude-opus-5", "high"))
        try:
            with override_settings(
                SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=True, RAG_REWRITE_MODEL="")
            ):
                override = self._capture_override()
        finally:
            reset_llm_choice(token)
        self.assertEqual(override, "claude-haiku-4-5")

    def test_rewriter_env_override_beats_the_pin(self):
        token = set_llm_choice(LlmChoice("claude", "claude-opus-5", "high"))
        try:
            with override_settings(
                SEARCH_ENGINE=_se(
                    AGENT_EFFORT_LEVELS=True, RAG_REWRITE_MODEL="claude-sonnet-5"
                )
            ):
                override = self._capture_override()
        finally:
            reset_llm_choice(token)
        self.assertEqual(override, "claude-sonnet-5")

    def test_rewriter_inherits_when_flag_off(self):
        token = set_llm_choice(LlmChoice("claude", "claude-opus-5"))
        try:
            with override_settings(
                SEARCH_ENGINE=_se(AGENT_EFFORT_LEVELS=False, RAG_REWRITE_MODEL="")
            ):
                override = self._capture_override()
        finally:
            reset_llm_choice(token)
        self.assertIsNone(override)  # inherit = the choice wrapper's model
