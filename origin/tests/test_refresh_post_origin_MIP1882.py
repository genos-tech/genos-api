"""MIP-1882: the refresh cookie is Lax, refresh works by POST, and a foreign Origin is refused.

Production had `SameSite=None` on the refresh cookie although the app and the API are same-site, so
any website could sign a member out (cross-site POST to signout) or rotate the token (refresh was a
GET). What these pin:

  1. The cookie is `Lax` with DEBUG off and on the response.
  2. POST with the cookie rotates like GET; the body is ignored (the inherited POST read the token
     from the body and skipped the password check); GET still works for one release.
  3. An `Origin` that is not in CORS_ALLOWED_ORIGINS (or the literal "null") gets 403 on refresh
     and signout before the cookie is read: no rotation, no blacklist, no Set-Cookie. No Origin
     header is allowed (same-origin navigations send none).
"""

import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from origin.tests.test_base import BaseAPITestCase

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-refresh-post-origin",
    }
}
GOOD = "https://app.example.test"
BAD = "https://evil.example"


@override_settings(CACHES=_LOCMEM_CACHE, CORS_ALLOWED_ORIGINS=[GOOD, "http://localhost:3000"])
class RefreshPostOriginTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.user.is_active = True
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_active", "is_email_verified"])
        self.raw = str(RefreshToken.for_user(self.user))

    def _call(self, method, name="token_refresh", raw="default", **extra):
        raw = self.raw if raw == "default" else raw
        if raw:
            self.client.cookies["refresh"] = raw
        return getattr(self.client, method)(reverse(name), **extra)

    def test_post_with_the_cookie_rotates(self):
        resp = self._call("post")
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertTrue(resp.data["access"])
        self.assertNotIn("refresh", resp.data)
        self.assertNotEqual(resp.cookies["refresh"].value, self.raw)
        self.assertEqual(BlacklistedToken.objects.count(), 1)

    def test_get_is_not_allowed_and_rotates_nothing(self):
        resp = self._call("get")
        self.assertEqual(resp.status_code, 405, resp.content[:200])
        self.assertNotIn("refresh", resp.cookies)
        self.assertEqual(BlacklistedToken.objects.count(), 0)
        self.assertEqual(self._call("post").status_code, 200, "the token was never spent")

    def test_post_ignores_a_token_in_the_body(self):
        resp = self._call("post", raw="", data={"refresh": self.raw}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(BlacklistedToken.objects.count(), 0)

    def test_post_with_an_old_password_cookie_is_401(self):
        self.user.set_password("a-brand-new-password-9")
        self.user.save(update_fields=["password"])
        resp = self._call("post")
        self.assertEqual(resp.status_code, 401)

    def test_a_foreign_origin_is_refused_without_touching_the_cookie(self):
        resp = self._call("post", HTTP_ORIGIN=BAD)
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("refresh", resp.cookies)
        self.assertEqual(BlacklistedToken.objects.count(), 0)
        self.assertEqual(self._call("post").status_code, 200, "the token was never spent")

    def test_a_cross_site_request_is_refused_even_without_an_origin(self):
        resp = self._call("post", HTTP_SEC_FETCH_SITE="cross-site")
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("refresh", resp.cookies)
        self.assertEqual(BlacklistedToken.objects.count(), 0)
        self.assertEqual(self._call("post", HTTP_SEC_FETCH_SITE="same-site").status_code, 200)

    def test_the_literal_null_origin_is_refused(self):
        self.assertEqual(self._call("post", HTTP_ORIGIN="null").status_code, 403)
        self.assertEqual(BlacklistedToken.objects.count(), 0)

    def test_an_allowed_origin_and_no_origin_pass(self):
        self.assertEqual(self._call("post", HTTP_ORIGIN=GOOD + "/").status_code, 200)
        raw = self.client.cookies["refresh"].value
        self.assertEqual(
            self._call("post", raw=raw, HTTP_ORIGIN="http://localhost:3000").status_code, 200
        )
        raw = self.client.cookies["refresh"].value
        self.assertEqual(self._call("post", raw=raw).status_code, 200)

    def test_signout_with_a_foreign_origin_is_refused_and_blacklists_nothing(self):
        resp = self._call("post", name="signout", HTTP_ORIGIN=BAD)
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn("refresh", resp.cookies)
        self.assertEqual(BlacklistedToken.objects.count(), 0)
        self.assertEqual(self._call("post").status_code, 200, "the session is intact")

    def test_signout_with_the_app_origin_still_works(self):
        resp = self._call("post", name="signout", HTTP_ORIGIN=GOOD)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(BlacklistedToken.objects.count(), 1)

    def test_the_cookie_is_lax(self):
        resp = self._call("post")
        self.assertEqual(resp.cookies["refresh"]["samesite"], "Lax")


class RefreshCookieSameSiteSettingTests(SimpleTestCase):
    def test_production_settings_use_lax(self):
        code = (
            "import os; os.environ['DJANGO_SETTINGS_MODULE']='apis.settings'; "
            "import apis.settings as s; print(s.DEBUG, s.AUTH_COOKIE_SAMESITE, s.AUTH_COOKIE_SECURE)"
        )
        env = {**os.environ, "DJANGO_DEBUG": "false"}
        done = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertIn("False Lax True", done.stdout, done.stderr[-600:])

    def test_the_loaded_setting_is_lax(self):
        self.assertEqual(settings.AUTH_COOKIE_SAMESITE, "Lax")
