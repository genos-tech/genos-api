"""MIP-1790: disconnecting Google or GitHub, and deleting the account, revoke the grant at the provider.

Until now `ConnectedAccount.delete()` only forgot our encrypted copy of the token: the grant stayed
alive at Google and GitHub (the GitHub one with the `repo` scope). `origin.services.oauth.revoke`
asks the provider to kill it, best effort, behind `OAUTH_REVOKE_ON_DISCONNECT` (default OFF).

Every provider call here is mocked: no test reaches Google or GitHub (the network guard would fail
it). The background thread is replaced by an inline runner so the calls are observable.
"""

import os
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common.user_models import ConnectedAccount
from origin.services import crypto
from origin.services.account_deletion import delete_account
from origin.services.oauth import revoke

User = get_user_model()

FLAG = "OAUTH_REVOKE_ON_DISCONNECT"
POST = "origin.services.oauth.revoke.requests.post"
DELETE = "origin.services.oauth.revoke.requests.delete"
SPAWN = "origin.services.oauth.revoke._spawn"
LOGGER = "origin.services.oauth.revoke"

REFRESH = "refresh-token-SECRET-1"
ACCESS = "access-token-SECRET-2"
GH_TOKEN = "gho_github-token-SECRET-3"
CLIENT_ID = "gh-client-id-123"
CLIENT_SECRET = "gh-client-secret-SECRET-4"
SECRETS = (REFRESH, ACCESS, GH_TOKEN, CLIENT_SECRET)

GH_SETTINGS = dict(
    GITHUB_OAUTH_CLIENT_ID=CLIENT_ID,
    GITHUB_OAUTH_CLIENT_SECRET=CLIENT_SECRET,
)


def _resp(status_code: int) -> mock.MagicMock:
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.ok = status_code < 400
    resp.text = "provider body that must never be logged"
    return resp


def _inline(fn) -> None:
    fn()


class _Base(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="revoker", email="revoker@example.com", password="pw123456"
        )
        self.client.force_authenticate(user=self.user)
        self.enterContext(mock.patch(SPAWN, _inline))

    def _env(self, value):
        return mock.patch.dict(os.environ, {FLAG: value})

    def _off_by_default(self):
        self.enterContext(mock.patch.dict(os.environ))
        os.environ.pop(FLAG, None)

    def _google(self, sub="g-sub", refresh=REFRESH, access=ACCESS, **kw):
        return ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id=sub,
            provider_email=f"{sub}@example.com",
            access_token_encrypted=crypto.encrypt(access),
            refresh_token_encrypted=crypto.encrypt(refresh) if refresh else None,
            **kw,
        )

    def _github(self, token=GH_TOKEN):
        return ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="gh-sub",
            access_token_encrypted=crypto.encrypt(token),
        )

    def _disconnect(self, account):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.delete(f"/api/v2/integrations/account/{account.id}/")

    def _assert_no_secret_in(self, logs):
        text = "\n".join(logs.output)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertNotIn("g-sub@example.com", text)
        self.assertNotIn("provider body that must never be logged", text)


class TheSwitch(TestCase):
    def test_off_unless_the_variable_says_otherwise(self):
        for value in ("", "0", "false", "no", "off", "  ", "banana"):
            with mock.patch.dict(os.environ, {FLAG: value}):
                self.assertFalse(revoke.revoke_enabled(), value)
        with mock.patch.dict(os.environ):
            os.environ.pop(FLAG, None)
            self.assertFalse(revoke.revoke_enabled())

    def test_on_for_the_usual_true_words(self):
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with mock.patch.dict(os.environ, {FLAG: value}):
                self.assertTrue(revoke.revoke_enabled(), value)

    def test_not_in_the_settings_module(self):
        """`apis/settings.py` is a retrieval path: a change there starts the billed agent-evals."""
        from django.conf import settings

        self.assertFalse(hasattr(settings, FLAG))


