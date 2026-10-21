"""Fail-closed production config (MIP-719).

A deploy that loses an environment variable must come up locked down, or
not come up at all. Three seams, each pinned here:

  * DEBUG defaults to OFF. Checked in a fresh interpreter, because the test
    process's own settings are already imported (and CI sets no
    DJANGO_DEBUG, which is the point).
  * The web server refuses to boot on a browser-facing config it cannot
    serve safely (`validate_web_config`, called from wsgi.py/asgi.py — not
    settings.py, because the crons share these settings and set none of
    it). The every-process secret checks live in test_security_hardening.
  * DRF's default permission is IsAuthenticated, and the set of routes that
    are public anyway is pinned, so making one public is a reviewed diff
    here rather than a forgotten `permission_classes`.

Plus, from MIP-621, every process refuses internal hops that could be read
off the wire (`_validate_transport`): Postgres below sslmode=require, plain
redis:// or http:// to a host that isn't private, and an api → collab push
signed with the user-token key.
"""

import base64
import os
import secrets
import subprocess
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver, path
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.test import APIClient

from apis.settings import (
    _is_private_host,
    _parse_field_encryption_keys,
    _validate_transport,
    validate_web_config,
)

# Through `origin.views` first: importing rest_framework.views cold resolves
# DEFAULT_AUTHENTICATION_CLASSES, whose ApiKeyAuthentication imports
# origin.views mid-way and trips a circular import.
from origin.views.common.base_auth_api_view import APIView

_REPO = Path(__file__).resolve().parents[2]

# A valid Fernet key that is NOT settings_test's, so a pass here can't be
# the test settings leaking in.
_KEY = "Xl0rQ9nZb3pW1yV7kT5mR8sJ2hF4dC6aE0gB9uN3iLo="
_OTHER_KEY = "pQ3wE5rT7yU9iO1aS3dF5gH7jK9lZ1xC3vB5nM7qW9e="
# Blind index keys (MIP-619), likewise not settings_test's.
_INDEX_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
_OTHER_INDEX_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()

# The production shape of SEARCH_ENGINE (MIP-721): TLS and a login. The CA is
# left to the system store here; test_opensearch_client covers a pinned CA.
_SEARCH = {
    "OPENSEARCH_HOST": "opensearch.railway.internal",
    "OPENSEARCH_PORT": 9200,
    "OPENSEARCH_USE_SSL": True,
    "OPENSEARCH_USERNAME": "genos_app",
    "OPENSEARCH_PASSWORD": "test-only-not-a-real-password",
    "OPENSEARCH_CA_CERT": "",
}


def _web(**overrides):
    kwargs = {
        "debug": False,
        "allowed_hosts": ["api.genosai.dev", "healthcheck.railway.app"],
        "cors_allowed_origins": ["https://app.genosai.dev"],
        "field_encryption_keys": [_KEY],
        "blind_index_keys": [_INDEX_KEY],
        "search_engine": _SEARCH,
    }
    kwargs.update(overrides)
    validate_web_config(**kwargs)


def _fresh_python(code, **env):
    """Run `code` in a new interpreter with ONLY the given env (+ PATH).

    The ambient environment is dropped on purpose: the docker stack sets
    DJANGO_DEBUG=true, which would hide exactly the default under test.
    """
    base = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_REPO)}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


# Enough for apis.settings to import with DEBUG off. Values are throwaway.
_PROD_SECRETS = {
    "DJANGO_SECRET_KEY": "test-only-not-a-real-secret",
    "JWT_SECRET_KEY": "test-only-not-a-real-jwt-key",
    "DATABASE_URL": "postgres://u:p@127.0.0.1:1/none",
}


