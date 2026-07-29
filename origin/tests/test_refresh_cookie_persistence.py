"""The refresh cookie must outlive the app being closed.

The defect: `_set_refresh_cookie` set no `max_age`/`expires`, which makes a
SESSION cookie — the browser discards it the moment the browser or the
installed PWA is fully quit. The refresh token inside was still valid for
days, so the server was perfectly willing to continue the session; the
credential just wasn't there to present. The user experienced "quitting the
app logs me out", which is not a policy anyone chose.

What these pin:

  1. Every path that issues the cookie sets a real lifetime, so closing the
     app no longer ends the session. Sign-in is the obvious one, but refresh
     matters just as much: it re-issues the cookie on rotation, and a
     session-cookie there would quietly re-introduce the bug for anyone who
     stayed signed in.
  2. That lifetime tracks REFRESH_TOKEN_LIFETIME rather than a hardcoded
     number, so the cookie can't outlive the credential it carries (a cookie
     that survives its token = a guaranteed forced sign-out later) or die
     before it (the original bug).
  3. Sign-out still clears it. Persisting the cookie is only safe if the
     explicit exit still works.
"""

from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from origin.tests.test_base import BaseAPITestCase

REFRESH_MAX_AGE = int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds())

# The sign-in throttles key off the default cache, which is Redis here and
# therefore shared across runs — without an isolated cache, repeated runs
# eventually answer 429 instead of exercising the cookie.
_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-refresh-cookie",
    }
}


@override_settings(CACHES=_LOCMEM_CACHE)
class RefreshCookiePersistenceTests(BaseAPITestCase):
    PASSWORD = "correct-horse-battery-staple-7"

    def setUp(self):
        super().setUp()
        self.user.set_password(self.PASSWORD)
        self.user.is_active = True
        # Sign-in refuses unverified email accounts before it mints
        # anything, so without this the endpoint 403s and never reaches
        # the cookie code these tests are about.
        self.user.is_email_verified = True
        self.user.save(update_fields=["password", "is_active", "is_email_verified"])

    def _signin(self):
        return self.client.post(
            reverse("token_obtain_pair"),
            {"email": self.user.email, "password": self.PASSWORD},
            format="json",
        )

    def test_signin_cookie_survives_closing_the_app(self):
        resp = self._signin()
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        cookie = resp.cookies["refresh"]
        # A session cookie reports an empty max-age; that empty string IS
        # the bug, so assert the positive value rather than truthiness.
        self.assertEqual(cookie["max-age"], REFRESH_MAX_AGE)
        self.assertTrue(cookie["httponly"])

    def test_cookie_lifetime_matches_the_token_lifetime(self):
        # Drift either way is a bug: a cookie outliving its token means a
        # surprise sign-out, and one dying first is what we just fixed.
        resp = self._signin()
        self.assertEqual(
            resp.cookies["refresh"]["max-age"],
            int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()),
        )

    def test_refresh_reissues_a_persistent_cookie(self):
        # Rotation re-sets the cookie. If that path issued a session cookie,
        # the fix would silently lapse for anyone who stayed signed in.
        self._signin()
        # GET, not POST: the view reads the token from the cookie rather
        # than the body (`CookieTokenRefreshView.get`).
        resp = self.client.get(reverse("token_refresh"))
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(resp.cookies["refresh"]["max-age"], REFRESH_MAX_AGE)

    def test_signout_still_clears_the_cookie(self):
        self._signin()
        resp = self.client.post(reverse("signout"), {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        # Deletion is expressed as an immediate expiry with an empty value.
        self.assertEqual(resp.cookies["refresh"].value, "")
