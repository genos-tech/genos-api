"""MIP-1907: signing out revokes every refresh token of the session's family.

The defect (found in the re-check of #1035, MIP-1878): a graced replay of a rotated token mints
ANOTHER full 7-day refresh token, and signout only blacklisted the token in the cookie. A sibling
the browser no longer held (T1 when the cookie ended on T2) kept rotating forward for 7 days
after signout.

Every refresh token now carries a family id (`fam`). A token without one (minted before this) uses
its own jti as the family, and the first rotation stamps it, so old sessions keep working.
Signout records the family as revoked and the refresh view refuses it, normal and graced path alike.
"""

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from origin.tests.test_base import BaseAPITestCase

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-refresh-family",
    }
}


def _claims(raw):
    return RefreshToken(raw, verify=False)


@override_settings(CACHES=_LOCMEM_CACHE)
class RefreshFamilyRevokeTests(BaseAPITestCase):
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

    def signout(self, raw):
        self.client.cookies["refresh"] = raw
        return self.client.post(reverse("signout"))

    def _t0_t1_t2(self):
        """Rotate T0 -> T1, then a graced replay of T0 mints T2."""
        r1 = self.refresh(self.t0)
        self.assertEqual(r1.status_code, 200)
        t1 = r1.cookies["refresh"].value
        r2 = self.refresh(self.t0)
        self.assertEqual(r2.status_code, 200)
        t2 = r2.cookies["refresh"].value
        self.assertNotEqual(t1, t2)
        return t1, t2

    def test_signout_with_the_replay_token_revokes_the_sibling(self):
        t1, t2 = self._t0_t1_t2()
        self.assertEqual(self.signout(t2).status_code, 200)
        self.assertEqual(self.refresh(t1).status_code, 403)
        self.assertEqual(self.refresh(t2).status_code, 403)

    def test_signout_with_the_first_token_revokes_the_replay_token(self):
        t1, t2 = self._t0_t1_t2()
        self.assertEqual(self.signout(t1).status_code, 200)
        self.assertEqual(self.refresh(t2).status_code, 403)
        self.assertEqual(self.refresh(t1).status_code, 403)

    def test_signout_with_the_original_graced_token_revokes_the_family(self):
        t1, t2 = self._t0_t1_t2()
        self.assertEqual(self.signout(self.t0).status_code, 200)
        self.assertEqual(self.refresh(t1).status_code, 403)
        self.assertEqual(self.refresh(t2).status_code, 403)

    def test_a_token_without_a_family_still_refreshes_and_its_rotation_carries_one(self):
        self.assertNotIn("fam", _claims(self.t0).payload)
        resp = self.refresh(self.t0)
        self.assertEqual(resp.status_code, 200)
        t1 = resp.cookies["refresh"].value
        self.assertEqual(_claims(t1)["fam"], _claims(self.t0)["jti"])
        # ... and the rotation keeps it
        t2 = self.refresh(t1).cookies["refresh"].value
        self.assertEqual(_claims(t2)["fam"], _claims(self.t0)["jti"])

    def test_signing_out_a_legacy_token_revokes_its_rotated_successor(self):
        t1 = self.refresh(self.t0).cookies["refresh"].value
        self.assertEqual(self.signout(self.t0).status_code, 200)
        self.assertEqual(self.refresh(t1).status_code, 403)

    def test_signout_does_not_touch_another_session(self):
        other = str(RefreshToken.for_user(self.user))
        t1, t2 = self._t0_t1_t2()
        self.assertEqual(self.signout(t2).status_code, 200)
        resp = self.refresh(other)
        self.assertEqual(resp.status_code, 200)

    def test_login_tokens_carry_a_fresh_family(self):
        from origin.serializers.common.user_serializers import CustomTokenObtainPairSerializer

        a = CustomTokenObtainPairSerializer.get_token(self.user)
        b = CustomTokenObtainPairSerializer.get_token(self.user)
        self.assertTrue(a["fam"])
        self.assertNotEqual(a["fam"], b["fam"])
