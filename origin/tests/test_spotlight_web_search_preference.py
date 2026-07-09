"""Tests for the `spotlight_web_search_enabled` user preference endpoint
(`GET / PATCH /api/v2/user/preferences/spotlight-web-search/`).

Persisting this per-account (rather than only in browser localStorage) is
what makes the Spotlight "Web search" toggle follow the user across
devices/sessions.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

URL = "/api/v2/user/preferences/spotlight-web-search/"


class TestSpotlightWebSearchPreference(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="pref-user",
            email="pref@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client.force_authenticate(user=self.user)

    def test_get_returns_default_false(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"spotlight_web_search_enabled": False})

    def test_patch_true_persists(self):
        resp = self.client.patch(URL, {"spotlight_web_search_enabled": True}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"spotlight_web_search_enabled": True})
        self.user.refresh_from_db()
        self.assertTrue(self.user.spotlight_web_search_enabled)

    def test_patch_false_persists(self):
        self.user.spotlight_web_search_enabled = True
        self.user.save(update_fields=["spotlight_web_search_enabled"])
        resp = self.client.patch(URL, {"spotlight_web_search_enabled": False}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"spotlight_web_search_enabled": False})
        self.user.refresh_from_db()
        self.assertFalse(self.user.spotlight_web_search_enabled)

    def test_patch_non_boolean_is_400(self):
        resp = self.client.patch(URL, {"spotlight_web_search_enabled": "true"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_patch_missing_field_is_400(self):
        resp = self.client.patch(URL, {}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_is_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(URL)
        self.assertIn(resp.status_code, (401, 403))

    def test_patch_only_affects_own_user(self):
        # No accepted `user_id` field — even if a malicious client passes
        # one, the endpoint operates on request.user.
        other = User.objects.create_user(
            username="other",
            email="other@test.com",
            password="x",
            is_email_verified=True,
        )
        resp = self.client.patch(
            URL,
            {"spotlight_web_search_enabled": True, "user_id": other.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(self.user.spotlight_web_search_enabled)
        self.assertFalse(other.spotlight_web_search_enabled)
