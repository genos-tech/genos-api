"""Adaptive effort — the "auto" router (UX tier model §5.3).

Dark behind AGENT_AUTO_EFFORT (default off). The contracts pinned:

  * the router FAILS OPEN to "medium" — error, timeout, or off-script
    output must degrade to the balanced rung, never to an error;
  * with the flag OFF a saved "auto" resolves exactly like an unset
    preference — the whole rollback story, no migration;
  * the provisional choice is medium + auto=True, so every pre-router
    consumer sees the same rung the ask degrades to on router failure;
  * the preference endpoint accepts "auto" even while the flag is off
    (a saved "auto" is never a brick).
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIClient

from origin.search_engine import quota
from origin.search_engine.agent_views import _resolve_choice_for
from origin.search_engine.llm import effort_router
from origin.search_engine.llm.choice import resolve_user_effort

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas


def _fake_client(reply: str):
    client = mock.Mock()
    client.generate_step.return_value = iter([(reply, None)])
    return client


class RouterTests(SimpleTestCase):
    def _route(self, reply):
        with mock.patch.object(
            effort_router, "get_model_client", return_value=_fake_client(reply)
        ):
            return effort_router.route_effort("why is the beta milestone blocked?", "claude")

    def test_each_verdict_passes_through(self):
        for reply, expected in (("low", "low"), ("medium", "medium"), ("high", "high")):
            self.assertEqual(self._route(reply), expected)

    def test_off_script_output_fails_open_to_medium(self):
        self.assertEqual(self._route("definitely high effort"), "medium")

    def test_light_formatting_noise_is_tolerated(self):
        self.assertEqual(self._route(" High.\n"), "high")

    def test_a_raising_client_fails_open_to_medium(self):
        broken = mock.Mock()
        broken.generate_step.side_effect = RuntimeError("adapter down")
        with mock.patch.object(effort_router, "get_model_client", return_value=broken):
            self.assertEqual(effort_router.route_effort("q", "gemini"), "medium")

    def test_router_uses_the_providers_rung_zero_model(self):
        client = _fake_client("low")
        with mock.patch.object(effort_router, "get_model_client", return_value=client):
            effort_router.route_effort("q", "claude")
        kwargs = client.generate_step.call_args.kwargs
        self.assertEqual(kwargs["model_override"], "claude-haiku-4-5")
        self.assertEqual(kwargs["tools"], [])


class FlagOffRollbackTests(SimpleTestCase):
    def test_saved_auto_resolves_like_unset(self):
        # "auto" is deliberately NOT in EFFORTS, so the not-in-EFFORTS
        # branch handles it: derive from the legacy model, else default.
        choice = resolve_user_effort("claude", "auto", "")
        self.assertEqual(choice.effort, "low")
        self.assertFalse(choice.auto)
        derived = resolve_user_effort("claude", "auto", "claude-opus-5")
        self.assertEqual(derived.effort, "high")


def _se(auto_flag: bool, tier_auto: bool):
    quotas = {
        **TEST_QUOTAS,
        "free": {**TEST_QUOTAS["free"], "auto_effort": tier_auto},
    }
    se = _search_engine_with_quotas(quotas)
    se["AGENT_EFFORT_LEVELS"] = True
    se["AGENT_AUTO_EFFORT"] = auto_flag
    return se


class ResolveChoiceAutoTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.user.preferred_llm_provider = "claude"
        self.user.preferred_llm_effort = "auto"
        self.user.save(update_fields=["preferred_llm_provider", "preferred_llm_effort"])
        quota.invalidate_effective_tier([self.user.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])
        super().tearDown()

    @override_settings(SEARCH_ENGINE=_se(auto_flag=True, tier_auto=True))
    def test_flag_and_tier_yield_a_provisional_medium(self):
        choice = _resolve_choice_for(self.user)
        self.assertTrue(choice.auto)
        self.assertEqual(choice.effort, "medium")
        self.assertEqual(choice.model, "claude-sonnet-5")

    @override_settings(SEARCH_ENGINE=_se(auto_flag=False, tier_auto=True))
    def test_flag_off_ignores_the_saved_auto(self):
        choice = _resolve_choice_for(self.user)
        self.assertFalse(choice.auto)
        self.assertEqual(choice.effort, "low")

    @override_settings(SEARCH_ENGINE=_se(auto_flag=True, tier_auto=False))
    def test_tier_without_auto_ignores_the_saved_auto(self):
        choice = _resolve_choice_for(self.user)
        self.assertFalse(choice.auto)
        self.assertEqual(choice.effort, "low")


class PreferenceEndpointAutoTests(BaseAPITestCase):
    URL = "/api/v2/user/preferences/llm-model/"

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_auto_is_accepted_even_while_the_flag_is_off(self):
        resp = self.client.patch(
            self.URL, {"provider": "claude", "effort": "auto"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferred_llm_effort, "auto")

    def test_unknown_effort_is_still_rejected(self):
        resp = self.client.patch(
            self.URL, {"provider": "claude", "effort": "turbo"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)
