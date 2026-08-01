"""Tests for the anonymous unsubscribe path (PR A3 of the email series):
token round-trip, the scanner trap (GET must be side-effect-free), the
single-key write semantics, and the List-Unsubscribe header builder.
"""

import uuid

from django.test import override_settings

from origin.models.common.notification_models import NotificationPreference
from origin.services.email_unsubscribe import (
    SCOPE_ALL,
    make_token,
    parse_token,
    unsubscribe_headers,
    unsubscribe_url,
)
from origin.tests.test_base import BaseAPITestCase


def _url(token: str) -> str:
    return f"/api/v2/email/unsubscribe/{token}/"


class TokenTests(BaseAPITestCase):
    def test_round_trip(self):
        token = make_token(self.user.id, "mention_chat")
        self.assertEqual(parse_token(token), (str(self.user.id), "mention_chat"))

    def test_tampered_token_is_rejected(self):
        token = make_token(self.user.id, SCOPE_ALL)
        self.assertIsNone(parse_token(token[:-2] + "xx"))

    def test_garbage_is_rejected(self):
        self.assertIsNone(parse_token("not-a-token"))


class HeaderBuilderTests(BaseAPITestCase):
    @override_settings(API_PUBLIC_BASE_URL="https://api.genos.test")
    def test_headers_carry_one_click_pair(self):
        headers = unsubscribe_headers(self.user.id)
        self.assertEqual(set(headers), {"List-Unsubscribe", "List-Unsubscribe-Post"})
        self.assertEqual(headers["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        url = headers["List-Unsubscribe"]
        self.assertTrue(url.startswith("<https://api.genos.test/api/v2/email/unsubscribe/"))
        self.assertTrue(url.endswith("/>"))

    @override_settings(API_PUBLIC_BASE_URL="")
    def test_no_base_url_means_no_headers(self):
        # Better to omit the header than emit a broken link.
        self.assertEqual(unsubscribe_headers(self.user.id), {})
        self.assertIsNone(unsubscribe_url(self.user.id))


class UnsubscribeGetTests(BaseAPITestCase):
    def test_get_renders_confirm_and_has_NO_side_effect(self):
        # THE trap: corporate mail scanners prefetch every GET link in
        # every email. A GET that unsubscribes would silently unsubscribe
        # whole companies. GET must confirm only.
        token = make_token(self.user.id, SCOPE_ALL)
        resp = self.client.get(_url(token))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "<form", html=False)
        # No prefs row is created, let alone mutated.
        self.assertFalse(NotificationPreference.objects.filter(user=self.user).exists())

    def test_get_with_existing_prefs_leaves_them_untouched(self):
        NotificationPreference.objects.create(user=self.user)
        token = make_token(self.user.id, "mention_chat")
        self.client.get(_url(token))
        prefs = NotificationPreference.objects.get(user=self.user)
        self.assertTrue(prefs.email_enabled)
        self.assertEqual(prefs.category_settings, {})

    def test_get_tampered_token_400s(self):
        resp = self.client.get(_url("garbage"))
        self.assertEqual(resp.status_code, 400)

    def test_get_unknown_user_400s(self):
        resp = self.client.get(_url(make_token(uuid.uuid4(), SCOPE_ALL)))
        self.assertEqual(resp.status_code, 400)

    def test_digest_scope_is_reserved_until_a6(self):
        resp = self.client.get(_url(make_token(self.user.id, "digest")))
        self.assertEqual(resp.status_code, 400)


class UnsubscribePostTests(BaseAPITestCase):
    def test_post_all_scope_flips_email_enabled(self):
        token = make_token(self.user.id, SCOPE_ALL)
        resp = self.client.post(_url(token))
        self.assertEqual(resp.status_code, 200)
        prefs = NotificationPreference.objects.get(user=self.user)
        self.assertFalse(prefs.email_enabled)
        # Only the email channel — in-app/push masters untouched.
        self.assertTrue(prefs.master_enabled)
        self.assertTrue(prefs.push_enabled)

    def test_post_category_scope_writes_single_prefixed_key(self):
        NotificationPreference.objects.create(
            user=self.user,
            category_settings={"mention_chat": False, "email:chats": True},
        )
        token = make_token(self.user.id, "mention_chat")
        resp = self.client.post(_url(token))
        self.assertEqual(resp.status_code, 200)
        prefs = NotificationPreference.objects.get(user=self.user)
        # The one key was written; every existing key survived (no
        # full-map replace).
        self.assertEqual(
            prefs.category_settings,
            {"mention_chat": False, "email:chats": True, "email:mention_chat": False},
        )
        self.assertTrue(prefs.email_enabled)

    def test_double_post_is_idempotent(self):
        token = make_token(self.user.id, SCOPE_ALL)
        self.assertEqual(self.client.post(_url(token)).status_code, 200)
        self.assertEqual(self.client.post(_url(token)).status_code, 200)
        self.assertFalse(NotificationPreference.objects.get(user=self.user).email_enabled)

    def test_one_click_form_post_works_without_csrf(self):
        # RFC 8058: mail providers POST `List-Unsubscribe=One-Click` as a
        # form body, with no cookies and no CSRF token.
        token = make_token(self.user.id, SCOPE_ALL)
        resp = self.client.post(
            _url(token),
            data="List-Unsubscribe=One-Click",
            content_type="application/x-www-form-urlencoded",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(NotificationPreference.objects.get(user=self.user).email_enabled)

    def test_post_tampered_token_mutates_nothing(self):
        resp = self.client.post(_url("garbage"))
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(NotificationPreference.objects.filter(user=self.user).exists())
