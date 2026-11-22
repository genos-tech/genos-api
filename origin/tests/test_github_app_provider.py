"""The `github_app` OAuth provider and its token refresh (MIP-2086).

GitHub ROTATES the App user token's refresh token: using one kills the old refresh token and the
old access token. So two requests that refresh at the same moment would lose the connection unless
the refresh runs under a row lock and re-reads the row first. The concurrent test below runs two
real threads against Postgres to prove that; the rest is mocked HTTP.
"""

import threading
import time
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from origin.models.common.user_models import ConnectedAccount
from origin.services import crypto
from origin.services.oauth import revoke
from origin.services.oauth.base import InvalidGrantError, TokenResponse
from origin.services.oauth.github_app import GitHubAppOAuthProvider
from origin.services.oauth.registry import get_provider, supported_provider_names
from origin.services.oauth.tokens import ReauthRequired, get_valid_access_token
from origin.tests.transaction_support import CheapFlushTransactionTestCase

User = get_user_model()

APP = dict(GITHUB_APP_CLIENT_ID="Iv1.appclient", GITHUB_APP_CLIENT_SECRET="app-secret-SECRET")
POST = "origin.services.oauth.github_app.requests.post"


def _resp(body, status=200):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestRegistry(SimpleTestCase):
    def test_github_app_is_a_registered_provider(self):
        self.assertIsInstance(get_provider("github_app"), GitHubAppOAuthProvider)

    def test_github_app_flow_routes_are_served_but_never_as_a_login(self):
        # The flow is wired (MIP-2087); the views additionally need GITHUB_APP_ENABLED, and the
        # login (GET initiate) refuses this provider: a login with it would create users. Both
        # are pinned in test_github_app_install_flow.
        self.assertEqual(set(supported_provider_names()), {"google", "github", "github_app"})

    def test_flags(self):
        provider = get_provider("github_app")
        self.assertTrue(provider.supports_refresh)
        self.assertTrue(provider.rotates_refresh_token)
        self.assertFalse(get_provider("google").rotates_refresh_token)
        self.assertFalse(get_provider("github").rotates_refresh_token)


@override_settings(**APP)
class TestProvider(SimpleTestCase):
    def setUp(self):
        self.provider = GitHubAppOAuthProvider()

    def test_authorize_url_uses_the_app_client_and_no_scope(self):
        url = self.provider.authorize_url(state="st", intent="connect", redirect_uri="https://a/cb")
        self.assertTrue(url.startswith("https://github.com/login/oauth/authorize?"))
        self.assertIn("client_id=Iv1.appclient", url)
        self.assertIn("state=st", url)
        self.assertNotIn("scope", url)

    @patch(POST)
    def test_exchange_code(self, post):
        post.return_value = _resp(
            {"access_token": "ghu_a", "refresh_token": "ghr_r", "expires_in": 28800}
        )
        token = self.provider.exchange_code(code="c", redirect_uri="https://a/cb")
        self.assertEqual(
            token,
            TokenResponse(
                access_token="ghu_a",
                refresh_token="ghr_r",
                expires_in_seconds=28800,
                granted_scopes=[],
            ),
        )
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["client_id"], "Iv1.appclient")
        self.assertEqual(data["client_secret"], "app-secret-SECRET")
        self.assertEqual(post.call_args.kwargs["headers"]["Accept"], "application/json")

    @patch(POST)
    def test_refresh_sends_the_refresh_grant(self, post):
        post.return_value = _resp(
            {"access_token": "ghu_new", "refresh_token": "ghr_new", "expires_in": 28800}
        )
        token = self.provider.refresh(refresh_token="ghr_old")
        self.assertEqual((token.access_token, token.refresh_token), ("ghu_new", "ghr_new"))
        data = post.call_args.kwargs["data"]
        self.assertEqual(data["grant_type"], "refresh_token")
        self.assertEqual(data["refresh_token"], "ghr_old")

    @patch(POST)
    def test_dead_refresh_token_is_invalid_grant_even_on_http_200(self, post):
        # GitHub answers 200 with an `error` body: there is no HTTPError to catch.
        post.return_value = _resp({"error": "bad_refresh_token", "error_description": "SECRET"})
        with self.assertRaises(InvalidGrantError) as ctx:
            self.provider.refresh(refresh_token="ghr_old")
        self.assertNotIn("SECRET", str(ctx.exception))

    @patch(POST)
    def test_other_errors_are_not_reauth(self, post):
        post.return_value = _resp({"error": "incorrect_client_credentials"})
        with self.assertRaises(RuntimeError) as ctx:
            self.provider.refresh(refresh_token="ghr_old")
        self.assertNotIsInstance(ctx.exception, InvalidGrantError)
        # A failed code exchange is never InvalidGrantError either.
        post.return_value = _resp({"error": "bad_verification_code"})
        with self.assertRaises(RuntimeError):
            self.provider.exchange_code(code="c", redirect_uri="https://a/cb")

    @patch("origin.services.oauth.github_app.requests.get")
    def test_profile_claims_no_email(self, get):
        get.return_value = _resp({"id": 42, "login": "octo", "name": None, "email": "a@b.c"})
        profile = self.provider.fetch_profile(access_token="ghu_a")
        self.assertEqual(profile.provider_user_id, "42")
        self.assertEqual(profile.display_name, "octo")
        self.assertIsNone(profile.email)
        self.assertFalse(profile.email_verified)


