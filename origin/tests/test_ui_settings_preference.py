"""Tests for the `ui_settings` cross-device preference endpoint
(`GET / PATCH /api/v2/user/preferences/ui-settings/`).

Backs `CustomUser.ui_settings` — a schemaless bag of UI preferences
(theme, locale, chat bubble style, sidebar item visibility, …) the
frontend `useUiSettings` provider owns. Persisting it per-account (not
just browser localStorage) is what makes those preferences follow the
user across devices/sessions.

The endpoint's contract: PATCH shallow-merges the posted keys into the
stored blob (omitted keys survive; a `null` value deletes its key);
values must be JSON scalars or a flat list/object of them.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

User = get_user_model()

URL = "/api/v2/user/preferences/ui-settings/"


class TestUiSettingsPreference(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="pref-user",
            email="pref@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client.force_authenticate(user=self.user)

    def test_get_returns_empty_object_by_default(self):
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ui_settings": {}})

    def test_patch_persists_and_returns_merged(self):
        resp = self.client.patch(URL, {"theme": "dark"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ui_settings": {"theme": "dark"}})
        self.user.refresh_from_db()
        self.assertEqual(self.user.ui_settings, {"theme": "dark"})

    def test_patch_shallow_merges_rather_than_replacing(self):
        self.client.patch(URL, {"theme": "dark"}, format="json")
        resp = self.client.patch(URL, {"locale": "ja"}, format="json")
        self.assertEqual(resp.status_code, 200)
        # The earlier key survives — this is a merge, not a replace.
        self.assertEqual(resp.json(), {"ui_settings": {"theme": "dark", "locale": "ja"}})

    def test_patch_overwrites_existing_key(self):
        self.client.patch(URL, {"theme": "dark"}, format="json")
        resp = self.client.patch(URL, {"theme": "light"}, format="json")
        self.assertEqual(resp.json(), {"ui_settings": {"theme": "light"}})

    def test_patch_null_deletes_key(self):
        self.client.patch(URL, {"theme": "dark", "locale": "ja"}, format="json")
        resp = self.client.patch(URL, {"theme": None}, format="json")
        self.assertEqual(resp.status_code, 200)
        # `null` resets the preference to its built-in default by removing it.
        self.assertEqual(resp.json(), {"ui_settings": {"locale": "ja"}})

    def test_patch_accepts_flat_object_value(self):
        value = {"inbox": False, "chats": True, "tasks": True}
        resp = self.client.patch(URL, {"sidebar_visibility": value}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["ui_settings"]["sidebar_visibility"], value)

    def test_patch_accepts_flat_list_value(self):
        resp = self.client.patch(URL, {"pinned": ["a", "b"]}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["ui_settings"]["pinned"], ["a", "b"])

    def test_patch_rejects_nested_object(self):
        resp = self.client.patch(
            URL, {"deep": {"a": {"b": 1}}}, format="json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_patch_empty_body_is_400(self):
        resp = self.client.patch(URL, {}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_patch_non_object_body_is_400(self):
        resp = self.client.patch(URL, [1, 2, 3], format="json")
        self.assertEqual(resp.status_code, 400)

    def test_unauthenticated_is_rejected(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(URL)
        self.assertIn(resp.status_code, (401, 403))

    def test_patch_only_affects_own_user(self):
        other = User.objects.create_user(
            username="other",
            email="other@test.com",
            password="x",
            is_email_verified=True,
        )
        resp = self.client.patch(URL, {"theme": "dark"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.user.ui_settings, {"theme": "dark"})
        self.assertEqual(other.ui_settings, {})
