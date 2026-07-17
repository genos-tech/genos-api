"""Security-hardening tests: auth throttling, security headers, and the
production secret-fallback guard.

Throttle tests override the cache to a per-class locmem store (the real
default cache is Redis — shared across runs, so throttle history would
leak between tests) and patch each throttle's `rate` down so the tests
stay fast.
"""

from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from apis.settings import _validate_prod_secrets
from origin.tests.test_base import BaseAPITestCase
from origin.views.common.auth_views import (
    PasswordResetRequestThrottle,
    SignInBurstThrottle,
    UserViewSet,
)

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-security-hardening",
    }
}


class TestProdSecretGuard(BaseAPITestCase):
    def test_debug_mode_accepts_fallbacks(self):
        _validate_prod_secrets(
            debug=True,
            secret_key="django-insecure-whatever",
            jwt_signing_key="your_secret_key",
        )

    def test_prod_rejects_django_fallback(self):
        with self.assertRaises(ImproperlyConfigured):
            _validate_prod_secrets(
                debug=False,
                secret_key="django-insecure-whatever",
                jwt_signing_key="real-jwt-key",
            )

    def test_prod_rejects_jwt_fallback(self):
        with self.assertRaises(ImproperlyConfigured):
            _validate_prod_secrets(
                debug=False,
                secret_key="a-real-50-char-secret",
                jwt_signing_key="your_secret_key",
            )

    def test_prod_rejects_empty_keys(self):
        with self.assertRaises(ImproperlyConfigured):
            _validate_prod_secrets(debug=False, secret_key="", jwt_signing_key="k")
        with self.assertRaises(ImproperlyConfigured):
            _validate_prod_secrets(debug=False, secret_key="k", jwt_signing_key="")

    def test_prod_accepts_real_keys(self):
        _validate_prod_secrets(
            debug=False,
            secret_key="a-real-50-char-secret",
            jwt_signing_key="a-real-jwt-key",
        )


@override_settings(CACHES=_LOCMEM_CACHE)
class TestAuthThrottling(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache

        cache.clear()

    def test_signin_burst_throttle(self):
        body = {"email": "nobody@example.com", "password": "wrong-password"}
        with mock.patch.object(SignInBurstThrottle, "rate", "2/min"):
            r1 = self.client.post("/api/v2/user/signin/", body)
            r2 = self.client.post("/api/v2/user/signin/", body)
            r3 = self.client.post("/api/v2/user/signin/", body)
        self.assertEqual(r1.status_code, 401)
        self.assertEqual(r2.status_code, 401)
        self.assertEqual(r3.status_code, 429)

    def test_password_reset_request_throttle(self):
        body = {"email": "nobody@example.com"}
        with mock.patch.object(PasswordResetRequestThrottle, "rate", "2/hour"):
            r1 = self.client.post("/api/v2/user/password-reset/request/", body)
            r2 = self.client.post("/api/v2/user/password-reset/request/", body)
            r3 = self.client.post("/api/v2/user/password-reset/request/", body)
        # Enumeration-safe 200s for the allowed attempts, then 429.
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r3.status_code, 429)

    def test_throttles_attached_to_auth_views(self):
        # Cheap wiring check for the endpoints not exercised above.
        from origin.views.common.auth_views import (
            CustomTokenObtainPairView,
            PasswordResetConfirmView,
            PasswordResetRequestView,
            ResendVerificationView,
        )

        for view in (
            CustomTokenObtainPairView,
            PasswordResetRequestView,
            PasswordResetConfirmView,
            ResendVerificationView,
            UserViewSet,
        ):
            self.assertTrue(view.throttle_classes, f"{view.__name__} has no throttles")


class TestSignupViewSurface(BaseAPITestCase):
    def test_user_viewset_is_create_only(self):
        # Narrowed from ModelViewSet: an accidental router registration
        # must not expose AllowAny list/retrieve/update/delete over the
        # user table.
        for action in ("list", "retrieve", "update", "partial_update", "destroy"):
            self.assertFalse(hasattr(UserViewSet, action), f"UserViewSet exposes {action}")
        self.assertTrue(hasattr(UserViewSet, "create"))


class TestSecurityHeaders(BaseAPITestCase):
    def test_nosniff_and_frame_deny_on_every_response(self):
        resp = self.client.get("/api/v2/health/")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "same-origin")

    @override_settings(SECURE_HSTS_SECONDS=31536000)
    def test_hsts_emitted_on_secure_requests(self):
        resp = self.client.get("/api/v2/health/", secure=True)
        self.assertEqual(
            resp.headers.get("Strict-Transport-Security"), "max-age=31536000"
        )