def _account(**over):
    user = User.objects.create_user(
        username=over.pop("username", "gha-user"),
        email=over.pop("email", "gha-user@test.com"),
        password="testpass123",
        is_email_verified=True,
    )
    fields = dict(
        user=user,
        provider="github_app",
        provider_user_id="42",
        access_token_encrypted=crypto.encrypt("ghu_old"),
        refresh_token_encrypted=crypto.encrypt("ghr_old"),
        access_token_expires_at=timezone.now() - timedelta(minutes=1),
    )
    fields.update(over)
    return ConnectedAccount.objects.create(**fields)


def _fresh(access="ghu_new", refresh="ghr_new", expires_in=28800):
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_in_seconds=expires_in,
        granted_scopes=[],
    )


@override_settings(**APP)
class TestRefresh(TestCase):
    def test_valid_token_is_returned_without_a_refresh(self):
        account = _account(access_token_expires_at=timezone.now() + timedelta(hours=4))
        with patch.object(GitHubAppOAuthProvider, "refresh") as refresh:
            self.assertEqual(get_valid_access_token(account), "ghu_old")
        refresh.assert_not_called()

    def test_expired_token_refreshes_and_stores_the_rotated_refresh_token(self):
        account = _account()
        with patch.object(GitHubAppOAuthProvider, "refresh", return_value=_fresh()) as refresh:
            self.assertEqual(get_valid_access_token(account), "ghu_new")
        refresh.assert_called_once_with(refresh_token="ghr_old")
        row = ConnectedAccount.objects.get(pk=account.pk)
        self.assertEqual(crypto.decrypt(row.access_token_encrypted), "ghu_new")
        # GitHub rotated it: the NEW refresh token must be the one stored.
        self.assertEqual(crypto.decrypt(row.refresh_token_encrypted), "ghr_new")
        self.assertGreater(row.access_token_expires_at, timezone.now() + timedelta(hours=7))
        # Plaintext never reaches the columns.
        self.assertNotIn("ghu_new", row.access_token_encrypted)
        self.assertNotIn("ghr_new", row.refresh_token_encrypted)

    def test_stale_instance_finds_the_winners_token_instead_of_spending_a_dead_one(self):
        # `stale` was loaded before another request refreshed; its refresh token is now dead.
        account = _account()
        stale = ConnectedAccount.objects.get(pk=account.pk)
        with patch.object(GitHubAppOAuthProvider, "refresh", return_value=_fresh()):
            get_valid_access_token(account)
        with patch.object(GitHubAppOAuthProvider, "refresh") as refresh:
            self.assertEqual(get_valid_access_token(stale), "ghu_new")
        refresh.assert_not_called()
        # The caller's instance now carries the current tokens, so saving it cannot write stale ones.
        self.assertEqual(crypto.decrypt(stale.refresh_token_encrypted), "ghr_new")

    def test_refresh_runs_inside_a_row_lock(self):
        account = _account()
        seen = {}

        def fake_refresh(*, refresh_token):
            from django.db import connection

            seen["in_atomic"] = connection.in_atomic_block
            return _fresh()

        with patch.object(GitHubAppOAuthProvider, "refresh", side_effect=fake_refresh):
            with patch(
                "origin.services.oauth.tokens.ConnectedAccount.objects.select_for_update",
                wraps=ConnectedAccount.objects.select_for_update,
            ) as locker:
                get_valid_access_token(account)
        locker.assert_called_once()
        self.assertTrue(seen["in_atomic"])

    def test_dead_refresh_token_means_reauth_not_a_crash(self):
        account = _account()
        with patch.object(
            GitHubAppOAuthProvider, "refresh", side_effect=InvalidGrantError("bad_refresh_token")
        ):
            with self.assertRaises(ReauthRequired):
                get_valid_access_token(account)
        # Nothing was overwritten.
        row = ConnectedAccount.objects.get(pk=account.pk)
        self.assertEqual(crypto.decrypt(row.refresh_token_encrypted), "ghr_old")

    def test_missing_refresh_token_means_reauth(self):
        account = _account(refresh_token_encrypted=None)
        with self.assertRaises(ReauthRequired):
            get_valid_access_token(account)

    def test_disconnected_while_in_flight_means_reauth(self):
        account = _account()
        ConnectedAccount.objects.filter(pk=account.pk).delete()
        with patch.object(GitHubAppOAuthProvider, "refresh") as refresh:
            with self.assertRaises(ReauthRequired):
                get_valid_access_token(account)
        refresh.assert_not_called()

    def test_transient_http_error_propagates(self):
        account = _account()
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 502
        with patch.object(
            GitHubAppOAuthProvider, "refresh", side_effect=requests.HTTPError(response=resp)
        ):
            with self.assertRaises(requests.HTTPError):
                get_valid_access_token(account)

    def test_a_response_without_a_new_refresh_token_keeps_the_old_one(self):
        account = _account()
        with patch.object(GitHubAppOAuthProvider, "refresh", return_value=_fresh(refresh=None)):
            get_valid_access_token(account)
        row = ConnectedAccount.objects.get(pk=account.pk)
        self.assertEqual(crypto.decrypt(row.refresh_token_encrypted), "ghr_old")


