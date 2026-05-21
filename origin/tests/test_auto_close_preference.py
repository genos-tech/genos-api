"""Tests for the `auto_close_on_pr_merge` user preference endpoint
(`GET / PATCH /api/v2/user/preferences/auto-close-on-pr-merge/`).
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

URL = "/api/v2/user/preferences/auto-close-on-pr-merge/"


class TestAutoCloseOnPrMergePreference(TestCase):
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
        self.assertEqual(resp.json(), {"auto_close_on_pr_merge": False})

    def test_patch_true_persists(self):
        resp = self.client.patch(URL, {"auto_close_on_pr_merge": True}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"auto_close_on_pr_merge": True})
        self.user.refresh_from_db()
        self.assertTrue(self.user.auto_close_on_pr_merge)

    def test_patch_false_persists(self):
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])
        resp = self.client.patch(URL, {"auto_close_on_pr_merge": False}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"auto_close_on_pr_merge": False})
        self.user.refresh_from_db()
        self.assertFalse(self.user.auto_close_on_pr_merge)

    def test_patch_non_boolean_is_400(self):
        resp = self.client.patch(URL, {"auto_close_on_pr_merge": "true"}, format="json")
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
            {"auto_close_on_pr_merge": True, "user_id": other.id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        other.refresh_from_db()
        self.assertTrue(self.user.auto_close_on_pr_merge)
        self.assertFalse(other.auto_close_on_pr_merge)