@override_settings(**GH_SETTINGS)
class SwitchOffChangesNothing(_Base):
    def setUp(self):
        super().setUp()
        self._off_by_default()

    def test_disconnect_calls_no_provider_and_reads_no_token(self):
        account = self._google()
        with (
            mock.patch(POST) as post,
            mock.patch(DELETE) as delete,
            mock.patch("origin.services.oauth.revoke.crypto") as crypto_mock,
        ):
            resp = self._disconnect(account)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ConnectedAccount.objects.filter(id=account.id).exists())
        post.assert_not_called()
        delete.assert_not_called()
        crypto_mock.decrypt.assert_not_called()

    def test_capture_does_not_even_run_the_query(self):
        self._google()
        with self.assertNumQueries(0):
            self.assertEqual(revoke.capture(ConnectedAccount.objects.filter(user=self.user)), [])

    def test_account_deletion_calls_no_provider(self):
        self._google()
        self._github()
        with mock.patch(POST) as post, mock.patch(DELETE) as delete:
            with self.captureOnCommitCallbacks(execute=True):
                delete_account(self.user)
        self.assertFalse(ConnectedAccount.objects.filter(user=self.user).exists())
        post.assert_not_called()
        delete.assert_not_called()


@override_settings(**GH_SETTINGS)
class GoogleDisconnect(_Base):
    def setUp(self):
        super().setUp()
        self.enterContext(self._env("1"))

    def test_revokes_the_refresh_token_once_with_the_right_request(self):
        account = self._google()
        with mock.patch(POST, return_value=_resp(200)) as post, mock.patch(DELETE) as delete:
            resp = self._disconnect(account)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ConnectedAccount.objects.filter(id=account.id).exists())
        delete.assert_not_called()
        post.assert_called_once()
        args, kwargs = post.call_args
        self.assertEqual(args, ("https://oauth2.googleapis.com/revoke",))
        self.assertEqual(kwargs["data"], {"token": REFRESH})
        self.assertEqual(kwargs["timeout"], 3)
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertNotIn("auth", kwargs)

    def test_falls_back_to_the_access_token_without_a_refresh_token(self):
        account = self._google(refresh=None)
        with mock.patch(POST, return_value=_resp(200)) as post:
            self._disconnect(account)
        self.assertEqual(post.call_args.kwargs["data"], {"token": ACCESS})

    def test_nothing_is_sent_before_the_delete_commits(self):
        account = self._google()
        with mock.patch(POST, return_value=_resp(200)) as post:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                resp = self.client.delete(f"/api/v2/integrations/account/{account.id}/")
            self.assertEqual(resp.status_code, 204)
            post.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            callbacks[0]()
        post.assert_called_once()

    def test_the_login_identity_is_refused_and_not_revoked(self):
        self.user.primary_auth_provider = "google"
        self.user.save(update_fields=["primary_auth_provider"])
        account = self._google(is_login_identity=True)
        with mock.patch(POST) as post:
            resp = self._disconnect(account)
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(ConnectedAccount.objects.filter(id=account.id).exists())
        post.assert_not_called()

    def test_by_provider_route_also_revokes(self):
        self._google()
        with mock.patch(POST, return_value=_resp(200)) as post:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.delete("/api/v2/integrations/google/")
        self.assertEqual(resp.status_code, 204)
        post.assert_called_once()

    def test_a_failure_never_reaches_the_user_and_leaks_nothing(self):
        cases = {
            "500": dict(return_value=_resp(500)),
            "503": dict(return_value=_resp(503)),
            "403": dict(return_value=_resp(403)),
            "timeout": dict(side_effect=requests.Timeout(f"slow {REFRESH}")),
            "connection": dict(side_effect=requests.ConnectionError(f"down {CLIENT_SECRET}")),
            "bug": dict(side_effect=RuntimeError(f"boom {ACCESS}")),
        }
        for name, behaviour in cases.items():
            with self.subTest(name):
                account = self._google(sub=f"g-{name}")
                with (
                    mock.patch(POST, **behaviour) as post,
                    self.assertLogs(LOGGER, level="INFO") as logs,
                ):
                    resp = self._disconnect(account)
                self.assertEqual(resp.status_code, 204)
                self.assertFalse(ConnectedAccount.objects.filter(id=account.id).exists())
                post.assert_called_once()
                self.assertIn("provider=google", "\n".join(logs.output))
                self.assertNotIn("g-sub@example.com", "\n".join(logs.output))
                for secret in SECRETS:
                    self.assertNotIn(secret, "\n".join(logs.output))

    def test_a_token_already_invalid_counts_as_success(self):
        for code in (400, 404):
            with self.subTest(code):
                account = self._google(sub=f"g-{code}")
                with (
                    mock.patch(POST, return_value=_resp(code)),
                    self.assertLogs(LOGGER, level="INFO") as logs,
                ):
                    resp = self._disconnect(account)
                self.assertEqual(resp.status_code, 204)
                text = "\n".join(logs.output)
                self.assertIn("already_invalid", text)
                self.assertNotIn("failed", text)
                self.assertNotEqual({r.levelname for r in logs.records}, {"WARNING"})

    def test_an_unreadable_stored_token_is_skipped_quietly(self):
        account = ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="g-bad",
            access_token_encrypted="placeholder",
        )
        with mock.patch(POST) as post:
            resp = self._disconnect(account)
        self.assertEqual(resp.status_code, 204)
        post.assert_not_called()