@override_settings(**APP)
class TestConcurrentRefresh(CheapFlushTransactionTestCase):
    """Two real threads, two connections, one row. Only one may spend the refresh token."""

    def test_two_requests_refresh_once(self):
        account = _account()
        calls = []
        results = {}
        errors = []
        barrier = threading.Barrier(2)

        def fake_refresh(*, refresh_token):
            calls.append(refresh_token)
            # Hold the lock long enough for the other thread to be waiting on it.
            time.sleep(0.3)
            if len(calls) > 1:
                # The old refresh token is dead at GitHub once it was used.
                raise InvalidGrantError("bad_refresh_token")
            return _fresh()

        def worker(name):
            try:
                instance = ConnectedAccount.objects.get(pk=account.pk)  # both load it stale
                barrier.wait(timeout=5)
                results[name] = get_valid_access_token(instance)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                connections.close_all()

        with patch.object(GitHubAppOAuthProvider, "refresh", side_effect=fake_refresh):
            threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(calls, ["ghr_old"])  # the refresh token was spent exactly once
        self.assertEqual(results, {"a": "ghu_new", "b": "ghu_new"})
        row = ConnectedAccount.objects.get(pk=account.pk)
        self.assertEqual(crypto.decrypt(row.refresh_token_encrypted), "ghr_new")


