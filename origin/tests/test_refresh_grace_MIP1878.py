"""MIP-1878: a refresh token that was just rotated is accepted again for a short grace window.

The defect: rotation blacklists the old refresh token. If the response carrying the new cookie is
lost (client abort, navigation) or a second tab refreshes with the same cookie, the browser still
holds the blacklisted token and the next refresh got 403, which signs the person out. Ken decided
(2026-11-21): the previous token is accepted for `REFRESH_GRACE_SECONDS` = 30 s.

What these pin:

  1. Replaying the previous token inside the window gives 200 with a working access token and a
     new refresh cookie; the successor still works.
  2. At most 3 uses; after the window (key gone), 403; a token blacklisted by signout (never
     rotated) stays 403; signout with a graced token, or with its successor, closes the window.
  3. A password change still wins (401); another user's grace entry, a missing or broken cache
     fail closed (403).
"""

from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from origin.tests.test_base import BaseAPITestCase
from origin.views.common import auth_views

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-refresh-grace",
    }
}


def _jti(raw):
    return RefreshToken(raw, verify=False)["jti"]


@override_settings(CACHES=_LOCMEM_CACHE)
class RefreshGraceTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.user.is_active = True
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_active", "is_email_verified"])
        self.t0 = str(RefreshToken.for_user(self.user))

    def refresh(self, raw):
        self.client.cookies["refresh"] = raw
        return self.client.post(reverse("token_refresh"))

    def rotate(self):
        resp = self.refresh(self.t0)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        return resp.cookies["refresh"].value

    def test_the_window_is_thirty_seconds(self):
        self.assertEqual(auth_views.REFRESH_GRACE_SECONDS, 30)
        with mock.patch.object(auth_views.cache, "set", wraps=auth_views.cache.set) as spy:
            self.rotate()
        self.assertTrue(
            any(c.args[2:] == (30,) or c.kwargs.get("timeout") == 30 for c in spy.call_args_list)
        )

    def test_replaying_the_previous_token_gets_a_working_session(self):
        t1 = self.rotate()
        resp = self.refresh(self.t0)  # the response with t1 was "lost"
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        branch = resp.cookies["refresh"].value
        self.assertNotIn(branch, (self.t0, t1))
        me = self.client.get(
            "/api/v2/user/me/",
            HTTP_AUTHORIZATION=f"Bearer {resp.data['access']}",
        )
        self.assertNotEqual(me.status_code, 401, me.content[:200])

    def test_the_successor_still_works_after_a_replay(self):
        t1 = self.rotate()
        self.assertEqual(self.refresh(self.t0).status_code, 200)
        self.assertEqual(self.refresh(t1).status_code, 200)

    def test_at_most_three_graced_uses(self):
        self.rotate()
        for _ in range(3):
            self.assertEqual(self.refresh(self.t0).status_code, 200)
        self.assertEqual(self.refresh(self.t0).status_code, 403)

    def test_after_the_window_it_is_403(self):
        self.rotate()
        cache.delete(f"refresh-grace:{_jti(self.t0)}")
        self.assertEqual(self.refresh(self.t0).status_code, 403)

    def test_a_token_blacklisted_by_signout_is_never_graced(self):
        self.client.cookies["refresh"] = self.t0
        self.assertEqual(self.client.post(reverse("signout")).status_code, 200)
        self.assertEqual(self.refresh(self.t0).status_code, 403)

    def test_signout_with_the_graced_token_closes_the_window(self):
        self.rotate()
        self.client.cookies["refresh"] = self.t0
        self.client.post(reverse("signout"))
        self.assertEqual(self.refresh(self.t0).status_code, 403)

    def test_signout_with_the_successor_closes_the_predecessors_window(self):
        t1 = self.rotate()
        self.client.cookies["refresh"] = t1
        self.client.post(reverse("signout"))
        self.assertEqual(self.refresh(self.t0).status_code, 403)

    def test_signout_with_a_token_minted_by_a_graced_use_closes_the_original_window(self):
        self.rotate()
        branch = self.refresh(self.t0).cookies["refresh"].value  # minted through the grace window
        self.client.cookies["refresh"] = branch
        self.client.post(reverse("signout"))
        self.assertEqual(self.refresh(self.t0).status_code, 403)

    def test_a_password_change_still_signs_the_session_out(self):
        self.rotate()
        self.user.set_password("a-brand-new-password-9")
        self.user.save(update_fields=["password"])
        self.assertEqual(self.refresh(self.t0).status_code, 401)

    def test_another_users_grace_entry_is_refused(self):
        self.rotate()
        cache.set(f"refresh-grace:{_jti(self.t0)}", "someone-else", 30)
        self.assertEqual(self.refresh(self.t0).status_code, 403)

    def test_an_inactive_user_is_refused(self):
        self.rotate()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertIn(self.refresh(self.t0).status_code, (401, 403))

    # The per-IP throttle (MIP-2232) reads the same cache and has its own outage tests; here it
    # is out of the way so the test is about the grace window alone.
    @mock.patch.object(auth_views.TokenRefreshThrottle, "allow_request", return_value=True)
    def test_an_unavailable_cache_fails_closed_and_normal_refresh_still_works(self, _throttle):
        self.rotate()
        with mock.patch.object(auth_views.cache, "get", return_value=None):
            self.assertEqual(self.refresh(self.t0).status_code, 403)
        with mock.patch.object(auth_views.cache, "get", side_effect=RuntimeError("redis down")):
            self.assertEqual(self.refresh(self.t0).status_code, 403)
        with (
            mock.patch.object(auth_views.cache, "set", side_effect=RuntimeError("redis down")),
            mock.patch.object(auth_views.cache, "add", side_effect=RuntimeError("redis down")),
        ):
            fresh = str(RefreshToken.for_user(self.user))
            self.assertEqual(self.refresh(fresh).status_code, 200)

    def test_a_failed_rotation_leaves_no_window(self):
        bad = "not.a.token"
        self.assertEqual(self.refresh(bad).status_code, 403)
        self.assertEqual(BlacklistedToken.objects.count(), 0)
