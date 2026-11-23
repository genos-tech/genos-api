"""MIP-2242: a rotated refresh token that comes back after its grace window revokes the family.

Before, the server only refused the replay. A thief who had refreshed first kept a chain that
renewed forever. Now the replay blacklists every refresh token of that login family (in the
database, so a cache flush cannot undo it) and writes a `session.refresh_reused` security event.
"""

from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.tests.test_base import BaseAPITestCase

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-refresh-reuse",
    }
}


@override_settings(CACHES=_LOCMEM_CACHE)
class RefreshReuseRevokesFamilyTests(BaseAPITestCase):
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

    def _expire_grace(self):
        # The 30 s window is a cache entry: dropping them is "30 seconds later".
        cache.clear()

    def _reuse_events(self):
        return SecurityEvent.objects.filter(action=SecurityAction.REFRESH_REUSED)

    def test_replay_after_the_window_revokes_the_chain_the_thief_holds(self):
        # The thief refreshed first and holds T1; the owner's later refresh with T0 is the reuse.
        r1 = self.refresh(self.t0)
        self.assertEqual(r1.status_code, 200)
        thief_t1 = r1.cookies["refresh"].value
        self._expire_grace()

        self.assertEqual(self.refresh(self.t0).status_code, 403)

        self.assertEqual(self.refresh(thief_t1).status_code, 403)
        self.assertEqual(self._reuse_events().count(), 1)
        self.assertEqual(self._reuse_events().first().metadata.get("tokens_revoked"), 1)

    def test_revocation_is_in_the_database_not_only_the_cache(self):
        r1 = self.refresh(self.t0)
        t1 = r1.cookies["refresh"].value
        self._expire_grace()
        self.refresh(self.t0)
        cache.clear()  # a flushed cache forgets `_revoke_family`; the blacklist row remains
        self.assertEqual(self.refresh(t1).status_code, 403)
        jti = RefreshToken(t1, verify=False)["jti"]
        self.assertTrue(BlacklistedToken.objects.filter(token__jti=jti).exists())

    def test_replay_inside_the_window_is_not_a_reuse(self):
        r1 = self.refresh(self.t0)
        t1 = r1.cookies["refresh"].value
        self.assertEqual(self.refresh(self.t0).status_code, 200)  # a second tab, graced
        self.assertEqual(self.refresh(t1).status_code, 200)
        self.assertEqual(self._reuse_events().count(), 0)

    def test_another_family_of_the_same_user_is_untouched(self):
        other = str(RefreshToken.for_user(self.user))  # a second device
        r1 = self.refresh(self.t0)
        self.assertEqual(r1.status_code, 200)
        self._expire_grace()
        self.assertEqual(self.refresh(self.t0).status_code, 403)
        self.assertEqual(self.refresh(other).status_code, 200)

    def test_a_cache_that_is_down_does_not_revoke_anyone(self):
        r1 = self.refresh(self.t0)
        t1 = r1.cookies["refresh"].value
        with mock.patch.object(cache, "get", side_effect=RuntimeError("cache down")):
            self.assertEqual(self.refresh(self.t0).status_code, 403)
        self.assertEqual(self._reuse_events().count(), 0)
        self.assertEqual(self.refresh(t1).status_code, 200)