@override_settings(**APP)
class TestNonExpiringToken(TestCase):
    def test_app_without_expiry_has_a_token_that_never_asks_for_reauth(self):
        # "Expire user authorization tokens" off at GitHub: no expiry, no refresh token.
        account = _account(access_token_expires_at=None, refresh_token_encrypted=None)
        with patch.object(GitHubAppOAuthProvider, "refresh") as refresh:
            self.assertEqual(get_valid_access_token(account), "ghu_old")
        refresh.assert_not_called()


@override_settings(**APP)
class TestRevokeCapture(TestCase):
    """The 8-hour access token has usually expired by disconnect time: capture must refresh it, or
    GitHub answers 404 and the 6-month refresh token is reported revoked while still working."""

    def test_expired_access_token_is_refreshed_before_it_is_captured(self):
        account = _account()  # expired a minute ago
        with patch.dict("os.environ", {"OAUTH_REVOKE_ON_DISCONNECT": "true"}):
            with patch.object(GitHubAppOAuthProvider, "refresh", return_value=_fresh()):
                grants = revoke.capture([account])
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0].access_token, "ghu_new")

    def test_valid_access_token_is_captured_as_is_without_a_refresh(self):
        account = _account(access_token_expires_at=timezone.now() + timedelta(hours=4))
        with patch.dict("os.environ", {"OAUTH_REVOKE_ON_DISCONNECT": "true"}):
            with patch.object(GitHubAppOAuthProvider, "refresh") as refresh:
                grants = revoke.capture([account])
        refresh.assert_not_called()
        self.assertEqual(grants[0].access_token, "ghu_old")

    def test_dead_refresh_token_keeps_the_stored_token_and_still_captures(self):
        account = _account()
        with patch.dict("os.environ", {"OAUTH_REVOKE_ON_DISCONNECT": "true"}):
            with patch.object(
                GitHubAppOAuthProvider, "refresh", side_effect=InvalidGrantError("bad")
            ):
                grants = revoke.capture([account])
        self.assertEqual(grants[0].access_token, "ghu_old")


@override_settings(**APP)
class TestRevoke(SimpleTestCase):
    def test_revokes_the_authorization_with_the_apps_own_credentials(self):
        grant = revoke.Grant(provider="github_app", access_token="ghu_tok")
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 204
        with patch("origin.services.oauth.revoke.requests.delete", return_value=resp) as delete:
            outcome, _ = revoke._call(grant)
        self.assertEqual(outcome, "revoked")
        args, kwargs = delete.call_args
        # The grant, not just the token: the refresh token dies with it.
        self.assertEqual(args[0], "https://api.github.com/applications/Iv1.appclient/grant")
        self.assertEqual(kwargs["auth"], ("Iv1.appclient", "app-secret-SECRET"))
        self.assertEqual(kwargs["json"], {"access_token": "ghu_tok"})
        self.assertFalse(kwargs["allow_redirects"])

    def test_unknown_to_github_counts_as_revoked_already(self):
        resp = MagicMock(spec=requests.Response)
        resp.status_code = 404
        with patch("origin.services.oauth.revoke.requests.delete", return_value=resp):
            outcome, _ = revoke._call(revoke.Grant(provider="github_app", access_token="x"))
        self.assertEqual(outcome, "already_invalid")

    @override_settings(GITHUB_APP_CLIENT_ID="", GITHUB_APP_CLIENT_SECRET="")
    def test_not_configured_sends_nothing(self):
        with patch("origin.services.oauth.revoke.requests.delete") as delete:
            outcome, _ = revoke._call(revoke.Grant(provider="github_app", access_token="x"))
        self.assertEqual(outcome, "not_configured")
        delete.assert_not_called()
