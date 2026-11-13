"""MIP-905: the member's content-length preference (`GET / PATCH /api/v2/user/preferences/content-length/`).

Stored whatever `AGENT_CONTENT_LENGTH` says. Nothing here is model-visible: the preference is
not in the llm-model preference payload, and no agent tool returns it.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

URL = "/api/v2/user/preferences/content-length/"
LLM_URL = "/api/v2/user/preferences/llm-model/"


class ContentLengthPreferenceTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="cl-user", email="cl@test.com", password="x", is_email_verified=True
        )
        self.client.force_authenticate(user=self.user)

    def test_default_is_standard(self):
        self.assertEqual(self.user.preferred_content_length, "standard")
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"content_length": "standard"})

    def test_an_empty_stored_value_reads_as_standard(self):
        self.user.preferred_content_length = ""
        self.user.save(update_fields=["preferred_content_length"])
        self.assertEqual(self.client.get(URL).json(), {"content_length": "standard"})

    def test_each_level_persists(self):
        for level in ("short", "long", "standard"):
            resp = self.client.patch(URL, {"content_length": level}, format="json")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"content_length": level})
            self.user.refresh_from_db()
            self.assertEqual(self.user.preferred_content_length, level)
            self.assertEqual(self.client.get(URL).json(), {"content_length": level})

    def test_value_is_trimmed_and_lowercased(self):
        resp = self.client.patch(URL, {"content_length": "  LONG "}, format="json")
        self.assertEqual(resp.json(), {"content_length": "long"})

    def test_unknown_empty_missing_and_non_string_are_400_and_change_nothing(self):
        self.client.patch(URL, {"content_length": "long"}, format="json")
        for body in (
            {"content_length": "huge"},
            {"content_length": ""},
            {"content_length": None},
            {"content_length": 3},
            {"content_length": ["long"]},
            {},
        ):
            resp = self.client.patch(URL, body, format="json")
            self.assertEqual(resp.status_code, 400, body)
        self.user.refresh_from_db()
        self.assertEqual(self.user.preferred_content_length, "long")

    def test_unauthenticated_is_rejected(self):
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get(URL).status_code, (401, 403))
        self.assertIn(self.client.patch(URL, {"content_length": "long"}).status_code, (401, 403))

    def test_patch_only_affects_the_caller(self):
        other = User.objects.create_user(
            username="cl-other", email="cl-other@test.com", password="x", is_email_verified=True
        )
        resp = self.client.patch(
            URL, {"content_length": "short", "user_id": str(other.id)}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        other.refresh_from_db()
        self.assertEqual(other.preferred_content_length, "standard")

    def test_the_llm_model_preference_payload_is_untouched(self):
        self.client.patch(URL, {"content_length": "long"}, format="json")
        resp = self.client.get(LLM_URL)
        self.assertEqual(set(resp.json()), {"provider", "model", "effort"})
