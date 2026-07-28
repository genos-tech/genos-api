"""Session revocation: a password change must evict every live session.

The defect these cover was silent and open-ended. `PasswordResetConfirmView`
set the new password and cleared the token — nothing else. The only
`.blacklist()` call in the codebase is in `LogoutView`, acting on the single
refresh token in the cookie it was handed. So a stolen session survived the
victim's password reset, and because `ROTATE_REFRESH_TOKENS=True` mints a fresh
7-day token on every refresh, it survived *indefinitely*: the attacker simply
kept refreshing. Resetting your password is the one action taken specifically to
end that, and it did nothing.

`SIMPLE_JWT["CHECK_REVOKE_TOKEN"]` fixes it by binding every token to the
password it was minted under. The tests below pin the three things that have to
hold for that to be true in practice rather than in principle:

  1. The old ACCESS token dies (`test_old_access_token_is_rejected...`).
  2. The old REFRESH token cannot buy a working one. This is the load-bearing
     case — rotation re-stamps jti/exp/iat on the SAME payload rather than
     re-minting via `for_user`, so the stale claim persists. Had simplejwt
     re-minted instead, it would have stamped the CURRENT password hash and
     handed full access straight back, leaving a hole exactly where the attack
     lives.
  3. Every minting path still produces a token that authenticates. The claim is
     stamped inside `Token.for_user`, so any login path that builds a token
     another way would emit one with no claim and be dead on arrival. The
     happy-path tests are what catch that.

Also covered: password reset is now refused for OAuth accounts, which could
otherwise be given a real password and a second way in that bypasses the
provider entirely.
"""

import hashlib
from datetime import timedelta
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework_simplejwt.settings import api_settings as jwt_api_settings
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import get_md5_hash_password

from origin.tests.test_base import BaseAPITestCase

# Throttles are keyed in the default cache, which is Redis in this project —
# shared across runs, so reset-request history would leak between tests and
# turn a 200 into a spurious 429.
_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-session-revocation",
    }
}

# A protected endpoint, used purely as "does this token still work?".
PROTECTED_URL_NAME = "user_me"


class RevocationTestMixin:
    def _tokens_for(self, user):
        """Mint a pair the way the sign-in views do."""
        refresh = RefreshToken.for_user(user)
        return str(refresh), str(refresh.access_token)

    def _get_me(self, access):
        return self.client.get(
            reverse(PROTECTED_URL_NAME), HTTP_AUTHORIZATION=f"Bearer {access}"
        )

    def _set_password(self, user, raw):
        user.set_password(raw)
        user.save(update_fields=["password"])


@override_settings(CACHES=_LOCMEM_CACHE)
class TestPasswordChangeRevokesSessions(RevocationTestMixin, BaseAPITestCase):
    def test_token_works_before_the_password_changes(self):
        # The control. Without this, every assertion below could pass
        # because the token never worked at all.
        _, access = self._tokens_for(self.user)
        self.assertEqual(self._get_me(access).status_code, 200)

    def test_old_access_token_is_rejected_after_a_password_change(self):
        _, access = self._tokens_for(self.user)
        self._set_password(self.user, "a-brand-new-password-42")

        self.assertEqual(self._get_me(access).status_code, 401)

    def test_old_refresh_token_cannot_buy_a_working_access_token(self):
        """The attack this whole change exists to stop.

        Rotation hands the holder a fresh refresh token every time, so an
        attacker who keeps using the session never loses it. What must be
        true is that the ACCESS token derived from a stale refresh is dead
        — asserting only that the refresh endpoint responds would miss it.
        """
        refresh, _ = self._tokens_for(self.user)
        self._set_password(self.user, "a-brand-new-password-42")

        derived_access = str(RefreshToken(refresh).access_token)

        self.assertEqual(self._get_me(derived_access).status_code, 401)

    def test_rotation_does_not_re_stamp_the_current_password(self):
        """Pins the simplejwt internal the fix depends on.

        `TokenRefreshSerializer` rotates by calling `set_jti/set_exp/set_iat`
        on the same payload. If a future version re-minted via `for_user`
        instead, it would stamp the CURRENT hash and silently reopen the
        hole — with every other test here still green.
        """
        refresh, _ = self._tokens_for(self.user)
        claim_before = RefreshToken(refresh).get(jwt_api_settings.REVOKE_TOKEN_CLAIM)

        self._set_password(self.user, "a-brand-new-password-42")

        rotated = RefreshToken(refresh)
        rotated.set_jti()
        rotated.set_exp()
        rotated.set_iat()

        self.assertEqual(
            rotated.get(jwt_api_settings.REVOKE_TOKEN_CLAIM),
            claim_before,
            "rotation re-stamped the claim; stale sessions would survive",
        )

    def test_another_users_session_is_untouched(self):
        # Revocation must be scoped to the account whose password changed.
        _, other_access = self._tokens_for(self.user2)
        self._set_password(self.user, "a-brand-new-password-42")

        self.assertEqual(self._get_me(other_access).status_code, 200)

    def test_a_fresh_token_after_the_change_works(self):
        self._set_password(self.user, "a-brand-new-password-42")
        _, access = self._tokens_for(self.user)

        self.assertEqual(self._get_me(access).status_code, 200)