@override_settings(**GH_SETTINGS)
class GitHubDisconnect(_Base):
    def setUp(self):
        super().setUp()
        self.enterContext(self._env("true"))

    def test_deletes_the_grant_with_the_app_credentials(self):
        account = self._github()
        with mock.patch(DELETE, return_value=_resp(204)) as delete, mock.patch(POST) as post:
            resp = self._disconnect(account)
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ConnectedAccount.objects.filter(id=account.id).exists())
        post.assert_not_called()
        delete.assert_called_once()
        args, kwargs = delete.call_args
        self.assertEqual(args, (f"https://api.github.com/applications/{CLIENT_ID}/grant",))
        self.assertEqual(kwargs["auth"], (CLIENT_ID, CLIENT_SECRET))
        self.assertEqual(kwargs["json"], {"access_token": GH_TOKEN})
        self.assertEqual(kwargs["timeout"], 3)
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["headers"]["Accept"], "application/vnd.github+json")
        self.assertNotIn(GH_TOKEN, str(kwargs["headers"]))

    def test_failures_never_reach_the_user_and_leak_nothing(self):
        cases = {
            "500": dict(return_value=_resp(500)),
            "422": dict(return_value=_resp(422)),
            "401": dict(return_value=_resp(401)),
            "timeout": dict(side_effect=requests.Timeout(f"slow {GH_TOKEN}")),
            "connection": dict(side_effect=requests.ConnectionError(f"down {CLIENT_SECRET}")),
        }
        for name, behaviour in cases.items():
            with self.subTest(name):
                ConnectedAccount.objects.filter(user=self.user).delete()
                account = self._github()
                with (
                    mock.patch(DELETE, **behaviour) as delete,
                    self.assertLogs(LOGGER, level="INFO") as logs,
                ):
                    resp = self._disconnect(account)
                self.assertEqual(resp.status_code, 204)
                self.assertFalse(ConnectedAccount.objects.filter(id=account.id).exists())
                delete.assert_called_once()
                self.assertIn("provider=github", "\n".join(logs.output))
                for secret in SECRETS:
                    self.assertNotIn(secret, "\n".join(logs.output))

    def test_a_grant_already_gone_counts_as_success(self):
        for code in (400, 404):
            with self.subTest(code):
                ConnectedAccount.objects.filter(user=self.user).delete()
                account = self._github()
                with (
                    mock.patch(DELETE, return_value=_resp(code)),
                    self.assertLogs(LOGGER, level="INFO") as logs,
                ):
                    resp = self._disconnect(account)
                self.assertEqual(resp.status_code, 204)
                self.assertIn("already_invalid", "\n".join(logs.output))

    @override_settings(GITHUB_OAUTH_CLIENT_ID="", GITHUB_OAUTH_CLIENT_SECRET="")
    def test_without_the_app_credentials_nothing_is_called(self):
        account = self._github()
        with mock.patch(DELETE) as delete, self.assertLogs(LOGGER, level="INFO") as logs:
            resp = self._disconnect(account)
        self.assertEqual(resp.status_code, 204)
        delete.assert_not_called()
        self.assertIn("not_configured", "\n".join(logs.output))

    def test_the_client_id_cannot_change_the_path(self):
        account = self._github()
        with (
            override_settings(GITHUB_OAUTH_CLIENT_ID="a/../b?x=1"),
            mock.patch(DELETE, return_value=_resp(204)) as delete,
        ):
            self._disconnect(account)
        url = delete.call_args.args[0]
        self.assertEqual(url, "https://api.github.com/applications/a%2F..%2Fb%3Fx%3D1/grant")