class TestDebugDefault(SimpleTestCase):
    def test_unset_means_off(self):
        proc = _fresh_python(
            "import apis.settings as s; print(s.DEBUG, s.CORS_ALLOW_ALL_ORIGINS,"
            " s.AUTH_COOKIE_SECURE, s.SESSION_COOKIE_SECURE)",
            **_PROD_SECRETS,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["False", "False", "True", "True"])

    def test_unset_with_no_secrets_refuses_to_boot(self):
        # The old default (true) let this import succeed and sign JWTs with
        # the public fallback key in settings.py.
        proc = _fresh_python("import apis.settings")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ImproperlyConfigured", proc.stderr)

    def test_only_the_exact_opt_in_turns_it_on(self):
        for value, expected in (("true", "True"), ("TRUE", "True"), ("1", "False"), ("", "False")):
            with self.subTest(value=value):
                proc = _fresh_python(
                    "import apis.settings as s; print(s.DEBUG)",
                    DJANGO_DEBUG=value,
                    **_PROD_SECRETS,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), expected)


class TestValidateWebConfig(SimpleTestCase):
    def test_a_real_config_boots(self):
        _web()

    def test_debug_skips_every_check(self):
        _web(
            debug=True,
            allowed_hosts=[],
            cors_allowed_origins=[],
            field_encryption_keys=[],
            blind_index_keys=[],
            search_engine={
                **_SEARCH,
                "OPENSEARCH_USE_SSL": False,
                "OPENSEARCH_USERNAME": "",
                "OPENSEARCH_PASSWORD": "",
            },
        )

    def test_refuses_unset_or_localhost_hosts(self):
        # settings.py always appends Railway's healthcheck host, so "only that
        # host" is what an unset ALLOWED_HOSTS actually looks like.
        for hosts in (
            [],
            ["healthcheck.railway.app"],
            ["localhost", "healthcheck.railway.app"],
        ):
            with (
                self.subTest(hosts=hosts),
                self.assertRaisesMessage(ImproperlyConfigured, "ALLOWED_HOSTS is unset"),
            ):
                _web(allowed_hosts=hosts)

    def test_refuses_a_wildcard_host(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "ALLOWED_HOSTS contains '*'"):
            _web(allowed_hosts=["api.genosai.dev", "*", "healthcheck.railway.app"])

    def test_refuses_unset_or_localhost_cors(self):
        for origins in (
            [],
            ["http://localhost:3000", "http://localhost:8890"],
            ["http://localhost:3000"],
        ):
            with (
                self.subTest(origins=origins),
                self.assertRaisesMessage(ImproperlyConfigured, "CORS_ALLOWED_ORIGINS is unset"),
            ):
                _web(cors_allowed_origins=origins)

    def test_localhost_next_to_a_real_origin_is_allowed(self):
        _web(cors_allowed_origins=["https://app.genosai.dev", "http://localhost:3000"])

    def test_refuses_a_wildcard_origin(self):
        for origins in (["*"], ["https://app.genosai.dev", "https://*.genosai.dev"]):
            with (
                self.subTest(origins=origins),
                self.assertRaisesMessage(
                    ImproperlyConfigured, "CORS_ALLOWED_ORIGINS contains a wildcard"
                ),
            ):
                _web(cors_allowed_origins=origins)

    def test_refuses_a_missing_encryption_key(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "FIELD_ENCRYPTION_KEYS is unset"):
            _web(field_encryption_keys=[])

    def test_refuses_a_malformed_encryption_key(self):
        for key in ("not-a-key", _KEY[:-4], "ü" * 44):
            with (
                self.subTest(key=key),
                self.assertRaisesMessage(ImproperlyConfigured, "not a valid Fernet key"),
            ):
                _web(field_encryption_keys=[key])

    def test_checks_every_key_in_the_ring(self):
        # The old keys only decrypt, but one that is malformed still fails
        # the first read of a row it encrypted, so it must fail the deploy.
        with self.assertRaisesMessage(ImproperlyConfigured, "key #2 is not a valid Fernet key"):
            _web(field_encryption_keys=[_KEY, "not-a-key"])

    def test_accepts_a_rotation_ring(self):
        _web(field_encryption_keys=[_OTHER_KEY, _KEY])

    def test_refuses_a_key_listed_twice(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "lists the same key twice"):
            _web(field_encryption_keys=[_KEY, _KEY])

    def test_keyring_parsing(self):
        # Newest first, as written; the old name only when the new is unset.
        self.assertEqual(
            _parse_field_encryption_keys(f" {_OTHER_KEY} , {_KEY},", ""), [_OTHER_KEY, _KEY]
        )
        self.assertEqual(_parse_field_encryption_keys("", _KEY), [_KEY])
        self.assertEqual(_parse_field_encryption_keys("  ", _KEY), [_KEY])
        self.assertEqual(_parse_field_encryption_keys(_OTHER_KEY, _KEY), [_OTHER_KEY])
        self.assertEqual(_parse_field_encryption_keys("", ""), [])

    def test_refuses_a_missing_blind_index_key(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "BLIND_INDEX_KEYS is unset"):
            _web(blind_index_keys=[])

    def test_refuses_a_malformed_blind_index_key(self):
        # Too short, not base64, and 33 bytes: each would fail the first
        # sign-in instead of the deploy.
        too_long = base64.urlsafe_b64encode(b"x" * 33).decode()
        for key in (_INDEX_KEY[:-4], "not base64!", too_long):
            with (
                self.subTest(key=key),
                self.assertRaisesMessage(
                    ImproperlyConfigured, "BLIND_INDEX_KEYS key #1 is not 32 url-safe"
                ),
            ):
                _web(blind_index_keys=[key])

    def test_checks_every_blind_index_key(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "BLIND_INDEX_KEYS key #2 is not"):
            _web(blind_index_keys=[_INDEX_KEY, "not-a-key"])
        _web(blind_index_keys=[_OTHER_INDEX_KEY, _INDEX_KEY])

    def test_refuses_a_blind_index_key_listed_twice(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured, "BLIND_INDEX_KEYS lists the same key twice"
        ):
            _web(blind_index_keys=[_INDEX_KEY, _INDEX_KEY])

    def test_refuses_a_blind_index_key_shared_with_encryption(self):
        # A Fernet key is also 32 url-safe base64 bytes, so only this
        # check stops one key from doing both jobs.
        with self.assertRaisesMessage(ImproperlyConfigured, "reuses a FIELD_ENCRYPTION_KEYS key"):
            _web(blind_index_keys=[_OTHER_INDEX_KEY, _KEY])

    def test_refuses_opensearch_without_tls_or_a_login(self):
        # The web server's boot CMD runs `opensearch_setup || echo`, so this
        # is the check that stops a deploy, not the client's first use.
        for unset, needed in (
            ("OPENSEARCH_USE_SSL", "OPENSEARCH_USE_SSL=true"),
            ("OPENSEARCH_USERNAME", "OPENSEARCH_USERNAME"),
            ("OPENSEARCH_PASSWORD", "OPENSEARCH_PASSWORD"),
        ):
            with (
                self.subTest(unset=unset),
                self.assertRaisesMessage(ImproperlyConfigured, needed),
            ):
                _web(search_engine={**_SEARCH, unset: False if unset.endswith("SSL") else ""})


