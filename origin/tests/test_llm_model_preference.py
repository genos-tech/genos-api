"""Tests for `PATCH /api/v2/user/preferences/llm-model/`.

The Settings model picker is fed by `MODEL_CATALOG` (via
`/api/v2/agent/models/`), but SAVING a choice goes through a separate
hard-coded provider allowlist in `LlmModelPreferenceView.patch`. Those
two lists are maintained by hand and nothing else in the suite ties them
together — a provider present in the catalog but missing from the
allowlist renders a selectable model whose save 400s, and every other
test still passes. That is exactly what happened when the OpenAI rungs
were added, hence this file.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

URL = "/api/v2/user/preferences/llm-model/"


class TestLlmModelPreference(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="llm-pref-user",
            email="llm-pref@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client.force_authenticate(user=self.user)

    def test_every_catalog_provider_can_be_saved(self):
        """The save allowlist must cover every provider in MODEL_CATALOG.

        Iterates the shipped catalog rather than a hard-coded list, so a
        future provider is covered the day its rungs are added.
        """
        catalog = settings.SEARCH_ENGINE["MODEL_CATALOG"]
        self.assertTrue(catalog, "MODEL_CATALOG is empty")
        seen = set()
        for entry in catalog:
            provider, model = entry["provider"], entry["model"]
            resp = self.client.patch(
                URL, {"provider": provider, "model": model}, format="json"
            )
            self.assertEqual(
                resp.status_code,
                200,
                f"saving catalog entry ({provider}, {model}) failed with "
                f"{resp.status_code}: {resp.content!r} — the picker offers this "
                f"model but the save allowlist rejects its provider",
            )
            self.user.refresh_from_db()
            self.assertEqual(self.user.preferred_llm_provider, provider)
            self.assertEqual(self.user.preferred_llm_model, model)
            seen.add(provider)
        # Guards against the catalog silently losing a provider entirely.
        self.assertEqual(seen, {"gemini", "claude", "openai"})

    def test_saved_openai_choice_round_trips_through_the_resolver(self):
        """Saving is only half the path — the resolver must accept it too.

        `resolve_user_choice` has its own allowlist; if it disagreed with
        the save path, the preference would persist and then be silently
        ignored at ask time.
        """
        from origin.search_engine.llm.choice import resolve_user_choice

        self.client.patch(
            URL, {"provider": "openai", "model": "gpt-5.6-terra"}, format="json"
        )
        self.user.refresh_from_db()
        choice = resolve_user_choice(
            self.user.preferred_llm_provider, self.user.preferred_llm_model
        )
        self.assertEqual(choice.provider, "openai")
        self.assertEqual(choice.model, "gpt-5.6-terra")

    def test_unknown_provider_is_rejected(self):
        resp = self.client.patch(
            URL, {"provider": "mistral", "model": "mistral-large"}, format="json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_empty_provider_clears_the_preference(self):
        self.client.patch(URL, {"provider": "openai", "model": "gpt-5.6-sol"}, format="json")
        resp = self.client.patch(URL, {"provider": "", "model": ""}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferred_llm_provider, "")