@override_settings(CACHES=_LOCMEM_CACHE)
class TestMintingPathsStampTheClaim(RevocationTestMixin, BaseAPITestCase):
    """Guards against a login path that builds tokens outside `for_user`.

    Such a path emits a token with no claim, which `get_user` rejects on the
    first request — that login would be dead on deploy, not degraded. These
    assert the minted token actually authenticates.
    """

    def setUp(self):
        super().setUp()
        # Sign-in is gated on email verification (a 403 before any token is
        # minted), and BaseAPITestCase leaves users unverified.
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_email_verified"])

    def test_signin_mints_a_usable_token(self):
        response = self.client.post(
            reverse("token_obtain_pair"),
            {"email": self.user.email, "password": "testpass123"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.assertEqual(self._get_me(response.json()["access"]).status_code, 200)

    def test_signin_token_carries_the_claim(self):
        response = self.client.post(
            reverse("token_obtain_pair"),
            {"email": self.user.email, "password": "testpass123"},
            format="json",
        )
        refresh = RefreshToken(response.json()["refresh"])

        self.assertEqual(
            refresh.get(jwt_api_settings.REVOKE_TOKEN_CLAIM),
            get_md5_hash_password(self.user.password),
        )

    def test_oauth_style_unusable_password_still_authenticates(self):
        """OAuth accounts have `set_unusable_password()`, not a real one.

        That value is random but STABLE — it's written once in the signup
        branch and never on later logins — so the claim is stable too. If it
        were re-rolled per login, every OAuth session would die on the next
        sign-in.
        """
        self.user.set_unusable_password()
        self.user.save(update_fields=["password"])

        _, access = self._tokens_for(self.user)

        self.assertEqual(self._get_me(access).status_code, 200)


@override_settings(CACHES=_LOCMEM_CACHE)
class TestRefreshEndpointRejectsRevokedSessions(RevocationTestMixin, BaseAPITestCase):
    """The refresh endpoint must 401 rather than hand back a dead token.

    `TokenRefreshSerializer` validates only signature and expiry, so without
    an explicit check a revoked session gets a cheerful 200 plus an access
    token that fails on every real call. Nothing in the frontend recovers
    from that: `forceSignOut` fires only on a 401/403 from THIS endpoint, and
    the axios interceptor deliberately skips 401. The user would sit in a
    silently broken app until they happened to reload.
    """

    def _refresh(self, refresh_value):
        self.client.cookies["refresh"] = refresh_value
        return self.client.get(reverse("token_refresh"))

    def test_valid_session_refreshes_normally(self):
        refresh, _ = self._tokens_for(self.user)

        response = self._refresh(refresh)

        self.assertEqual(response.status_code, 200)
        self.assertIn("access", response.json())

    def test_refresh_401s_after_a_password_change(self):
        refresh, _ = self._tokens_for(self.user)
        self._set_password(self.user, "a-brand-new-password-42")

        self.assertEqual(self._refresh(refresh).status_code, 401)

    def test_refreshed_access_token_actually_works(self):
        # The 200 above must carry a token that authenticates, not just a
        # 200-shaped body.
        refresh, _ = self._tokens_for(self.user)

        access = self._refresh(refresh).json()["access"]

        self.assertEqual(self._get_me(access).status_code, 200)

    def test_refresh_401s_for_a_token_minted_before_the_flag(self):
        """Tokens predating the setting carry no claim at all.

        They're already dead at `get_user`, so the refresh endpoint has to
        agree — otherwise it hands out access tokens that cannot work.
        """
        refresh = RefreshToken.for_user(self.user)
        del refresh.payload[jwt_api_settings.REVOKE_TOKEN_CLAIM]

        self.assertEqual(self._refresh(str(refresh)).status_code, 401)

    def test_deleted_user_gets_401_not_a_500(self):
        """`TokenRefreshSerializer` resolves the user with a bare `.get()`
        and no `try`, so a token for a hard-deleted user raises
        DoesNotExist. Reaching that would 500 the one endpoint the
        frontend polls."""
        refresh, _ = self._tokens_for(self.user2)
        self.user2.delete()

        self.assertEqual(self._refresh(refresh).status_code, 401)

    def test_missing_cookie_still_403s(self):
        # Pre-existing contract; the new check must not change it.
        self.assertEqual(self.client.get(reverse("token_refresh")).status_code, 403)

    def test_garbage_cookie_is_left_to_the_serializer(self):
        # Our check bails out on unparseable input rather than masking the
        # serializer's own rejection.
        self.assertEqual(self._refresh("not-a-jwt").status_code, 403)


@override_settings(CACHES=_LOCMEM_CACHE)
class TestPasswordResetEndToEnd(RevocationTestMixin, BaseAPITestCase):
    def _issue_reset_token(self, user):
        raw = "reset-token-under-test"
        user.password_reset_token_hash = hashlib.sha256(raw.encode()).hexdigest()
        user.password_reset_token_expires_at = timezone.now() + timedelta(hours=1)
        user.save(
            update_fields=[
                "password_reset_token_hash",
                "password_reset_token_expires_at",
            ]
        )
        return raw

    def test_reset_evicts_a_live_session(self):
        """The whole point, end to end through the real endpoint."""
        _, attacker_access = self._tokens_for(self.user)
        self.assertEqual(self._get_me(attacker_access).status_code, 200)

        raw = self._issue_reset_token(self.user)
        response = self.client.post(
            reverse("password_reset_confirm"),
            {"token": raw, "new_password": "a-brand-new-password-42"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)

        self.assertEqual(self._get_me(attacker_access).status_code, 401)

    def test_reset_evicts_the_attackers_refresh_token_too(self):
        attacker_refresh, _ = self._tokens_for(self.user)

        raw = self._issue_reset_token(self.user)
        self.client.post(
            reverse("password_reset_confirm"),
            {"token": raw, "new_password": "a-brand-new-password-42"},
            format="json",
        )

        derived = str(RefreshToken(attacker_refresh).access_token)
        self.assertEqual(self._get_me(derived).status_code, 401)


@override_settings(CACHES=_LOCMEM_CACHE)
class TestPasswordResetRefusedForOAuthAccounts(BaseAPITestCase):
    """A reset must never give an OAuth-only account a real password.

    It would create a second way in that bypasses everything the provider
    does for us — Google's MFA, its anomaly detection, an org's ability to
    disable the account centrally. `is_email_verified` is already True on
    OAuth signups, so nothing downstream would have blocked that login.
    """

    def setUp(self):
        super().setUp()
        self.user.primary_auth_provider = "google"
        self.user.set_unusable_password()
        self.user.is_email_verified = True
        self.user.save()

    @mock.patch("origin.views.common.auth_views.send_templated_email")
    def test_request_mints_no_token_for_a_google_account(self, send_email):
        response = self.client.post(
            reverse("password_reset_request"),
            {"email": self.user.email},
            format="json",
        )

        # Still 200 and still silent, so this stays enumeration-safe.
        self.assertEqual(response.status_code, 200)
        send_email.assert_not_called()

        self.user.refresh_from_db()
        self.assertIsNone(self.user.password_reset_token_hash)

    @mock.patch("origin.views.common.auth_views.send_templated_email")
    def test_request_still_works_for_an_email_account(self, send_email):
        response = self.client.post(
            reverse("password_reset_request"),
            {"email": self.user2.email},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        send_email.assert_called_once()

        self.user2.refresh_from_db()
        self.assertIsNotNone(self.user2.password_reset_token_hash)

    def test_confirm_refuses_a_pre_existing_token_for_a_google_account(self):
        """Defence in depth: tokens minted before the request-side gate
        existed are still live for their TTL."""
        raw = "leftover-token"
        self.user.password_reset_token_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.user.password_reset_token_expires_at = timezone.now() + timedelta(hours=1)
        self.user.save()

        response = self.client.post(
            reverse("password_reset_confirm"),
            {"token": raw, "new_password": "a-brand-new-password-42"},
            format="json",
        )

        # Same 400 as an unknown token, so this adds no signal.
        self.assertEqual(response.status_code, 400)

        self.user.refresh_from_db()
        self.assertFalse(self.user.has_usable_password())

    def test_google_account_cannot_sign_in_with_a_password_afterwards(self):
        """The consequence, stated as behaviour rather than internals."""
        raw = "leftover-token"
        self.user.password_reset_token_hash = hashlib.sha256(raw.encode()).hexdigest()
        self.user.password_reset_token_expires_at = timezone.now() + timedelta(hours=1)
        self.user.save()

        self.client.post(
            reverse("password_reset_confirm"),
            {"token": raw, "new_password": "a-brand-new-password-42"},
            format="json",
        )

        response = self.client.post(
            reverse("token_obtain_pair"),
            {"email": self.user.email, "password": "a-brand-new-password-42"},
            format="json",
        )

        self.assertEqual(response.status_code, 401)
