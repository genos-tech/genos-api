"""Tests for the email channel foundations (PR A1 of the email series):
`should_email` gating, the `email_enabled` preference field, the
`language` preference endpoint, and the `headers=` passthrough on
`send_templated_email`.

Mirrors `test_webpush.py::ShouldPushTests` deliberately — the two gates
must stay behaviorally parallel except where email differs on purpose
(independent master column, different defaults, `email:`-prefixed
overrides, fail-closed unknown categories).
"""

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status

from origin.models.common.notification_models import NotificationPreference
from origin.services.email import send_templated_email
from origin.services.email_gating import should_email
from origin.tests.test_base import BaseAPITestCase


class ShouldEmailTests(BaseAPITestCase):
    def test_no_prefs_row_uses_email_defaults(self):
        # user2 has no NotificationPreference row -> email defaults apply.
        self.assertTrue(should_email(self.user2.id, "mention_chat"))
        self.assertTrue(should_email(self.user2.id, "task_assign"))
        self.assertTrue(should_email(self.user2.id, "inbox"))
        # Firehose / in-app-only categories default OFF for email even
        # though they default ON for push.
        self.assertFalse(should_email(self.user2.id, "chats"))
        self.assertFalse(should_email(self.user2.id, "reactions"))
        self.assertFalse(should_email(self.user2.id, "agent_run_done"))

    def test_unknown_category_fails_closed(self):
        # Push fails open (default True) for unknown categories; email must
        # fail CLOSED — an unclassified event never generates mail.
        self.assertFalse(should_email(self.user2.id, "some_future_category"))
        NotificationPreference.objects.create(user=self.user2)
        self.assertFalse(should_email(self.user2.id, "some_future_category"))

    def test_email_enabled_false_blocks(self):
        NotificationPreference.objects.create(user=self.user2, email_enabled=False)
        self.assertFalse(should_email(self.user2.id, "mention_chat"))

    def test_master_disabled_blocks(self):
        NotificationPreference.objects.create(user=self.user2, master_enabled=False)
        self.assertFalse(should_email(self.user2.id, "mention_chat"))

    def test_push_enabled_false_does_not_block_email(self):
        # The two channel masters are independent: turning push off must
        # not silently kill email.
        NotificationPreference.objects.create(user=self.user2, push_enabled=False)
        self.assertTrue(should_email(self.user2.id, "mention_chat"))

    def test_coarse_mentions_off_blocks(self):
        NotificationPreference.objects.create(user=self.user2, enable_mentions=False)
        self.assertFalse(should_email(self.user2.id, "mention_chat"))
        self.assertFalse(should_email(self.user2.id, "mention_task"))

    def test_task_assign_has_no_coarse_gate(self):
        # Like `reactions` for push: no coarse column exists for
        # task_assign, so unrelated coarse toggles must not affect it.
        NotificationPreference.objects.create(user=self.user2, enable_mentions=False)
        self.assertTrue(should_email(self.user2.id, "task_assign"))

    def test_prefixed_override_beats_default(self):
        NotificationPreference.objects.create(
            user=self.user2,
            category_settings={"email:chats": True, "email:mention_chat": False},
        )
        self.assertTrue(should_email(self.user2.id, "chats"))
        self.assertFalse(should_email(self.user2.id, "mention_chat"))

    def test_unprefixed_key_never_leaks_into_email(self):
        # An unprefixed key is an in-app/push override. Email's defaults
        # differ, so inheriting it would flip categories the user only
        # meant to change for the other channels.
        NotificationPreference.objects.create(
            user=self.user2,
            category_settings={"chats": True, "mention_chat": False},
        )
        self.assertFalse(should_email(self.user2.id, "chats"))
        self.assertTrue(should_email(self.user2.id, "mention_chat"))

    def test_default_row_allows(self):
        NotificationPreference.objects.create(user=self.user2)
        self.assertTrue(should_email(self.user2.id, "mention_chat"))


class EmailEnabledPreferenceEndpointTests(BaseAPITestCase):
    """`email_enabled` rides the existing notification-preferences view."""

    def setUp(self):
        super().setUp()
        self.url = reverse("user_notification_preferences")

    def test_defaults_true_on_lazy_create(self):
        self.authenticate()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["email_enabled"])

    def test_put_false_persists(self):
        self.authenticate()
        resp = self.client.put(self.url, {"email_enabled": False}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["email_enabled"])
        prefs = NotificationPreference.objects.get(user=self.user)
        self.assertFalse(prefs.email_enabled)
        # Independent of the push master.
        self.assertTrue(prefs.push_enabled)

    def test_put_accepts_prefixed_category_keys(self):
        # The serializer's no-allowlist stance must admit `email:` keys.
        self.authenticate()
        resp = self.client.put(
            self.url,
            {"category_settings": {"email:chats": True, "mention_chat": False}},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        prefs = NotificationPreference.objects.get(user=self.user)
        self.assertEqual(
            prefs.category_settings,
            {"email:chats": True, "mention_chat": False},
        )


class LanguagePreferenceViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("language_preference")

    def test_unauthenticated_returns_401(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_get_default_is_empty(self):
        self.authenticate()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["language"], "")

    def test_patch_known_locale_stores(self):
        self.authenticate()
        resp = self.client.patch(self.url, {"language": "ja"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["language"], "ja")
        self.user.refresh_from_db()
        self.assertEqual(self.user.language, "ja")

    def test_patch_regioned_tag_normalizes_to_primary_subtag(self):
        self.authenticate()
        resp = self.client.patch(self.url, {"language": "en-US"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["language"], "en")
        self.user.refresh_from_db()
        self.assertEqual(self.user.language, "en")

    def test_patch_unknown_locale_stores_null_and_200s(self):
        # Machine-reported value: an unknown locale is not a client bug the
        # user can act on, so it must not fail the boot-time sync.
        self.authenticate()
        self.user.language = "ja"
        self.user.save(update_fields=["language"])
        resp = self.client.patch(self.url, {"language": "tlh"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["language"], "")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.language)

    def test_patch_non_string_returns_400(self):
        self.authenticate()
        resp = self.client.patch(self.url, {"language": 7}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


@override_settings(DEFAULT_FROM_EMAIL="Genos <noreply@genos.test>")
class SendTemplatedEmailHeadersTests(TestCase):
    _CONTEXT = {
        "user_name": "Alice",
        "reset_url": "https://app/reset?t=xyz",
        "expiry_minutes": 30,
    }

    def test_headers_reach_the_message(self):
        send_templated_email(
            to="recipient@example.com",
            subject="Reset your password",
            template_base="password_reset",
            context=self._CONTEXT,
            headers={"List-Unsubscribe": "<https://api/unsub/tok/>"},
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(
            mail.outbox[0].extra_headers,
            {"List-Unsubscribe": "<https://api/unsub/tok/>"},
        )

    def test_headers_omitted_keeps_transactional_path_unchanged(self):
        send_templated_email(
            to="recipient@example.com",
            subject="Reset your password",
            template_base="password_reset",
            context=self._CONTEXT,
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].extra_headers, {})
