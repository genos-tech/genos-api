"""Refresh rotation must not carry a legacy `email` claim forward (MIP-766).

MIP-619 stopped minting an `email` claim, but simplejwt's rotation copies every
claim of the refresh token it is given into the next one. So a session that
began before MIP-619 kept the address in its refresh cookie, in each access
token and in a plaintext `token_blacklist_outstandingtoken` row for as long as
the user stayed active. Production had 33 such live tokens, all created after
the claim was removed.

What these pin:

  1. Rotating a token that carries `email` yields a refresh token AND an access
     token without it, through both ways into the endpoint (GET with the
     cookie, and the inherited POST).
  2. Nothing else is stripped: `username`, `user_id` and the rest survive.
  3. A token that never had the claim rotates as before.
"""

import base64
import json

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from origin.tests.test_base import BaseAPITestCase

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-refresh-scrub",
    }
}


def _claims(jwt_string):
    """The payload of a JWT, without verifying it."""
    part = jwt_string.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


@override_settings(CACHES=_LOCMEM_CACHE)
class RefreshScrubsLegacyClaimsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.user.is_active = True
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_active", "is_email_verified"])

    def _legacy_refresh(self):
        """A refresh token minted before MIP-619: it carries the user's email."""
        token = RefreshToken.for_user(self.user)
        token["username"] = self.user.username
        token["email"] = self.user.email
        return str(token)

    def _modern_refresh(self):
        token = RefreshToken.for_user(self.user)
        token["username"] = self.user.username
        return str(token)

    def _refresh_via_cookie(self, refresh):
        self.client.cookies["refresh"] = refresh
        return self.client.get(reverse("token_refresh"))

    def test_the_legacy_token_really_carries_the_claim(self):
        """Guards the fixture: without this the tests below prove nothing."""
        self.assertEqual(_claims(self._legacy_refresh())["email"], self.user.email)

    def test_rotation_over_the_cookie_drops_the_claim_from_both_tokens(self):
        resp = self._refresh_via_cookie(self._legacy_refresh())

        self.assertEqual(resp.status_code, 200, resp.content[:200])
        new_refresh = resp.cookies["refresh"].value
        self.assertNotIn("email", _claims(new_refresh))
        self.assertNotIn("email", _claims(resp.data["access"]))

    def test_rotation_over_post_drops_the_claim_from_both_tokens(self):
        resp = self.client.post(
            reverse("token_refresh"), {"refresh": self._legacy_refresh()}, format="json"
        )

        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertNotIn("email", _claims(resp.data["refresh"]))
        self.assertNotIn("email", _claims(resp.data["access"]))

    def test_other_claims_survive_the_scrub(self):
        legacy = self._legacy_refresh()
        before = _claims(legacy)

        resp = self._refresh_via_cookie(legacy)

        after = _claims(resp.cookies["refresh"].value)
        for claim in ("username", "user_id", "token_type"):
            self.assertEqual(after[claim], before[claim], claim)
        self.assertNotEqual(after["jti"], before["jti"])  # still a fresh token

    def test_a_token_that_never_had_the_claim_rotates_as_before(self):
        modern = self._modern_refresh()

        resp = self._refresh_via_cookie(modern)

        self.assertEqual(resp.status_code, 200, resp.content[:200])
        self.assertEqual(_claims(resp.cookies["refresh"].value)["username"], self.user.username)
        self.assertNotIn("email", _claims(resp.data["access"]))

    def test_a_bad_token_is_still_refused(self):
        resp = self._refresh_via_cookie("not-a-token")

        self.assertEqual(resp.status_code, 403)
