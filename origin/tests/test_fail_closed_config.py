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
"""

import os
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

from apis.settings import validate_web_config

# Through `origin.views` first: importing rest_framework.views cold resolves
# DEFAULT_AUTHENTICATION_CLASSES, whose ApiKeyAuthentication imports
# origin.views mid-way and trips a circular import.
from origin.views.common.base_auth_api_view import APIView

_REPO = Path(__file__).resolve().parents[2]

# A valid Fernet key that is NOT settings_test's, so a pass here can't be
# the test settings leaking in.
_KEY = "Xl0rQ9nZb3pW1yV7kT5mR8sJ2hF4dC6aE0gB9uN3iLo="


def _web(**overrides):
    kwargs = {
        "debug": False,
        "allowed_hosts": ["api.genosai.dev", "healthcheck.railway.app"],
        "cors_allowed_origins": ["https://app.genosai.dev"],
        "field_encryption_key": _KEY,
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
        _web(debug=True, allowed_hosts=[], cors_allowed_origins=[], field_encryption_key="")

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
        with self.assertRaisesMessage(ImproperlyConfigured, "OAUTH_TOKEN_ENCRYPTION_KEY is unset"):
            _web(field_encryption_key="")

    def test_refuses_a_malformed_encryption_key(self):
        for key in ("not-a-key", _KEY[:-4], "ü" * 44):
            with (
                self.subTest(key=key),
                self.assertRaisesMessage(ImproperlyConfigured, "not a valid Fernet key"),
            ):
                _web(field_encryption_key=key)


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
        for module in ("apis.wsgi", "apis.asgi"):
            with self.subTest(module=module):
                proc = self._boot(
                    module,
                    ALLOWED_HOSTS="api.genosai.dev",
                    CORS_ALLOWED_ORIGINS="https://app.genosai.dev",
                    OAUTH_TOKEN_ENCRYPTION_KEY=_KEY,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_manage_py_does_not_run_it(self):
        # The crons: same settings, no browser-facing variables.
        proc = _fresh_python(
            "import django; django.setup()",
            DJANGO_SETTINGS_MODULE="apis.settings",
            ALLOWED_HOSTS="*",
            **_PROD_SECRETS,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


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
