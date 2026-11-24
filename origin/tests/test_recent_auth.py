"""A stolen session must not be enough to make a lasting foothold (MIP-2240).

API keys, webhook endpoints and AI-app connections ask for the password again
(`POST /api/v2/user/confirm-password/` answers a short-lived token the creating call carries),
and the owner gets an email when one is made. Each test below fails on the code before this
change: the creation used to go through on the access token alone and sent no email.
"""

from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from origin.models.common.api_key_models import ApiKey
from origin.models.common.webhook_models import WebhookEndpoint
from origin.services import credential_alert, recent_auth
from origin.tests.test_base import BaseAPITestCase

CONFIRM = "/api/v2/user/confirm-password/"
KEYS = "/api/v2/api-keys/"
WEBHOOKS = "/api/v2/webhooks/"

_PUBLIC_DNS = patch(
    "origin.services.outbound_url.socket.getaddrinfo",
    return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
)


class RecentAuthBase(BaseAPITestCase):
    recent_auth_gate = True

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.authenticate()

    def confirm(self, password="testpass123"):
        return self.client.post(CONFIRM, {"password": password}, format="json")

    def token(self):
        res = self.confirm()
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["recent_auth_token"]


class TestConfirmPassword(RecentAuthBase):
    def test_the_right_password_gives_a_token(self):
        res = self.confirm()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["expires_in"], recent_auth.WINDOW_SECONDS)
        self.assertTrue(recent_auth.is_valid(self.user, body["recent_auth_token"]))

    def test_a_wrong_password_gives_nothing(self):
        res = self.confirm("not-the-password")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("recent_auth_token", res.json())

    def test_a_missing_or_non_text_password_is_refused_not_a_500(self):
        for body in ({}, {"password": 12345}, {"password": ["a"]}, {"password": ""}):
            res = self.client.post(CONFIRM, body, format="json")
            self.assertEqual(res.status_code, 403, body)

    def test_it_needs_a_session(self):
        self.unauthenticate()
        self.assertEqual(self.confirm().status_code, 401)

    def test_guessing_is_throttled_per_member(self):
        codes = [self.confirm(f"guess-{i}").status_code for i in range(7)]
        self.assertEqual(codes[:5], [403] * 5)
        self.assertEqual(codes[5], 429)
        # ... and the right password does not get through while it is refused.
        self.assertEqual(self.confirm().status_code, 429)

    def test_an_account_with_no_password_is_told_nothing_is_needed(self):
        self.user.set_unusable_password()
        self.user.save()
        self.authenticate()  # a session is bound to the password hash: sign in again
        res = self.confirm()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"required": False})


class TestTokenRules(RecentAuthBase):
    def test_it_expires(self):
        token = self.token()
        with patch("django.core.signing.time.time", return_value=timezone.now().timestamp()):
            self.assertTrue(recent_auth.is_valid(self.user, token))
        later = (timezone.now() + timedelta(seconds=recent_auth.WINDOW_SECONDS + 5)).timestamp()
        with patch("django.core.signing.time.time", return_value=later):
            self.assertFalse(recent_auth.is_valid(self.user, token))

    def test_it_is_for_one_member(self):
        self.assertFalse(recent_auth.is_valid(self.user2, self.token()))

    def test_a_new_password_voids_it(self):
        token = self.token()
        self.user.set_password("a-brand-new-pass-77")
        self.user.save()
        self.assertFalse(recent_auth.is_valid(self.user, token))

    def test_junk_is_not_a_token(self):
        for junk in (None, "", "abc", 5, ["x"], "x" * 1000):
            self.assertFalse(recent_auth.is_valid(self.user, junk), junk)


