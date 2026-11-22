"""GitHub App client (MIP-2086): boot validation, the App JWT, installation tokens and their cache,
and the installation models. Every GitHub call is mocked; nothing reaches the network.

The properties pinned here are the security rules of the design (MIP-2057, section 8):
  * off by default, and the secrets are required at boot ONLY when it is on;
  * the JWT is RS256, short-lived and carries the App's client id;
  * an installation token is read-only and narrowed, minted only for a known active installation,
    cached ENCRYPTED, never in the database, and a different repo/permission set never shares one.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import jwt
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from apis.settings import _validate_github_app
from origin.models.common.github_app_models import (
    GithubAppInstallation,
    GithubAppInstallationTeam,
)
from origin.models.common.team_models import TeamMaster
from origin.services import github_app
from origin.services.github_app import (
    GithubAppDisabled,
    GithubAppError,
    InstallationUnavailable,
    PermissionNotAllowed,
    forget_installation_token,
    get_installation_token,
)

User = get_user_model()


def _pem(private_key) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


_RSA = rsa.generate_private_key(public_exponent=65537, key_size=2048)
RSA_PEM = _pem(_RSA)
RSA_PUBLIC_PEM = (
    _RSA.public_key()
    .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    .decode()
)
EC_PEM = _pem(ec.generate_private_key(ec.SECP256R1()))

APP_SETTINGS = dict(
    GITHUB_APP_ENABLED=True,
    GITHUB_APP_ID="12345",
    GITHUB_APP_CLIENT_ID="Iv1.appclientid",
    GITHUB_APP_CLIENT_SECRET="app-client-secret",
    GITHUB_APP_PRIVATE_KEY=RSA_PEM,
    GITHUB_APP_WEBHOOK_SECRET="whsec",
    GITHUB_APP_SLUG="genos-test",
)

_ALL = dict(
    enabled=True,
    app_id="1",
    client_id="Iv1.x",
    client_secret="s",
    private_key=RSA_PEM,
    webhook_secret="w",
    slug="genos",
)


class TestBootValidation(SimpleTestCase):
    """`GITHUB_APP_ENABLED` off needs nothing; on, every secret is required and the key must parse."""

    def test_off_requires_nothing(self):
        _validate_github_app(
            enabled=False,
            app_id="",
            client_id="",
            client_secret="",
            private_key="",
            webhook_secret="",
            slug="",
        )

    def test_on_with_everything_passes(self):
        _validate_github_app(**_ALL)

    def test_on_names_each_missing_value(self):
        env_names = {
            "app_id": "GITHUB_APP_ID",
            "client_id": "GITHUB_APP_CLIENT_ID",
            "client_secret": "GITHUB_APP_CLIENT_SECRET",
            "private_key": "GITHUB_APP_PRIVATE_KEY",
            "webhook_secret": "GITHUB_APP_WEBHOOK_SECRET",
            "slug": "GITHUB_APP_SLUG",
        }
        for name, env_name in env_names.items():
            with self.subTest(missing=name):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    _validate_github_app(**{**_ALL, name: ""})
                self.assertIn(env_name, str(ctx.exception))

    def test_on_refuses_a_key_that_is_not_pem(self):
        with self.assertRaises(ImproperlyConfigured):
            _validate_github_app(**{**_ALL, "private_key": "not a key"})

    def test_on_refuses_a_non_rsa_key(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _validate_github_app(**{**_ALL, "private_key": EC_PEM})
        self.assertIn("RSA", str(ctx.exception))

    def test_error_never_contains_a_secret_value(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            _validate_github_app(**{**_ALL, "private_key": "", "client_secret": "TOPSECRET"})
        self.assertNotIn("TOPSECRET", str(ctx.exception))

    def test_default_is_off(self):
        from django.conf import settings

        self.assertFalse(settings.GITHUB_APP_ENABLED)


@override_settings(**APP_SETTINGS)
class TestAppJwt(SimpleTestCase):
    def test_rs256_claims(self):
        token = github_app.app_jwt(now=1_000_000)
        self.assertEqual(jwt.get_unverified_header(token)["alg"], "RS256")
        claims = jwt.decode(
            token,
            RSA_PUBLIC_PEM,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        self.assertEqual(claims["iss"], "Iv1.appclientid")
        self.assertEqual(claims["iat"], 1_000_000 - 60)
        # GitHub's maximum is 10 minutes after `exp` minus `iat`; stay under it.
        self.assertLessEqual(claims["exp"] - claims["iat"], 600)

    def test_signed_with_the_configured_key_only(self):
        other = _pem(rsa.generate_private_key(public_exponent=65537, key_size=2048))
        with override_settings(GITHUB_APP_PRIVATE_KEY=other):
            token = github_app.app_jwt(now=1_000_000)
        with self.assertRaises(jwt.InvalidSignatureError):
            jwt.decode(token, RSA_PUBLIC_PEM, algorithms=["RS256"], options={"verify_exp": False})

    @override_settings(GITHUB_APP_ENABLED=False)
    def test_disabled_raises_before_touching_the_key(self):
        with self.assertRaises(GithubAppDisabled):
            github_app.app_jwt()


def _resp(status=201, body=None):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    return resp


def _token_body(token="ghs_installation-token-SECRET", minutes=60):
    expires = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return {"token": token, "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}


@override_settings(**APP_SETTINGS)
class TestInstallationToken(TestCase):
    def setUp(self):
        cache.clear()
        self.installation = GithubAppInstallation.objects.create(
            installation_id=777, account_login="acme", account_type="Organization"
        )

    def _mint(self, post, **kwargs):
        return get_installation_token(777, **kwargs)

    @patch("origin.services.github_app.requests.post")
    def test_mints_narrowed_read_only_token_with_app_jwt(self, post):
        post.return_value = _resp(201, _token_body())
        token = get_installation_token(777, repositories=["rocket"])
        self.assertEqual(token, "ghs_installation-token-SECRET")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "https://api.github.com/app/installations/777/access_tokens")
        self.assertEqual(kwargs["json"]["repositories"], ["rocket"])
        self.assertEqual(
            kwargs["json"]["permissions"],
            {
                "metadata": "read",
                "pull_requests": "read",
                "checks": "read",
                "statuses": "read",
                "issues": "read",
            },
        )
        self.assertNotIn("contents", kwargs["json"]["permissions"])
        auth = kwargs["headers"]["Authorization"]
        self.assertTrue(auth.startswith("Bearer "))
        # The bearer is the App JWT (verifies against the App's public key), not a user token.
        claims = jwt.decode(
            auth.removeprefix("Bearer "),
            RSA_PUBLIC_PEM,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        self.assertEqual(claims["iss"], "Iv1.appclientid")
        self.assertFalse(kwargs["allow_redirects"])

    @patch("origin.services.github_app.requests.post")
    def test_second_call_is_served_from_the_cache(self, post):
        post.return_value = _resp(201, _token_body())
        first = get_installation_token(777, repositories=["rocket"])
        second = get_installation_token(777, repositories=["rocket"])
        self.assertEqual(first, second)
        self.assertEqual(post.call_count, 1)

    @patch("origin.services.github_app.requests.post")
    def test_cache_holds_ciphertext_never_the_token(self, post):
        post.return_value = _resp(201, _token_body())
        get_installation_token(777, repositories=["rocket"])
        key = github_app._cache_key(777, ("rocket",), dict(github_app.ALLOWED_PERMISSIONS))
        sealed = cache.get(key)
        self.assertIsNotNone(sealed)
        self.assertNotIn("ghs_installation-token-SECRET", sealed)
        self.assertNotIn("SECRET", sealed)

    @patch("origin.services.github_app.requests.post")
    def test_token_is_not_stored_in_the_database(self, post):
        post.return_value = _resp(201, _token_body())
        get_installation_token(777, repositories=["rocket"])
        names = {f.name for f in GithubAppInstallation._meta.get_fields()}
        self.assertFalse({n for n in names if "token" in n})
        row = GithubAppInstallation.objects.get(installation_id=777)
        self.assertNotIn("ghs_installation", repr(row.__dict__))

    @patch("origin.services.github_app.requests.post")
    def test_a_different_repo_or_permission_set_never_shares_a_token(self, post):
        post.side_effect = [
            _resp(201, _token_body("ghs_one")),
            _resp(201, _token_body("ghs_two")),
            _resp(201, _token_body("ghs_three")),
        ]
        a = get_installation_token(777, repositories=["rocket"])
        b = get_installation_token(777, repositories=["other"])
        c = get_installation_token(777, repositories=["rocket"], permissions={"metadata": "read"})
        self.assertEqual((a, b, c), ("ghs_one", "ghs_two", "ghs_three"))
        self.assertEqual(post.call_count, 3)

    @patch("origin.services.github_app.requests.post")
    def test_repository_order_does_not_change_the_key(self, post):
        post.return_value = _resp(201, _token_body())
        get_installation_token(777, repositories=["a", "b"])
        get_installation_token(777, repositories=["b", "a"])
        self.assertEqual(post.call_count, 1)

    @patch("origin.services.github_app.requests.post")
    def test_a_token_about_to_expire_is_not_cached(self, post):
        # Fewer than CACHE_SAFETY_SECONDS left: usable once, never cached.
        post.return_value = _resp(201, _token_body(minutes=2))
        get_installation_token(777, repositories=["rocket"])
        get_installation_token(777, repositories=["rocket"])
        self.assertEqual(post.call_count, 2)

    @patch("origin.services.github_app.requests.post")
    def test_cache_ttl_is_shorter_than_the_token_life(self, post):
        post.return_value = _resp(201, _token_body(minutes=60))
        with patch("origin.services.github_app.cache") as fake_cache:
            fake_cache.get.return_value = None
            get_installation_token(777, repositories=["rocket"])
        ttl = fake_cache.set.call_args.kwargs["timeout"]
        self.assertLessEqual(ttl, 60 * 60 - github_app.CACHE_SAFETY_SECONDS)
        self.assertGreater(ttl, 60 * 60 - github_app.CACHE_SAFETY_SECONDS - 30)

    @patch("origin.services.github_app.requests.post")
    def test_an_unreadable_cache_entry_is_a_miss(self, post):
        post.return_value = _resp(201, _token_body())
        key = github_app._cache_key(777, ("rocket",), dict(github_app.ALLOWED_PERMISSIONS))
        cache.set(key, "not-a-fernet-token")
        self.assertEqual(
            get_installation_token(777, repositories=["rocket"]), "ghs_installation-token-SECRET"
        )

    @patch("origin.services.github_app.requests.post")
    def test_forget_drops_the_cached_token(self, post):
        post.return_value = _resp(201, _token_body())
        get_installation_token(777, repositories=["rocket"])
        forget_installation_token(777, repositories=["rocket"])
        get_installation_token(777, repositories=["rocket"])
        self.assertEqual(post.call_count, 2)

    @patch("origin.services.github_app.requests.post")
    def test_write_or_unknown_permissions_are_refused_before_any_call(self, post):
        for bad in (
            {"contents": "read"},
            {"pull_requests": "write"},
            {"administration": "read"},
            {"metadata": "read", "issues": "admin"},
            {},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(PermissionNotAllowed):
                    get_installation_token(777, permissions=bad)
        post.assert_not_called()

    @patch("origin.services.github_app.requests.post")
    def test_unknown_installation_mints_nothing(self, post):
        with self.assertRaises(InstallationUnavailable):
            get_installation_token(999_999)
        post.assert_not_called()

    @patch("origin.services.github_app.requests.post")
    def test_suspended_installation_mints_nothing_even_with_a_cached_token(self, post):
        post.return_value = _resp(201, _token_body())
        get_installation_token(777, repositories=["rocket"])
        self.installation.suspended_at = datetime.now(timezone.utc)
        self.installation.save()
        with self.assertRaises(InstallationUnavailable):
            get_installation_token(777, repositories=["rocket"])
        self.assertEqual(post.call_count, 1)

    @patch("origin.services.github_app.requests.post")
    def test_github_404_and_403_mean_unavailable(self, post):
        for status in (404, 403):
            with self.subTest(status=status):
                post.return_value = _resp(status)
                with self.assertRaises(InstallationUnavailable):
                    get_installation_token(777, repositories=["rocket"])

    @patch("origin.services.github_app.requests.post")
    def test_other_failures_raise_without_leaking(self, post):
        post.return_value = _resp(500, {"message": "TOKEN-IN-BODY"})
        with self.assertRaises(GithubAppError) as ctx:
            get_installation_token(777, repositories=["rocket"])
        self.assertNotIn("TOKEN-IN-BODY", str(ctx.exception))

        post.return_value = _resp(201, {"no": "token"})
        with self.assertRaises(GithubAppError):
            get_installation_token(777, repositories=["rocket"])

    @patch("origin.services.github_app.requests.post")
    def test_network_error_names_only_the_class(self, post):
        post.side_effect = requests.ConnectionError("https://x?token=ghs_LEAK")
        with self.assertRaises(GithubAppError) as ctx:
            get_installation_token(777, repositories=["rocket"])
        self.assertNotIn("ghs_LEAK", str(ctx.exception))
        self.assertIn("ConnectionError", str(ctx.exception))

    @override_settings(GITHUB_APP_ENABLED=False)
    @patch("origin.services.github_app.requests.post")
    def test_disabled_is_a_no_op_that_never_calls_github(self, post):
        with self.assertRaises(GithubAppDisabled):
            get_installation_token(777, repositories=["rocket"])
        post.assert_not_called()


class TestInstallationModels(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="gha-owner",
            email="gha-owner@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.team = TeamMaster.objects.create(
            team_name="GHA Team", team_email="gha@team.com", owner=self.user
        )
        self.installation = GithubAppInstallation.objects.create(
            installation_id=5_000_000_001, account_login="acme", account_type="Organization"
        )

    def test_installation_id_is_unique_and_holds_a_large_id(self):
        self.assertEqual(
            GithubAppInstallation.objects.get(installation_id=5_000_000_001).account_login, "acme"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            GithubAppInstallation.objects.create(
                installation_id=5_000_000_001, account_login="dup", account_type="User"
            )

    def test_defaults_are_active_and_selected(self):
        self.assertTrue(self.installation.is_active)
        self.assertEqual(self.installation.repository_selection, "selected")

    def test_team_link_is_unique_per_pair_and_many_to_many(self):
        GithubAppInstallationTeam.objects.create(
            installation=self.installation, team=self.team, linked_by=self.user
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            GithubAppInstallationTeam.objects.create(installation=self.installation, team=self.team)
        other = GithubAppInstallation.objects.create(
            installation_id=2, account_login="me", account_type="User"
        )
        GithubAppInstallationTeam.objects.create(installation=other, team=self.team)
        self.assertEqual(self.team.github_app_installations.count(), 2)

    def test_deleting_an_installation_deletes_its_links_but_not_the_team(self):
        GithubAppInstallationTeam.objects.create(installation=self.installation, team=self.team)
        self.installation.delete()
        self.assertFalse(GithubAppInstallationTeam.objects.exists())
        self.assertTrue(TeamMaster.objects.filter(pk=self.team.pk).exists())

    def test_deleting_the_linking_user_keeps_the_link(self):
        link = GithubAppInstallationTeam.objects.create(
            installation=self.installation, team=self.team, linked_by=self.user
        )
        GithubAppInstallationTeam.objects.filter(pk=link.pk).update(linked_by=None)
        link.refresh_from_db()
        self.assertIsNone(link.linked_by)
