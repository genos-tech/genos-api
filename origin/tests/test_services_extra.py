"""Extra coverage for `origin/services/` modules not already exercised
elsewhere.

Focus areas:
  - crypto.py            — Fernet encrypt/decrypt round-trip + the lazy
                           "key not set" error. (No prior coverage.)
  - oauth/tokens.py      — get_valid_access_token refresh policy, the
                           ReauthRequired translation, _is_invalid_grant.
  - oauth/registry.py    — provider lookup + unknown-provider error.
  - oauth/github.py      — exchange_code / refresh / fetch_profile /
                           authorize_url / supports_refresh.
  - oauth/google.py      — exchange_code / refresh / authorize_url /
                           fetch_profile.
  - services/email.py    — send_templated_email lands in mail.outbox.
  - services/task_cache.py — get/set/invalidate against the real
                           LocMemCache configured in settings_test.
  - github_webhooks.py   — only the seams NOT covered by
                           test_github_webhook_registration.py:
                           parse_pr_url_full, _webhook_payload_url, the
                           IntegrityError race on create, the 201
                           unparseable-id path, and the
                           registered_by fall-back inside the events
                           sync.

External services (OpenSearch / LLM / network) are never hit: every
outbound `requests` call is patched at its call site, and Fernet is the
only crypto primitive used (it is local, no network).
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.models.common.user_models import (
    ConnectedAccount,
    GithubWebhookRegistration,
)
from origin.services import crypto, task_cache
from origin.services.email import send_templated_email
from origin.services.github_webhooks import (
    _webhook_payload_url,
    ensure_repo_webhook,
    parse_pr_url_full,
)
from origin.services.oauth.base import ProviderProfile, TokenResponse
from origin.services.oauth.github import GitHubOAuthProvider
from origin.services.oauth.google import GoogleOAuthProvider
from origin.services.oauth.registry import (
    get_provider,
    supported_provider_names,
)
from origin.services.oauth.tokens import (
    REFRESH_LEEWAY_SECONDS,
    ReauthRequired,
    _is_invalid_grant,
    get_valid_access_token,
)
from origin.tests.test_base import BaseAPITestCase

# A valid Fernet key generated once for round-trip tests.
_TEST_KEY = Fernet.generate_key().decode()


# ──────────────────────────────────────────────────────────────────────
# crypto.py
# ──────────────────────────────────────────────────────────────────────
class TestCrypto(TestCase):
    """Fernet helpers. `_get_fernet` is `@lru_cache`d, so we must clear
    the cache around every test for `@override_settings` to take effect —
    otherwise a key cached by a previous test leaks across cases."""

    def setUp(self):
        crypto._get_fernet.cache_clear()

    def tearDown(self):
        crypto._get_fernet.cache_clear()

    @override_settings(OAUTH_TOKEN_ENCRYPTION_KEY=_TEST_KEY)
    def test_encrypt_decrypt_round_trip(self):
        secret = "ghp_super_secret_token_value"
        ciphertext = crypto.encrypt(secret)
        # Ciphertext must not equal the plaintext, and decrypt restores it.
        self.assertNotEqual(ciphertext, secret)
        self.assertEqual(crypto.decrypt(ciphertext), secret)

    @override_settings(OAUTH_TOKEN_ENCRYPTION_KEY=_TEST_KEY)
    def test_encrypt_is_nondeterministic(self):
        # Fernet embeds a random IV + timestamp, so two encryptions of the
        # same plaintext differ — but both decrypt back to the same value.
        secret = "same-plaintext"
        c1 = crypto.encrypt(secret)
        c2 = crypto.encrypt(secret)
        self.assertNotEqual(c1, c2)
        self.assertEqual(crypto.decrypt(c1), secret)
        self.assertEqual(crypto.decrypt(c2), secret)

    @override_settings(OAUTH_TOKEN_ENCRYPTION_KEY=_TEST_KEY)
    def test_round_trip_unicode_and_empty(self):
        for value in ["", "ünïcödë-🚀-token", "a" * 5000]:
            crypto._get_fernet.cache_clear()  # not needed, but harmless
            self.assertEqual(crypto.decrypt(crypto.encrypt(value)), value)

    @override_settings(OAUTH_TOKEN_ENCRYPTION_KEY="")
    def test_empty_key_raises_clear_runtime_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            crypto.encrypt("anything")
        self.assertIn("OAUTH_TOKEN_ENCRYPTION_KEY is not set", str(ctx.exception))
        # decrypt funnels through the same lazy getter.
        crypto._get_fernet.cache_clear()
        with self.assertRaises(RuntimeError):
            crypto.decrypt("anything")

    @override_settings(OAUTH_TOKEN_ENCRYPTION_KEY=_TEST_KEY)
    def test_decrypt_rejects_tampered_ciphertext(self):
        secret = "tamper-me"
        ciphertext = crypto.encrypt(secret)
        # Flip a character in the middle to break the HMAC.
        mid = len(ciphertext) // 2
        tampered = ciphertext[:mid] + ("A" if ciphertext[mid] != "A" else "B") + ciphertext[mid + 1 :]
        with self.assertRaises(InvalidToken):
            crypto.decrypt(tampered)

    @override_settings(OAUTH_TOKEN_ENCRYPTION_KEY=_TEST_KEY)
    def test_decrypt_with_different_key_fails(self):
        ciphertext = crypto.encrypt("cross-key")
        # Swap the key and clear the lru_cache so the new key is picked up.
        other_key = Fernet.generate_key().decode()
        with override_settings(OAUTH_TOKEN_ENCRYPTION_KEY=other_key):
            crypto._get_fernet.cache_clear()
            with self.assertRaises(InvalidToken):
                crypto.decrypt(ciphertext)

    @override_settings(OAUTH_TOKEN_ENCRYPTION_KEY=_TEST_KEY.encode())
    def test_key_may_be_bytes(self):
        # _get_fernet handles a bytes key (key.encode() is skipped when it
        # is already bytes).
        self.assertEqual(crypto.decrypt(crypto.encrypt("bytes-key")), "bytes-key")


# ──────────────────────────────────────────────────────────────────────
# oauth/registry.py
# ──────────────────────────────────────────────────────────────────────
class TestOAuthRegistry(TestCase):
    def test_get_known_providers(self):
        self.assertIsInstance(get_provider("google"), GoogleOAuthProvider)
        self.assertIsInstance(get_provider("github"), GitHubOAuthProvider)

    def test_unknown_provider_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            get_provider("microsoft")
        self.assertIn("microsoft", str(ctx.exception))

    def test_supported_provider_names(self):
        names = supported_provider_names()
        self.assertEqual(set(names), {"google", "github"})


# ──────────────────────────────────────────────────────────────────────
# oauth/github.py  (HTTP mocked at the requests call site)
# ──────────────────────────────────────────────────────────────────────
@override_settings(
    GITHUB_OAUTH_CLIENT_ID="gh-client-id",
    GITHUB_OAUTH_CLIENT_SECRET="gh-client-secret",
)
class TestGitHubProvider(TestCase):
    def setUp(self):
        self.provider = GitHubOAuthProvider()

    def test_supports_refresh_is_false(self):
        self.assertFalse(self.provider.supports_refresh)

    def test_refresh_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.provider.refresh(refresh_token="anything")

    def test_authorize_url_login_uses_login_scopes(self):
        url = self.provider.authorize_url(
            state="st", intent="login", redirect_uri="https://app/cb"
        )
        self.assertTrue(url.startswith("https://github.com/login/oauth/authorize?"))
        self.assertIn("client_id=gh-client-id", url)
        self.assertIn("state=st", url)
        # login scopes only — no `repo`.
        self.assertIn("read%3Auser", url)  # "read:user" urlencoded
        self.assertNotIn("repo", url)

    def test_authorize_url_connect_adds_repo_scope(self):
        url = self.provider.authorize_url(
            state="st", intent="connect", redirect_uri="https://app/cb"
        )
        self.assertIn("repo", url)

    @patch("origin.services.oauth.github.requests.post")
    def test_exchange_code_success(self, mock_post):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 200
        resp.json.return_value = {"access_token": "ghp_abc", "scope": "repo,read:user"}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp

        token = self.provider.exchange_code(code="thecode", redirect_uri="https://app/cb")
        self.assertIsInstance(token, TokenResponse)
        self.assertEqual(token.access_token, "ghp_abc")
        self.assertIsNone(token.refresh_token)
        self.assertIsNone(token.expires_in_seconds)
        # GitHub scope string is comma-separated.
        self.assertEqual(token.granted_scopes, ["repo", "read:user"])
        # Asked for JSON back.
        self.assertEqual(mock_post.call_args.kwargs["headers"]["Accept"], "application/json")

    @patch("origin.services.oauth.github.requests.post")
    def test_exchange_code_empty_scope_yields_empty_list(self, mock_post):
        resp = MagicMock(spec=requests.Response)
        resp.json.return_value = {"access_token": "ghp_abc"}  # no scope key
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        token = self.provider.exchange_code(code="c", redirect_uri="r")
        self.assertEqual(token.granted_scopes, [])

    @patch("origin.services.oauth.github.requests.post")
    def test_exchange_code_error_payload_raises(self, mock_post):
        # GitHub returns HTTP 200 with {"error": ...} for a bad code.
        resp = MagicMock(spec=requests.Response)
        resp.json.return_value = {"error": "bad_verification_code"}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        with self.assertRaises(RuntimeError) as ctx:
            self.provider.exchange_code(code="c", redirect_uri="r")
        self.assertIn("GitHub OAuth exchange failed", str(ctx.exception))

    @patch("origin.services.oauth.github.requests.get")
    def test_fetch_profile_uses_public_email(self, mock_get):
        resp = MagicMock(spec=requests.Response)
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"id": 555, "email": "pub@x.com", "name": "Pub"}
        mock_get.return_value = resp
        profile = self.provider.fetch_profile(access_token="ghp")
        self.assertIsInstance(profile, ProviderProfile)
        self.assertEqual(profile.provider_user_id, "555")  # coerced to str
        self.assertEqual(profile.email, "pub@x.com")
        self.assertEqual(profile.display_name, "Pub")
        # Only the /user endpoint was hit (public email present).
        self.assertEqual(mock_get.call_count, 1)

    @patch("origin.services.oauth.github.requests.get")
    def test_fetch_profile_falls_back_to_emails_endpoint(self, mock_get):
        user_resp = MagicMock(spec=requests.Response)
        user_resp.raise_for_status.return_value = None
        user_resp.json.return_value = {"id": 7, "email": None, "login": "octo"}
        emails_resp = MagicMock(spec=requests.Response)
        emails_resp.raise_for_status.return_value = None
        emails_resp.json.return_value = [
            {"email": "secondary@x.com", "primary": False, "verified": True},
            {"email": "primary@x.com", "primary": True, "verified": True},
        ]
        mock_get.side_effect = [user_resp, emails_resp]

        profile = self.provider.fetch_profile(access_token="ghp")
        self.assertEqual(profile.email, "primary@x.com")
        # display_name falls back to login when name is absent.
        self.assertEqual(profile.display_name, "octo")
        self.assertEqual(mock_get.call_count, 2)

    @patch("origin.services.oauth.github.requests.get")
    def test_fetch_profile_emails_network_error_is_swallowed(self, mock_get):
        user_resp = MagicMock(spec=requests.Response)
        user_resp.raise_for_status.return_value = None
        user_resp.json.return_value = {"id": 9, "email": None, "login": "ghost"}
        # Second call (the /user/emails fallback) raises a RequestException;
        # provider must swallow it and return a None email.
        mock_get.side_effect = [user_resp, requests.ConnectionError("down")]
        profile = self.provider.fetch_profile(access_token="ghp")
        self.assertIsNone(profile.email)
        self.assertEqual(profile.provider_user_id, "9")


# ──────────────────────────────────────────────────────────────────────
# oauth/google.py
# ──────────────────────────────────────────────────────────────────────
@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="goog-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="goog-secret",
)
class TestGoogleProvider(TestCase):
    def setUp(self):
        self.provider = GoogleOAuthProvider()

    def test_supports_refresh_is_true_by_default(self):
        self.assertTrue(self.provider.supports_refresh)

    def test_authorize_url_login_omits_offline_consent(self):
        url = self.provider.authorize_url(
            state="s", intent="login", redirect_uri="https://app/cb"
        )
        self.assertIn("response_type=code", url)
        self.assertNotIn("access_type=offline", url)
        self.assertNotIn("prompt=consent", url)
        # login scope set has no calendar scope.
        self.assertNotIn("calendar", url)

    def test_authorize_url_connect_forces_offline_consent_and_calendar(self):
        url = self.provider.authorize_url(
            state="s", intent="connect", redirect_uri="https://app/cb"
        )
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)
        self.assertIn("calendar", url)

    @patch("origin.services.oauth.google.requests.post")
    def test_exchange_code_parses_token_and_scopes(self, mock_post):
        resp = MagicMock(spec=requests.Response)
        resp.raise_for_status.return_value = None
        resp.json.return_value = {
            "access_token": "ya29.abc",
            "refresh_token": "1//refresh",
            "expires_in": 3600,
            "scope": "openid email profile",
        }
        mock_post.return_value = resp
        token = self.provider.exchange_code(code="c", redirect_uri="r")
        self.assertEqual(token.access_token, "ya29.abc")
        self.assertEqual(token.refresh_token, "1//refresh")
        self.assertEqual(token.expires_in_seconds, 3600)
        # Google scope string is space-separated.
        self.assertEqual(token.granted_scopes, ["openid", "email", "profile"])

    @patch("origin.services.oauth.google.requests.post")
    def test_refresh_omitted_refresh_token_is_none(self, mock_post):
        resp = MagicMock(spec=requests.Response)
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"access_token": "ya29.new", "expires_in": 3599}
        mock_post.return_value = resp
        token = self.provider.refresh(refresh_token="1//refresh")
        self.assertEqual(token.access_token, "ya29.new")
        # Google omits refresh_token on refresh — provider returns None.
        self.assertIsNone(token.refresh_token)
        self.assertEqual(token.expires_in_seconds, 3599)
        # Sent grant_type=refresh_token.
        self.assertEqual(mock_post.call_args.kwargs["data"]["grant_type"], "refresh_token")

    @patch("origin.services.oauth.google.requests.get")
    def test_fetch_profile(self, mock_get):
        resp = MagicMock(spec=requests.Response)
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"sub": "id-123", "email": "g@x.com", "name": "G User"}
        mock_get.return_value = resp
        profile = self.provider.fetch_profile(access_token="tok")
        self.assertEqual(profile.provider_user_id, "id-123")
        self.assertEqual(profile.email, "g@x.com")
        self.assertEqual(profile.display_name, "G User")


# ──────────────────────────────────────────────────────────────────────
# oauth/tokens.py  (get_valid_access_token + ReauthRequired)
# ──────────────────────────────────────────────────────────────────────
@override_settings(OAUTH_TOKEN_ENCRYPTION_KEY=_TEST_KEY)
class TestGetValidAccessToken(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        crypto._get_fernet.cache_clear()

    def tearDown(self):
        crypto._get_fernet.cache_clear()

    def _github_account(self):
        return ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="gh-1",
            access_token_encrypted=crypto.encrypt("ghp_live"),
        )

    def _google_account(self, expires_at=None, with_refresh=True):
        return ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="goog-1",
            access_token_encrypted=crypto.encrypt("ya29.current"),
            refresh_token_encrypted=crypto.encrypt("1//refresh") if with_refresh else None,
            access_token_expires_at=expires_at,
        )

    def test_github_token_returned_without_refresh(self):
        # GitHub supports_refresh=False → just decrypt and return.
        account = self._github_account()
        with patch("origin.services.oauth.tokens.get_provider") as mock_get_provider:
            provider = MagicMock()
            provider.supports_refresh = False
            mock_get_provider.return_value = provider
            token = get_valid_access_token(account)
        self.assertEqual(token, "ghp_live")
        # No refresh attempt was made on the provider.
        provider.refresh.assert_not_called()

    def test_valid_unexpired_google_token_not_refreshed(self):
        # Expiry comfortably beyond the leeway window → return as-is.
        future = timezone.now() + timedelta(seconds=REFRESH_LEEWAY_SECONDS + 600)
        account = self._google_account(expires_at=future)
        provider = MagicMock()
        provider.supports_refresh = True
        with patch("origin.services.oauth.tokens.get_provider", return_value=provider):
            token = get_valid_access_token(account)
        self.assertEqual(token, "ya29.current")
        provider.refresh.assert_not_called()

    def test_expired_google_token_triggers_refresh_and_persists(self):
        past = timezone.now() - timedelta(seconds=60)
        account = self._google_account(expires_at=past)
        provider = MagicMock()
        provider.supports_refresh = True
        provider.refresh.return_value = TokenResponse(
            access_token="ya29.fresh",
            refresh_token="1//newrefresh",
            expires_in_seconds=3600,
            granted_scopes=["openid"],
        )
        with patch("origin.services.oauth.tokens.get_provider", return_value=provider):
            token = get_valid_access_token(account)
        self.assertEqual(token, "ya29.fresh")
        provider.refresh.assert_called_once_with(refresh_token="1//refresh")
        # The new token + refresh + expiry are persisted (encrypted).
        account.refresh_from_db()
        self.assertEqual(crypto.decrypt(account.access_token_encrypted), "ya29.fresh")
        self.assertEqual(crypto.decrypt(account.refresh_token_encrypted), "1//newrefresh")
        self.assertIsNotNone(account.access_token_expires_at)
        self.assertGreater(account.access_token_expires_at, timezone.now())

    def test_refresh_keeps_old_refresh_token_when_none_returned(self):
        # Google typically omits refresh_token on refresh; the stored one
        # must be preserved.
        account = self._google_account(expires_at=None)  # None → needs refresh
        provider = MagicMock()
        provider.supports_refresh = True
        provider.refresh.return_value = TokenResponse(
            access_token="ya29.fresh2",
            refresh_token=None,
            expires_in_seconds=None,  # also exercise the "no expiry update" branch
            granted_scopes=[],
        )
        with patch("origin.services.oauth.tokens.get_provider", return_value=provider):
            token = get_valid_access_token(account)
        self.assertEqual(token, "ya29.fresh2")
        account.refresh_from_db()
        # Old refresh token unchanged.
        self.assertEqual(crypto.decrypt(account.refresh_token_encrypted), "1//refresh")
        # No expiry returned → field stays None.
        self.assertIsNone(account.access_token_expires_at)

    def test_missing_refresh_token_raises_reauth_required(self):
        account = self._google_account(expires_at=None, with_refresh=False)
        provider = MagicMock()
        provider.supports_refresh = True
        with patch("origin.services.oauth.tokens.get_provider", return_value=provider):
            with self.assertRaises(ReauthRequired) as ctx:
                get_valid_access_token(account)
        self.assertEqual(ctx.exception.account_id, account.id)
        self.assertEqual(ctx.exception.provider, "google")
        provider.refresh.assert_not_called()

    def test_invalid_grant_translated_to_reauth_required(self):
        past = timezone.now() - timedelta(seconds=60)
        account = self._google_account(expires_at=past)
        provider = MagicMock()
        provider.supports_refresh = True
        bad_resp = MagicMock(spec=requests.Response)
        bad_resp.status_code = 400
        bad_resp.json.return_value = {"error": "invalid_grant"}
        http_err = requests.HTTPError(response=bad_resp)
        provider.refresh.side_effect = http_err
        with patch("origin.services.oauth.tokens.get_provider", return_value=provider):
            with self.assertRaises(ReauthRequired):
                get_valid_access_token(account)

    def test_other_http_error_propagates(self):
        # A non-invalid_grant error (e.g. 500) is NOT swallowed.
        past = timezone.now() - timedelta(seconds=60)
        account = self._google_account(expires_at=past)
        provider = MagicMock()
        provider.supports_refresh = True
        bad_resp = MagicMock(spec=requests.Response)
        bad_resp.status_code = 500
        bad_resp.json.return_value = {}
        provider.refresh.side_effect = requests.HTTPError(response=bad_resp)
        with patch("origin.services.oauth.tokens.get_provider", return_value=provider):
            with self.assertRaises(requests.HTTPError):
                get_valid_access_token(account)

    def test_invalid_request_400_is_not_reauth(self):
        # 400 with a different error code (invalid_request) is a config bug
        # reconnecting won't fix → propagate as HTTPError, not ReauthRequired.
        past = timezone.now() - timedelta(seconds=60)
        account = self._google_account(expires_at=past)
        provider = MagicMock()
        provider.supports_refresh = True
        bad_resp = MagicMock(spec=requests.Response)
        bad_resp.status_code = 400
        bad_resp.json.return_value = {"error": "invalid_request"}
        provider.refresh.side_effect = requests.HTTPError(response=bad_resp)
        with patch("origin.services.oauth.tokens.get_provider", return_value=provider):
            with self.assertRaises(requests.HTTPError):
                get_valid_access_token(account)


class TestIsInvalidGrant(TestCase):
    """Direct unit tests for the _is_invalid_grant predicate."""

    def _err(self, status, json_body=None, json_raises=False):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = status
        if json_raises:
            resp.json.side_effect = ValueError("not json")
        else:
            resp.json.return_value = json_body
        return requests.HTTPError(response=resp)

    def test_true_for_400_invalid_grant(self):
        self.assertTrue(_is_invalid_grant(self._err(400, {"error": "invalid_grant"})))

    def test_false_for_400_other_error(self):
        self.assertFalse(_is_invalid_grant(self._err(400, {"error": "invalid_client"})))

    def test_false_for_non_400(self):
        self.assertFalse(_is_invalid_grant(self._err(401, {"error": "invalid_grant"})))

    def test_false_when_response_is_none(self):
        err = requests.HTTPError()
        err.response = None
        self.assertFalse(_is_invalid_grant(err))

    def test_false_when_body_not_json(self):
        self.assertFalse(_is_invalid_grant(self._err(400, json_raises=True)))

    def test_false_when_json_is_none(self):
        # resp.json() returning None must not blow up (covered by `or {}`).
        self.assertFalse(_is_invalid_grant(self._err(400, None)))


# ──────────────────────────────────────────────────────────────────────
# services/email.py
# ──────────────────────────────────────────────────────────────────────
@override_settings(DEFAULT_FROM_EMAIL="Genos <noreply@genos.test>")
class TestSendTemplatedEmail(TestCase):
    def test_password_reset_email_lands_in_outbox(self):
        send_templated_email(
            to="recipient@example.com",
            subject="Reset your password",
            template_base="password_reset",
            context={
                "user_name": "Alice",
                "reset_url": "https://app/reset?t=xyz",
                "expiry_minutes": 30,
            },
        )
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.subject, "Reset your password")
        self.assertEqual(msg.to, ["recipient@example.com"])
        self.assertEqual(msg.from_email, "Genos <noreply@genos.test>")
        # Text body rendered from the .txt template, with context substituted.
        self.assertIn("Alice", msg.body)
        self.assertIn("https://app/reset?t=xyz", msg.body)
        # An HTML alternative is attached.
        self.assertEqual(len(msg.alternatives), 1)
        html_body, mimetype = msg.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("https://app/reset?t=xyz", html_body)

    def test_email_verification_template(self):
        send_templated_email(
            to="newuser@example.com",
            subject="Verify your email",
            template_base="email_verification",
            context={
                "user_name": "Bob",
                "verify_url": "https://app/verify?t=abc",
                "expiry_hours": 24,
            },
        )
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Bob", mail.outbox[0].body)

    def test_missing_template_raises(self):
        # render_to_string raises TemplateDoesNotExist for an unknown base;
        # nothing is sent.
        from django.template import TemplateDoesNotExist

        with self.assertRaises(TemplateDoesNotExist):
            send_templated_email(
                to="x@example.com",
                subject="s",
                template_base="does_not_exist",
                context={},
            )
        self.assertEqual(len(mail.outbox), 0)


# ──────────────────────────────────────────────────────────────────────
# services/task_cache.py  (real LocMemCache)
# ──────────────────────────────────────────────────────────────────────
class TestTaskCache(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_set_then_get_round_trip(self):
        data = [{"task_id": 1, "title": "A"}]
        self.assertIsNone(task_cache.get_cached_project_tasks(5, 9))
        task_cache.set_cached_project_tasks(5, 9, data)
        self.assertEqual(task_cache.get_cached_project_tasks(5, 9), data)

    def test_key_stringifies_ints_and_strings_interchangeably(self):
        # int team/project and their str equivalents must map to one key.
        task_cache.set_cached_project_tasks(5, 9, "payload")
        self.assertEqual(task_cache.get_cached_project_tasks("5", "9"), "payload")

    def test_invalidate_clears_entry(self):
        task_cache.set_cached_project_tasks(1, 2, "x")
        task_cache.invalidate_project_tasks_cache(1, 2)
        self.assertIsNone(task_cache.get_cached_project_tasks(1, 2))

    def test_invalidate_with_none_is_noop(self):
        task_cache.set_cached_project_tasks(1, 2, "keep")
        # None args short-circuit and must NOT clear unrelated entries.
        task_cache.invalidate_project_tasks_cache(None, 2)
        task_cache.invalidate_project_tasks_cache(1, None)
        self.assertEqual(task_cache.get_cached_project_tasks(1, 2), "keep")

    def test_invalidate_for_task_uses_task_team_and_project(self):
        task_cache.set_cached_project_tasks(7, 8, "data")
        fake_task = MagicMock(team_id=7, project_id=8)
        task_cache.invalidate_for_task(fake_task)
        self.assertIsNone(task_cache.get_cached_project_tasks(7, 8))

    def test_invalidate_for_none_task_is_noop(self):
        # No crash, no effect.
        task_cache.invalidate_for_task(None)

    def test_invalidate_for_task_missing_attrs_is_noop(self):
        # getattr(..., None) on a task lacking team_id/project_id → both None
        # → invalidate short-circuits, unrelated entries survive.
        task_cache.set_cached_project_tasks(7, 8, "data")
        bare = object()
        task_cache.invalidate_for_task(bare)
        self.assertEqual(task_cache.get_cached_project_tasks(7, 8), "data")

    def test_different_teams_dont_collide(self):
        task_cache.set_cached_project_tasks(1, 9, "team1")
        task_cache.set_cached_project_tasks(2, 9, "team2")
        self.assertEqual(task_cache.get_cached_project_tasks(1, 9), "team1")
        self.assertEqual(task_cache.get_cached_project_tasks(2, 9), "team2")


# ──────────────────────────────────────────────────────────────────────
# github_webhooks.py — seams not covered by the existing registration test
# ──────────────────────────────────────────────────────────────────────
class TestParsePrUrlFull(TestCase):
    def test_valid_url_returns_owner_repo_number(self):
        self.assertEqual(
            parse_pr_url_full("https://github.com/acme/rocket/pull/42"),
            ("acme", "rocket", 42),
        )

    def test_trailing_slash_ok(self):
        self.assertEqual(
            parse_pr_url_full("https://github.com/acme/rocket/pull/7/"),
            ("acme", "rocket", 7),
        )

    def test_invalid_inputs_return_none(self):
        self.assertIsNone(parse_pr_url_full("https://github.com/acme/rocket"))
        self.assertIsNone(parse_pr_url_full("https://github.com/acme/rocket/issues/3"))
        self.assertIsNone(parse_pr_url_full(None))
        self.assertIsNone(parse_pr_url_full(123))


class TestWebhookPayloadUrl(TestCase):
    @override_settings(BACKEND_BASE_URL="https://api.example.com/")
    def test_strips_trailing_slash_and_appends_path(self):
        self.assertEqual(
            _webhook_payload_url(), "https://api.example.com/api/v2/github/webhook/"
        )

    @override_settings(BACKEND_BASE_URL="")
    def test_empty_base_url(self):
        # No base configured → path-only.
        self.assertEqual(_webhook_payload_url(), "/api/v2/github/webhook/")


@override_settings(
    GITHUB_WEBHOOK_SECRET="test-secret",
    BACKEND_BASE_URL="https://api.example.com",
)
class TestEnsureRepoWebhookExtraSeams(BaseAPITestCase):
    """Branches in `ensure_repo_webhook` /
    `_sync_existing_hook_events_if_needed` not hit by the existing
    registration test."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="123",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )

    def tearDown(self):
        cache.clear()

    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_x")
    def test_201_with_unparseable_id_returns_none(self, _token, mock_post):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 201
        resp.ok = True
        resp.json.return_value = {"id": "not-an-int"}
        mock_post.return_value = resp
        result = ensure_repo_webhook(self.user, "acme", "rocket")
        self.assertIsNone(result)
        self.assertEqual(GithubWebhookRegistration.objects.count(), 0)

    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_x")
    def test_201_create_race_integrity_error_returns_existing(self, _token, mock_post):
        # Genuine race: at short-circuit time NO row exists (so we proceed
        # to POST), but by create() time a concurrent save has inserted the
        # row. We model that by making the *first* .create() call insert the
        # winning row and then raise IntegrityError; the code's `except
        # IntegrityError` branch must re-query and return the winner.
        from django.db import IntegrityError

        resp = MagicMock(spec=requests.Response)
        resp.status_code = 201
        resp.ok = True
        resp.json.return_value = {"id": 4242}
        mock_post.return_value = resp

        real_create = GithubWebhookRegistration.objects.create
        state = {"winner_pk": None}

        def racing_create(*args, **kwargs):
            # Simulate the concurrent winner having just committed this row,
            # then our own insert losing the unique-constraint race.
            winner = real_create(*args, **kwargs)
            state["winner_pk"] = winner.pk
            raise IntegrityError("duplicate key value violates unique constraint")

        with patch.object(
            GithubWebhookRegistration.objects, "create", side_effect=racing_create
        ):
            result = ensure_repo_webhook(self.user, "acme", "rocket")

        # POST was issued (no short-circuit) and the fallback re-query found
        # the row the "concurrent" save created.
        mock_post.assert_called_once()
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, state["winner_pk"])
        self.assertEqual(result.hook_id, 4242)
        self.assertEqual(GithubWebhookRegistration.objects.count(), 1)

    @patch("origin.services.github_webhooks.requests.patch")
    @patch("origin.services.github_webhooks.get_valid_access_token")
    def test_sync_falls_back_to_registered_by_when_current_user_unconnected(
        self, mock_token, mock_patch
    ):
        # An existing registration owned by self.user. A *different* user
        # (user2, no GitHub ConnectedAccount) triggers the short-circuit;
        # the sync helper must fall back to the registration's
        # `registered_by` account to obtain a token.
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="rocket", hook_id=999, registered_by=self.user
        )
        mock_token.return_value = "ghp_fallback"
        ok = MagicMock(spec=requests.Response)
        ok.ok = True
        ok.status_code = 200
        mock_patch.return_value = ok

        result = ensure_repo_webhook(self.user2, "acme", "rocket")
        self.assertIsNotNone(result)
        # PATCH was issued using the fallback account's token.
        mock_patch.assert_called_once()
        # The token was fetched from self.user's (registered_by) account.
        self.assertEqual(mock_token.call_args.args[0], self.account)

    @patch("origin.services.github_webhooks.requests.patch")
    def test_sync_skips_when_no_token_anywhere(self, mock_patch):
        # Registration row whose registered_by user has no ConnectedAccount,
        # and the triggering user (user2) also has none → no PATCH.
        no_acct_user = self.user2  # user2 has no github ConnectedAccount
        reg = GithubWebhookRegistration.objects.create(
            owner="solo", repo="repo", hook_id=321, registered_by=no_acct_user
        )
        result = ensure_repo_webhook(self.user2, "solo", "repo")
        self.assertEqual(result.pk, reg.pk)
        mock_patch.assert_not_called()