class TestApiKeyNeedsThePassword(RecentAuthBase):
    BODY = {"name": "ci", "scope": "write", "allow_destructive": True, "expires_in_days": 365}

    def test_no_token_no_key(self):
        res = self.client.post(KEYS, self.BODY, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "recent_auth_required")
        self.assertFalse(ApiKey.objects.filter(user=self.user).exists())

    def test_a_forged_or_other_members_token_is_no_token(self):
        other = self.get_access_token(self.user2)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {other}")
        other_token = self.client.post(CONFIRM, {"password": "otherpass123"}, format="json")
        self.authenticate()
        for token in ("forged", other_token.json()["recent_auth_token"]):
            res = self.client.post(KEYS, {**self.BODY, "recent_auth_token": token}, format="json")
            self.assertEqual(res.status_code, 403)
        self.assertFalse(ApiKey.objects.filter(user=self.user).exists())

    def test_with_a_token_the_key_is_made(self):
        res = self.client.post(
            KEYS, {**self.BODY, "recent_auth_token": self.token()}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(ApiKey.objects.filter(user=self.user).exists())

    def test_an_account_with_no_password_is_not_asked(self):
        self.user.set_unusable_password()
        self.user.save()
        self.authenticate()  # a session is bound to the password hash: sign in again
        res = self.client.post(KEYS, self.BODY, format="json")
        self.assertEqual(res.status_code, 201, res.content)

    def test_the_key_is_not_in_the_refusal(self):
        res = self.client.post(KEYS, self.BODY, format="json")
        self.assertNotIn("key", res.json())


class TestWebhookNeedsThePassword(RecentAuthBase):
    def body(self, **extra):
        return {
            "team_id": str(self.team.team_id),
            "url": "https://hooks.example.com/in/secret-path-123",
            "events": ["task.created"],
            **extra,
        }

    def test_no_token_no_webhook(self):
        with _PUBLIC_DNS:
            res = self.client.post(WEBHOOKS, self.body(), format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "recent_auth_required")
        self.assertFalse(WebhookEndpoint.objects.exists())

    def test_with_a_token_the_webhook_is_made(self):
        with _PUBLIC_DNS:
            res = self.client.post(
                WEBHOOKS, self.body(recent_auth_token=self.token()), format="json"
            )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(WebhookEndpoint.objects.count(), 1)


class TestOwnerEmail(RecentAuthBase):
    def test_a_new_key_is_mailed_to_its_owner_without_the_key(self):
        token = self.token()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                KEYS,
                {"name": "build box", "scope": "read", "recent_auth_token": token},
                format="json",
            )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [self.user.email])
        self.assertIn("build box", message.body)
        self.assertNotIn(res.json()["key"], message.body)
        self.assertNotIn(res.json()["key"], message.alternatives[0][0])

    def test_a_new_webhook_is_mailed_by_host_only(self):
        token = self.token()
        with _PUBLIC_DNS, self.captureOnCommitCallbacks(execute=True):
            res = self.client.post(
                WEBHOOKS,
                {
                    "team_id": str(self.team.team_id),
                    "url": "https://hooks.example.com/in/secret-path-123",
                    "events": ["task.created"],
                    "recent_auth_token": token,
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("hooks.example.com", mail.outbox[0].body)
        self.assertNotIn("secret-path-123", mail.outbox[0].body)
        self.assertNotIn(res.json()["secret"], mail.outbox[0].body)

    def test_a_refused_creation_sends_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(KEYS, {"name": "x"}, format="json")
        self.assertEqual(mail.outbox, [])

    def test_a_failed_send_does_not_undo_the_key(self):
        token = self.token()
        with (
            patch(
                "origin.services.credential_alert.send_templated_email", side_effect=OSError("down")
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            res = self.client.post(KEYS, {"name": "x", "recent_auth_token": token}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertTrue(ApiKey.objects.filter(user=self.user, name="x").exists())

    def test_every_language_renders_and_a_name_cannot_bend_the_mail(self):
        for lang in credential_alert.LOCALES:
            self.user.language = lang
            for kind in (
                credential_alert.KIND_API_KEY,
                credential_alert.KIND_WEBHOOK,
                credential_alert.KIND_CONNECTION,
            ):
                msg = credential_alert.build_message(
                    self.user,
                    kind=kind,
                    label="a\r\nBcc: x@evil.test" + "z" * 200,
                    when=timezone.now(),
                )
                self.assertNotIn("\n", msg["subject"])
                self.assertNotIn("\n", msg["context"]["body"])
                self.assertLessEqual(len(msg["context"]["body"]), 400)
                self.assertNotIn("{", msg["subject"] + msg["context"]["body"], (lang, kind))
        self.user.language = "ar"
        self.assertEqual(
            credential_alert.build_message(
                self.user, kind="api_key", label="x", when=timezone.now()
            )["context"]["dir"],
            "rtl",
        )
        self.user.language = "xx"
        self.assertEqual(credential_alert._locale(self.user), "en")


@override_settings(DEBUG=False)
class TestSendsRealTemplates(RecentAuthBase):
    def test_html_and_text_parts_render_in_japanese(self):
        self.user.language = "ja"
        self.user.save()
        token = self.token()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(KEYS, {"name": "鍵", "recent_auth_token": token}, format="json")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("API キー", mail.outbox[0].subject)
        self.assertIn('lang="ja"', mail.outbox[0].alternatives[0][0])


from origin.models.common.mcp_connection_models import McpConnection  # noqa: E402
from origin.tests.test_mcp_oauth_consent import ConsentBase  # noqa: E402


class TestConsentNeedsThePassword(ConsentBase):
    """Connecting an AI app: the password again, and an email when it is a new app."""

    recent_auth_gate = True

    def _token(self):
        res = self.client.post(CONFIRM, {"password": "testpass123"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["recent_auth_token"]

    def test_allowing_without_the_password_writes_nothing(self):
        res = self.decide()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "recent_auth_required")
        self.assertEqual(McpConnection.objects.count(), 0)

    def test_allowing_with_the_password_connects_and_mails_the_owner(self):
        token = self._token()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.decide(recent_auth_token=token)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(McpConnection.objects.filter(user=self.user).count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertIn("Claude", mail.outbox[0].body)

    def test_consenting_again_with_wider_scopes_also_asks(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.decide(recent_auth_token=self._token()).status_code, 200)
        self.assertEqual(self.decide().status_code, 403)

    def test_a_refusal_needs_no_password(self):
        res = self.decide(allow=False)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(McpConnection.objects.count(), 0)
        self.assertEqual(mail.outbox, [])


from origin.tests.test_mcp_connections import ConnectionsBase, detail  # noqa: E402


class TestWideningAnAppAsksForThePassword(ConnectionsBase):
    """Raising a connected app from read to write, switching deleting on or adding a team is
    the same foothold as connecting it, so it asks too; narrowing never does."""

    recent_auth_gate = True

    def setUp(self):
        super().setUp()
        self.conn.scope = "read"  # the fixture's app starts with write
        self.conn.save()

    def patch(self, body):
        return self.client.patch(detail(self.conn), body, format="json")

    def test_widening_without_the_password_changes_nothing(self):
        for body in (
            {"scopes": ["mcp:read", "mcp:write"]},
            {"scopes": ["mcp:read", "mcp:write", "mcp:destructive"]},
        ):
            res = self.patch(body)
            self.assertEqual(res.status_code, 403, body)
            self.assertEqual(res.json()["code"], "recent_auth_required")
        self.conn.refresh_from_db()
        self.assertEqual((self.conn.scope, self.conn.allow_destructive), ("read", False))

    def test_adding_a_team_asks_too(self):
        other = self.team_of(self.user)
        res = self.patch(
            {
                "team_ids": [str(self.team.team_id), str(other.team_id)],
                "default_team_id": str(self.team.team_id),
            }
        )
        self.assertEqual(res.status_code, 403)

    def test_widening_with_the_password_works(self):
        token = self.client.post(CONFIRM, {"password": "testpass123"}, format="json").json()[
            "recent_auth_token"
        ]
        res = self.patch({"scopes": ["mcp:read", "mcp:write"], "recent_auth_token": token})
        self.assertEqual(res.status_code, 200, res.content)
        self.conn.refresh_from_db()
        self.assertEqual(self.conn.scope, "write")

    def test_narrowing_and_keeping_the_same_need_no_password(self):
        token = self.client.post(CONFIRM, {"password": "testpass123"}, format="json").json()[
            "recent_auth_token"
        ]
        self.assertEqual(
            self.patch(
                {"scopes": ["mcp:read", "mcp:write"], "recent_auth_token": token}
            ).status_code,
            200,
        )
        self.assertEqual(self.patch({"scopes": ["mcp:read"]}).status_code, 200)
        self.assertEqual(self.patch({"scopes": ["mcp:read"]}).status_code, 200)