@override_settings(**GH_SETTINGS)
class AccountDeletion(_Base):
    def setUp(self):
        super().setUp()
        self.enterContext(self._env("1"))

    def test_every_connected_account_is_revoked_after_the_erasure_commits(self):
        self._google(sub="g-one")
        self._google(sub="g-two", refresh="refresh-two-SECRET-5")
        self._github()
        with (
            mock.patch(POST, return_value=_resp(200)) as post,
            mock.patch(DELETE, return_value=_resp(204)) as delete,
        ):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                delete_account(self.user)
            post.assert_not_called()
            delete.assert_not_called()
            for callback in callbacks:
                callback()
        self.assertFalse(ConnectedAccount.objects.filter(user=self.user).exists())
        self.assertEqual(post.call_count, 2)
        self.assertEqual(
            sorted(c.kwargs["data"]["token"] for c in post.call_args_list),
            sorted([REFRESH, "refresh-two-SECRET-5"]),
        )
        delete.assert_called_once()

    def test_provider_failures_do_not_stop_the_erasure(self):
        self._google()
        self._github()
        with (
            mock.patch(POST, side_effect=requests.Timeout("slow")),
            mock.patch(DELETE, return_value=_resp(500)),
            self.assertLogs(LOGGER, level="INFO") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            summary = delete_account(self.user)
        self.assertEqual(summary["user_id"], str(self.user.id))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_deleted)
        self.assertFalse(ConnectedAccount.objects.filter(user=self.user).exists())
        for secret in SECRETS:
            self.assertNotIn(secret, "\n".join(logs.output))

    def test_a_rolled_back_erasure_revokes_nothing(self):
        """The account is still there, so its Google/GitHub connection must keep working."""
        self._google()
        self._github()

        class Boom(Exception):
            pass

        with mock.patch(POST) as post, mock.patch(DELETE) as delete:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(Boom):
                    with transaction.atomic():
                        delete_account(self.user)
                        raise Boom()
        self.assertEqual(callbacks, [])
        post.assert_not_called()
        delete.assert_not_called()
        self.assertEqual(ConnectedAccount.objects.filter(user=self.user).count(), 2)

    def test_a_user_with_no_connection_schedules_nothing(self):
        with mock.patch(POST) as post, mock.patch(DELETE) as delete:
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                delete_account(self.user)
        # The search-index scrub is also an on-commit callback (MIP-1893); no OAuth one is.
        self.assertEqual([c for c in callbacks if "delete_for_user" not in c.__qualname__], [])
        post.assert_not_called()
        delete.assert_not_called()


class RunsInTheBackground(TestCase):
    """The user's request must not wait for Google or GitHub."""

    def test_the_default_runner_is_a_daemon_thread(self):
        ran = []
        with mock.patch("origin.services.oauth.revoke.threading.Thread") as thread:
            revoke._spawn(lambda: ran.append(1))
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs["daemon"], True)
        thread.return_value.start.assert_called_once_with()
        self.assertEqual(ran, [])

    def test_the_thread_body_never_raises(self):
        grants = [revoke.Grant(provider="google", access_token="a", refresh_token="r")]
        with mock.patch(POST, side_effect=RuntimeError("boom")):
            revoke._revoke_all(grants)  # must not raise