class TestWebEntrypointsRunTheCheck(SimpleTestCase):
    """gunicorn imports apis.wsgi, so the check must run on that import."""

    def _boot(self, module, **env):
        return _fresh_python(f"import {module}", **_PROD_SECRETS, **env)

    def test_wsgi_and_asgi_refuse_a_config_missing_web_settings(self):
        for module in ("apis.wsgi", "apis.asgi"):
            with self.subTest(module=module):
                proc = self._boot(module)
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("ImproperlyConfigured: ALLOWED_HOSTS is unset", proc.stderr)

    def test_wsgi_and_asgi_boot_on_a_complete_config(self):
        # The old single-key name too: a deploy that predates the keyring
        # must keep booting until somebody renames the variable.
        for module in ("apis.wsgi", "apis.asgi"):
            for name, value in (
                ("FIELD_ENCRYPTION_KEYS", f"{_OTHER_KEY},{_KEY}"),
                ("OAUTH_TOKEN_ENCRYPTION_KEY", _KEY),
            ):
                with self.subTest(module=module, name=name):
                    proc = self._boot(
                        module,
                        ALLOWED_HOSTS="api.genosai.dev",
                        CORS_ALLOWED_ORIGINS="https://app.genosai.dev",
                        OPENSEARCH_USE_SSL="true",
                        OPENSEARCH_USERNAME="genos_app",
                        OPENSEARCH_PASSWORD="test-only-not-a-real-password",
                        BLIND_INDEX_KEYS=f"{_OTHER_INDEX_KEY},{_INDEX_KEY}",
                        **{name: value},
                    )
                    self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_wsgi_and_asgi_refuse_a_missing_keyring(self):
        for module in ("apis.wsgi", "apis.asgi"):
            with self.subTest(module=module):
                proc = self._boot(
                    module,
                    ALLOWED_HOSTS="api.genosai.dev",
                    CORS_ALLOWED_ORIGINS="https://app.genosai.dev",
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("ImproperlyConfigured: FIELD_ENCRYPTION_KEYS is unset", proc.stderr)

    def test_wsgi_and_asgi_refuse_a_missing_blind_index_key(self):
        for module in ("apis.wsgi", "apis.asgi"):
            with self.subTest(module=module):
                proc = self._boot(
                    module,
                    ALLOWED_HOSTS="api.genosai.dev",
                    CORS_ALLOWED_ORIGINS="https://app.genosai.dev",
                    FIELD_ENCRYPTION_KEYS=_KEY,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("ImproperlyConfigured: BLIND_INDEX_KEYS is unset", proc.stderr)

    def test_wsgi_and_asgi_refuse_plaintext_opensearch(self):
        for module in ("apis.wsgi", "apis.asgi"):
            with self.subTest(module=module):
                proc = self._boot(
                    module,
                    ALLOWED_HOSTS="api.genosai.dev",
                    CORS_ALLOWED_ORIGINS="https://app.genosai.dev",
                    FIELD_ENCRYPTION_KEYS=_KEY,
                    BLIND_INDEX_KEYS=_INDEX_KEY,
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("ImproperlyConfigured: OpenSearch needs", proc.stderr)

    def test_manage_py_does_not_run_it(self):
        # The crons: same settings, no browser-facing variables.
        proc = _fresh_python(
            "import django; django.setup()",
            DJANGO_SETTINGS_MODULE="apis.settings",
            ALLOWED_HOSTS="*",
            **_PROD_SECRETS,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


def _transport(**overrides):
    kwargs = {
        "debug": False,
        "database": {"HOST": "postgres.railway.internal", "OPTIONS": {"sslmode": "require"}},
        "redis_url": "redis://default:pw@redis.railway.internal:6379",
        "collab_internal_url": "http://backend-collab.railway.internal:8080",
        "collab_internal_secret": "test-only-collab-secret",
        "jwt_signing_key": "test-only-jwt-key",
    }
    kwargs.update(overrides)
    _validate_transport(**kwargs)


class TestValidateTransport(SimpleTestCase):
    def test_the_railway_shape_boots(self):
        _transport()

    def test_debug_skips_every_check(self):
        _transport(
            debug=True,
            database={"HOST": "db.example.com", "OPTIONS": {"sslmode": "disable"}},
            redis_url="redis://redis.example.com:6379",
            collab_internal_url="http://collab.example.com",
            collab_internal_secret="",
        )

    def test_refuses_a_database_mode_that_can_be_plaintext(self):
        # No sslmode at all is libpq's `prefer`, which falls back silently.
        for options in ({"sslmode": "disable"}, {"sslmode": "allow"}, {"sslmode": "prefer"}, {}):
            with (
                self.subTest(options=options),
                self.assertRaisesMessage(
                    ImproperlyConfigured, "database connection can be plaintext"
                ),
            ):
                _transport(database={"HOST": "postgres.railway.internal", "OPTIONS": options})

    def test_accepts_require_or_stricter(self):
        for mode in ("require", "verify-ca", "verify-full"):
            with self.subTest(mode=mode):
                _transport(database={"HOST": "db.example.com", "OPTIONS": {"sslmode": mode}})

    def test_loopback_database_is_exempt(self):
        # CI's Postgres service. A Unix socket has no host.
        for host in ("localhost", "127.0.0.1", "::1", "", "/var/run/postgresql"):
            with self.subTest(host=host):
                _transport(database={"HOST": host, "OPTIONS": {}})

    def test_refuses_plaintext_redis_off_the_private_network(self):
        # Railway's REDIS_PUBLIC_URL is a TCP proxy on the internet.
        for url in (
            "redis://default:pw@monorail.proxy.rlwy.net:12345",
            "redis://8.8.8.8:6379/1",
        ):
            with (
                self.subTest(url=url),
                self.assertRaisesMessage(ImproperlyConfigured, "REDIS_URL is plaintext"),
            ):
                _transport(redis_url=url)

    def test_accepts_tls_or_private_redis(self):
        for url in (
            "rediss://default:pw@monorail.proxy.rlwy.net:12345",
            "redis://redis:6379/1",  # docker-compose, and the settings default
            "redis://127.0.0.1:6379",
            "redis://[fd12::10]:6379",
        ):
            with self.subTest(url=url):
                _transport(redis_url=url)

    def test_refuses_plaintext_collab_off_the_private_network(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "COLLAB_INTERNAL_URL is plaintext"):
            _transport(collab_internal_url="http://collab.genosai.dev")

    def test_accepts_https_collab(self):
        _transport(collab_internal_url="https://collab.genosai.dev")

    def test_refuses_a_non_http_collab_url(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "must be an http(s) URL"):
            _transport(collab_internal_url="collab.genosai.dev")

    def test_refuses_a_collab_push_without_its_own_secret(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "COLLAB_INTERNAL_SECRET is not"):
            _transport(collab_internal_secret="")
        with self.assertRaisesMessage(ImproperlyConfigured, "equals JWT_SECRET_KEY"):
            _transport(collab_internal_secret="same", jwt_signing_key="same")

    def test_no_collab_url_needs_no_secret(self):
        # The push is off, so nothing signs with it.
        _transport(collab_internal_url="", collab_internal_secret="")

    def test_private_host_classification(self):
        # No host at all is the client's localhost default (redis-py).
        for host in (
            "redis",
            "backend-collab.railway.internal",
            "10.0.0.5",
            "fd12::1",
            "::1",
            None,
        ):
            with self.subTest(host=host):
                self.assertTrue(_is_private_host(host))
        for host in (
            "collab.genosai.dev",
            "railway.internal.example.com",
            "8.8.8.8",
            "2606:4700::1",
        ):
            with self.subTest(host=host):
                self.assertFalse(_is_private_host(host))


class TestTransportAtBoot(SimpleTestCase):
    """The DATABASES default and the check, as `import apis.settings` runs
    them with DEBUG off."""

    _REMOTE = "postgres://u:p@postgres.railway.internal:5432/origin"

    def _sslmode(self, **env):
        return _fresh_python(
            "import apis.settings as s;"
            " print(s.DATABASES['default'].get('OPTIONS', {}).get('sslmode'))",
            **{**_PROD_SECRETS, **env},
        )

    def test_remote_database_defaults_to_require(self):
        proc = self._sslmode(DATABASE_URL=self._REMOTE)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "require")

    def test_a_stricter_mode_in_the_url_is_kept(self):
        proc = self._sslmode(DATABASE_URL=f"{self._REMOTE}?sslmode=verify-full")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "verify-full")

    def test_a_weaker_mode_in_the_url_refuses_to_boot(self):
        proc = self._sslmode(DATABASE_URL=f"{self._REMOTE}?sslmode=disable")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ImproperlyConfigured: DATABASE_URL has sslmode=disable", proc.stderr)

    def test_loopback_database_is_left_alone(self):
        proc = self._sslmode()  # _PROD_SECRETS' 127.0.0.1
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "None")

    def test_dev_leaves_the_database_alone(self):
        proc = self._sslmode(DATABASE_URL=self._REMOTE, DJANGO_DEBUG="true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "None")

    def test_public_redis_refuses_to_boot(self):
        proc = self._sslmode(REDIS_URL="redis://default:pw@monorail.proxy.rlwy.net:12345")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ImproperlyConfigured: REDIS_URL is plaintext", proc.stderr)

    def test_collab_push_without_its_secret_refuses_to_boot(self):
        proc = self._sslmode(COLLAB_INTERNAL_URL="http://backend-collab.railway.internal:8080")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("COLLAB_INTERNAL_SECRET is not", proc.stderr)

    def test_private_domain_is_an_allowed_host(self):
        proc = _fresh_python(
            "import apis.settings as s; print(s.ALLOWED_HOSTS)",
            RAILWAY_PRIVATE_DOMAIN="backend-django.railway.internal",
            ALLOWED_HOSTS="api.genosai.dev",
            **_PROD_SECRETS,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            str(["api.genosai.dev", "healthcheck.railway.app", "backend-django.railway.internal"]),
        )

    def test_production_hsts_covers_subdomains(self):
        proc = _fresh_python(
            "import apis.settings as s;"
            " print(s.SECURE_HSTS_SECONDS, s.SECURE_HSTS_INCLUDE_SUBDOMAINS, s.SECURE_HSTS_PRELOAD)",
            **_PROD_SECRETS,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), ["31536000", "True", "False"])


class _Forgot(APIView):
    """A view that names no permission_classes."""

    def get(self, request):
        return Response({"ok": True})


urlpatterns = [path("forgot/", _Forgot.as_view())]


class TestDefaultPermission(SimpleTestCase):
    def test_the_default_is_authenticated(self):
        self.assertEqual(list(api_settings.DEFAULT_PERMISSION_CLASSES), [IsAuthenticated])

    def test_a_view_that_names_nothing_refuses_anonymous_callers(self):
        with self.settings(ROOT_URLCONF=__name__):
            res = APIClient().get("/forgot/")
        self.assertEqual(res.status_code, 401)


def _walk(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p.callback


def _view_class(callback):
    cls = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
    return cls if isinstance(cls, type) and issubclass(cls, APIView) else None


# Every route an anonymous caller may reach. Adding one is a security
# decision: say why in the PR that edits this set.
_PUBLIC_DRF = {
    "api/v2/health/",
    # MIP-772: the uptime probe's check on the alert cron. Its own bearer
    # token, not a user's; 404 without it.
    "api/v2/health/alerting/",
    "api/v2/user/signup/",
    "api/v2/user/signin/",
    "api/v2/user/signin/refresh/",
    "api/v2/user/demo/",
    "api/v2/user/signout/",
    "api/v2/user/password-reset/request/",
    "api/v2/user/password-reset/confirm/",
    "api/v2/user/verify-email/",
    "api/v2/user/verify-email/resend/",
    "api/v2/oauth/<str:provider_name>/initiate/",
    "api/v2/oauth/<str:provider_name>/callback/",
    "api/v2/github/webhook/",
    "api/v2/billing/plans/",
    "api/v2/billing/stripe/webhook/",
    "api/v2/team/invite/preview/",
    "api/public/v1/openapi.json",
}

# Plain Django views never see DRF permissions at all, so each one does its
# own check (anymail verifies the provider's webhook signature; the
# unsubscribe link carries a signed token; serve_media authenticates
# attachment prefixes itself). Pinned by module, because anymail registers
# one route per provider.
_PLAIN_DJANGO_MODULES = {
    "anymail.webhooks.",
    "origin.views.common.email_unsubscribe_views",
    "origin.views.common.media_views",
}


class TestPublicRoutesArePinned(SimpleTestCase):
    def setUp(self):
        self.routes = list(_walk(get_resolver().url_patterns))

    def test_the_public_drf_routes_are_exactly_the_known_set(self):
        public = set()
        for route, callback in self.routes:
            cls = _view_class(callback)
            if cls is None:
                continue
            perms = list(cls.permission_classes)
            if not perms or AllowAny in perms:
                public.add(route)
        self.assertEqual(public, _PUBLIC_DRF)

    def test_no_other_plain_django_views(self):
        stray = sorted(
            f"{route} -> {callback.__module__}"
            for route, callback in self.routes
            if _view_class(callback) is None
            and not any(callback.__module__.startswith(m) for m in _PLAIN_DJANGO_MODULES)
        )
        self.assertEqual(stray, [])
