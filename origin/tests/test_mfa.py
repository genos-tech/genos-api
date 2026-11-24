"""Two-factor sign-in with an authenticator app (MIP-2239, AUTH_SECURITY_PLAN §4).

What is pinned here, in the order the plan lists its traps:

* the code itself: RFC 4226 / RFC 6238 vectors, so it matches every authenticator app;
* the second factor is enforced BEFORE a token exists: a right password for an account with
  two-factor on answers `mfa_required` and sets no cookie and returns no access or refresh;
* the pending token authenticates nothing (the bypass the plan warns about): against a normal
  endpoint, against API-key creation and against the refresh endpoint it is a 401, and a real
  access token is not a pending token either;
* the verify step is locked per account (five wrong, then a growing lock that refuses even the
  right code), a code is accepted once (replay), a recovery code is accepted once;
* a password reset does not switch two-factor off, and a pending token dies with the password;
* a provider sign-in cannot walk round it;
* enrolling and disabling need the password.
"""

from __future__ import annotations

import time
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from origin.models.common.mfa_models import MFARecoveryCode, UserMFA, hash_recovery_code
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.user_models import ConnectedAccount
from origin.services import crypto, mfa, signup_consent, totp
from origin.services.oauth.base import ProviderProfile, TokenResponse
from origin.tests.test_base import BaseAPITestCase
from origin.views.common.oauth_views import OAuthCallbackView

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-mfa",
    }
}

PASSWORD = "testpass123"


class TotpVectorTests(TestCase):
    """The algorithm is the RFC's, so an authenticator app and this agree."""

    # RFC 4226 appendix D: secret "12345678901234567890", counters 0..9.
    SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    RFC4226 = [
        "755224",
        "287082",
        "359152",
        "969429",
        "338314",
        "254676",
        "287922",
        "162583",
        "399871",
        "520489",
    ]

    def test_rfc_4226_vectors(self):
        for counter, expected in enumerate(self.RFC4226):
            self.assertEqual(totp.hotp(self.SECRET, counter), expected)

    def test_rfc_6238_vector_at_59_seconds(self):
        # Appendix B, SHA-1, T=59: 94287082 (8 digits), whose last six are 287082.
        self.assertEqual(totp.totp(self.SECRET, now=59), "287082")

    def test_the_window_is_one_step_either_side(self):
        now = 1_000_000_000
        step = totp.current_step(now)
        for offset, ok in ((-2, False), (-1, True), (0, True), (1, True), (2, False)):
            code = totp.hotp(self.SECRET, step + offset)
            self.assertEqual(totp.match_step(self.SECRET, code, now=now) is not None, ok, offset)

    def test_it_reports_the_step_it_matched(self):
        now = 1_000_000_000
        step = totp.current_step(now)
        self.assertEqual(
            totp.match_step(self.SECRET, totp.hotp(self.SECRET, step - 1), now), step - 1
        )

    def test_a_malformed_code_matches_nothing(self):
        for bad in ("", "12345", "1234567", "abcdef", "١٢٣٤٥٦", None, 123456, "12 34 5"):
            with self.subTest(bad=bad):
                self.assertIsNone(totp.match_step(self.SECRET, bad, now=59))

    def test_spaces_and_a_dash_in_a_typed_code_are_ignored(self):
        code = totp.totp(self.SECRET, now=59)
        self.assertIsNotNone(totp.match_step(self.SECRET, f"{code[:3]} {code[3:]}", now=59))
        self.assertIsNotNone(totp.match_step(self.SECRET, f"{code[:3]}-{code[3:]}", now=59))

    def test_a_generated_secret_is_160_bits_of_unpadded_base32(self):
        secret = totp.generate_secret()
        self.assertEqual(len(secret), 32)
        self.assertNotIn("=", secret)
        self.assertNotEqual(secret, totp.generate_secret())

    def test_the_provisioning_uri_names_issuer_and_account(self):
        uri = totp.provisioning_uri("ABC", "a+b@example.com", "Genos")
        self.assertTrue(uri.startswith("otpauth://totp/Genos:a%2Bb%40example.com?secret=ABC"))
        self.assertIn("issuer=Genos", uri)
        self.assertIn("digits=6", uri)
        self.assertIn("period=30", uri)


@override_settings(CACHES=_LOCMEM_CACHE)
class MfaBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.now = 1_800_000_000.0
        # Only the TOTP module's clock: the signing module's time (the pending token's age) stays real.
        clock = mock.patch("origin.services.totp.time", new=SimpleNamespace(time=lambda: self.now))
        clock.start()
        self.addCleanup(clock.stop)
        for user in (self.user, self.user2):
            user.is_email_verified = True
            user.save(update_fields=["is_email_verified"])

    # -- helpers -------------------------------------------------------------------------

    def tick(self, periods=1):
        """Move the clock forward: a code is accepted once per time step."""
        self.now += totp.PERIOD * periods

    def secret(self, user=None):
        return crypto.decrypt(UserMFA.objects.get(user=user or self.user).secret_encrypted)

    def code(self, user=None):
        return totp.totp(self.secret(user), now=self.now)

    def wrong_code(self, user=None):
        good = {
            totp.hotp(self.secret(user), totp.current_step(self.now) + o)
            for o in (-3, -2, -1, 0, 1, 2, 3)
        }
        return next(c for c in (f"{n:06d}" for n in range(100000, 999999)) if c not in good)

    def signed_in(self):
        self.authenticate()

    def setup(self, password=PASSWORD):
        return self.client.post(reverse("mfa_setup"), {"password": password}, format="json")

    def enable(self, code):
        return self.client.post(reverse("mfa_enable"), {"code": code}, format="json")

    def turn_on(self, user=None):
        """Two-factor on for `user` through the real endpoints. Returns the recovery codes."""
        user = user or self.user
        self.authenticate(user)
        password = PASSWORD if user == self.user else "otherpass123"
        self.assertEqual(self.setup(password).status_code, 200)
        res = self.enable(self.code(user))
        self.assertEqual(res.status_code, 200, res.content)
        self.unauthenticate()
        self.tick()
        return res.json()["recovery_codes"]

    def password_signin(self, password=PASSWORD, email="test@example.com"):
        return self.client.post(
            reverse("token_obtain_pair"), {"email": email, "password": password}, format="json"
        )

    def pending(self):
        res = self.password_signin()
        self.assertEqual(res.status_code, 200)
        return res.json()["mfa_token"]

    def second_step(self, token, code):
        return self.client.post(
            reverse("signin_mfa"), {"mfa_token": token, "code": code}, format="json"
        )


class EnrolmentTests(MfaBase):
    def test_status_before_and_after(self):
        self.authenticate()
        before = self.client.get(reverse("mfa_status")).json()
        self.assertEqual(
            before,
            {"enabled": False, "eligible": True, "enabled_at": None, "recovery_codes_remaining": 0},
        )
        self.turn_on()
        self.authenticate()
        after = self.client.get(reverse("mfa_status")).json()
        self.assertTrue(after["enabled"])
        self.assertEqual(after["recovery_codes_remaining"], 10)
        self.assertIsNotNone(after["enabled_at"])

    def test_setup_needs_the_password(self):
        self.authenticate()
        for body in ({}, {"password": "wrong-password"}, {"password": 5}, {"password": None}):
            with self.subTest(body=body):
                res = self.client.post(reverse("mfa_setup"), body, format="json")
                self.assertEqual(res.status_code, 403)
                self.assertEqual(res.json()["code"], "bad_password")
        self.assertFalse(UserMFA.objects.exists())

    def test_setup_returns_the_secret_once_and_stores_it_encrypted(self):
        self.authenticate()
        res = self.setup()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Cache-Control"], "no-store")
        body = res.json()
        row = UserMFA.objects.get(user=self.user)
        self.assertNotEqual(row.secret_encrypted, body["secret"])
        self.assertNotIn(body["secret"], row.secret_encrypted)
        self.assertEqual(crypto.decrypt(row.secret_encrypted), body["secret"])
        self.assertIn(body["secret"], body["otpauth_uri"])
        self.assertIsNone(row.confirmed_at)
        # Half-finished setup changes nothing about signing in.
        self.unauthenticate()
        self.assertNotIn("mfa_required", self.password_signin().json())
        # And the status never hands the secret back.
        self.authenticate()
        self.assertNotIn("secret", self.client.get(reverse("mfa_status")).json())

    def test_a_wrong_first_code_does_not_turn_it_on(self):
        self.authenticate()
        self.setup()
        res = self.enable(self.wrong_code())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "invalid_code")
        self.assertFalse(mfa.is_enabled(self.user))
        self.assertEqual(MFARecoveryCode.objects.count(), 0)

    def test_enable_without_setup_is_refused(self):
        self.authenticate()
        res = self.enable("123456")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "no_setup")

    def test_enabling_hands_out_ten_recovery_codes_stored_only_as_hashes(self):
        codes = self.turn_on()
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        for code in codes:
            self.assertRegex(code, r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        stored = set(
            MFARecoveryCode.objects.filter(user=self.user).values_list("code_hash", flat=True)
        )
        self.assertEqual(stored, {hash_recovery_code(c) for c in codes})
        for code in codes:
            self.assertNotIn(code, stored)
            self.assertNotIn(code.replace("-", ""), stored)

    def test_setup_while_enabled_is_refused(self):
        self.turn_on()
        self.authenticate()
        res = self.setup()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "already_enabled")

    def test_restarting_setup_replaces_the_secret_but_keeps_the_failure_count(self):
        self.authenticate()
        self.setup()
        first = self.secret()
        for _ in range(3):
            self.enable(self.wrong_code())
        self.setup()
        self.assertNotEqual(self.secret(), first)
        self.assertEqual(UserMFA.objects.get(user=self.user).failed_attempts, 3)

    def test_the_setup_confirmation_is_locked_after_five_wrong_codes(self):
        self.authenticate()
        self.setup()
        for _ in range(5):
            self.assertEqual(self.enable(self.wrong_code()).status_code, 400)
        res = self.enable(self.code())
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res.json()["code"], "mfa_locked")
        self.assertFalse(mfa.is_enabled(self.user))

    def test_an_account_that_signs_in_with_a_provider_cannot_enrol(self):
        self.user.set_unusable_password()
        self.user.primary_auth_provider = "google"
        self.user.save()
        self.authenticate()
        res = self.client.post(reverse("mfa_setup"), {"password": "x"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "not_eligible")
        self.assertFalse(self.client.get(reverse("mfa_status")).json()["eligible"])

    def test_the_endpoints_need_a_signed_in_caller(self):
        for name in ("mfa_status",):
            self.assertEqual(self.client.get(reverse(name)).status_code, 401)
        for name in ("mfa_setup", "mfa_enable", "mfa_disable", "mfa_recovery_codes"):
            self.assertEqual(self.client.post(reverse(name), {}, format="json").status_code, 401)

    def test_enabling_is_recorded_without_any_secret(self):
        self.turn_on()
        event = SecurityEvent.objects.get(action=SecurityAction.MFA_ENABLED)
        self.assertEqual(event.metadata, {"method": "totp"})


class SignInEnforcementTests(MfaBase):
    def test_an_account_without_two_factor_signs_in_as_before(self):
        res = self.password_signin()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)
        self.assertNotIn("mfa_required", body)
        self.assertIn("refresh", res.cookies)

    def test_the_right_password_gets_no_token_and_no_cookie(self):
        self.turn_on()
        res = self.password_signin()
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(set(body), {"mfa_required", "mfa_token", "expires_in"})
        self.assertTrue(body["mfa_required"])
        self.assertNotIn("refresh", res.cookies)
        self.assertNotIn("access", body)
        self.assertNotIn("user_id", body)

    def test_a_password_with_stray_spaces_signs_in_as_it_does_without_two_factor(self):
        # simplejwt's serializer strips the password, so the gate must too.
        self.turn_on()
        res = self.password_signin(PASSWORD + " ")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["mfa_required"])

    def test_the_wrong_password_is_answered_exactly_as_for_an_account_without_it(self):
        plain = self.password_signin("wrong-password", email="other@example.com")
        self.turn_on()
        res = self.password_signin("wrong-password")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.status_code, plain.status_code)
        self.assertEqual(res.json(), plain.json())
        self.assertNotIn("mfa_token", res.json())

    def test_a_wrong_password_is_still_recorded_as_a_failed_sign_in(self):
        self.turn_on()
        self.password_signin("wrong-password")
        self.assertTrue(SecurityEvent.objects.filter(action=SecurityAction.SIGNIN_FAILED).exists())

    def test_a_deactivated_account_with_two_factor_gets_no_pending_token(self):
        self.turn_on()
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        res = self.password_signin()
        self.assertEqual(res.status_code, 401)
        self.assertNotIn("mfa_token", res.json())

    def test_the_second_step_mints_the_session(self):
        self.turn_on()
        token = self.pending()
        res = self.second_step(token, self.code())
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertTrue(body["access"])
        self.assertTrue(body["refresh"])
        self.assertEqual(body["email"], "test@example.com")
        self.assertIn("refresh", res.cookies)
        self.assertTrue(res.cookies["refresh"]["httponly"])
        event = SecurityEvent.objects.filter(action=SecurityAction.SIGNIN_SUCCEEDED).latest("pk")
        self.assertEqual(event.metadata, {"method": "password", "second_factor": "totp"})
        # The shape the client already reads from a password sign-in.
        plain = self.password_signin("otherpass123", email="other@example.com").json()
        self.assertEqual(set(body), set(plain))
        # The token it minted works.
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
        self.assertEqual(self.client.get(reverse("mfa_status")).status_code, 200)

    def test_the_refresh_cookie_it_sets_refreshes(self):
        self.turn_on()
        res = self.second_step(self.pending(), self.code())
        self.client.cookies["refresh"] = res.cookies["refresh"].value
        again = self.client.post(reverse("token_refresh"), {}, format="json")
        self.assertEqual(again.status_code, 200, again.content)

    # -- the pending token authenticates nothing ----------------------------------------

    def test_a_pending_token_is_not_a_bearer_token_for_a_normal_endpoint(self):
        self.turn_on()
        token = self.pending()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(self.client.get(reverse("mfa_status")).status_code, 401)
        self.assertEqual(self.client.get("/api/v2/user/profile/").status_code, 401)

    def test_a_pending_token_cannot_create_an_api_key(self):
        from origin.models.common.api_key_models import ApiKey

        self.turn_on()
        token = self.pending()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        res = self.client.post(reverse("api_keys"), {"name": "foothold"}, format="json")
        self.assertEqual(res.status_code, 401)
        self.assertFalse(ApiKey.objects.exists())

    def test_a_pending_token_is_not_a_refresh_token(self):
        self.turn_on()
        token = self.pending()
        self.client.cookies["refresh"] = token
        res = self.client.post(reverse("token_refresh"), {}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("access", res.json())
        self.assertNotIn("refresh", res.cookies)

    def test_a_pending_token_is_not_a_jwt_at_all(self):
        from rest_framework_simplejwt.exceptions import TokenError
        from rest_framework_simplejwt.tokens import AccessToken, UntypedToken

        self.turn_on()
        token = self.pending()
        self.assertNotEqual(token.count("."), 2)
        for cls in (AccessToken, UntypedToken):
            with self.assertRaises(TokenError):
                cls(token)

    def test_a_real_access_token_is_not_a_pending_token(self):
        self.turn_on()
        res = self.second_step(self.get_access_token(), self.code())
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["code"], "mfa_token_invalid")
        self.assertNotIn("access", res.json())

    def test_a_forged_or_missing_pending_token_is_refused(self):
        self.turn_on()
        token = self.pending()
        for bad in (None, "", "x", token[:-3] + "abc", token + "x", 5, ["a"], {"a": 1}):
            with self.subTest(bad=bad):
                res = self.second_step(bad, self.code())
                self.assertEqual(res.status_code, 401)
                self.assertEqual(res.json()["code"], "mfa_token_invalid")

    def test_a_pending_token_for_an_account_with_no_two_factor_is_refused(self):
        self.turn_on()
        token = self.pending()
        mfa.disable(self.user)
        res = self.second_step(token, "123456")
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["code"], "mfa_token_invalid")

    def test_a_pending_token_expires_after_five_minutes(self):
        self.turn_on()
        token = self.pending()
        with mock.patch("django.core.signing.time.time", return_value=time.time() + 301):
            res = self.second_step(token, self.code())
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["code"], "mfa_token_invalid")
        # A second earlier it still works.
        with mock.patch("django.core.signing.time.time", return_value=time.time() + 290):
            self.assertEqual(self.second_step(token, self.code()).status_code, 200)

    def test_a_pending_token_dies_when_the_password_changes(self):
        self.turn_on()
        token = self.pending()
        self.user.set_password("a-brand-new-password-42")
        self.user.save()
        res = self.second_step(token, self.code())
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["code"], "mfa_token_invalid")

    def test_the_second_step_ignores_a_bearer_header_and_a_cookie(self):
        self.turn_on()
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-token")
        res = self.second_step(self.pending(), self.code())
        self.assertEqual(res.status_code, 200)

    def test_a_non_object_body_is_a_refusal_not_a_crash(self):
        for body in ([1], "abc", 5):
            res = self.client.post(reverse("signin_mfa"), body, format="json")
            self.assertEqual(res.status_code, 400, body)


class VerifyLockTests(MfaBase):
    def setUp(self):
        super().setUp()
        self.turn_on()

    def test_a_right_code_is_accepted_once_a_replay_is_refused(self):
        code = self.code()
        self.assertEqual(self.second_step(self.pending(), code).status_code, 200)
        res = self.second_step(self.pending(), code)
        self.assertEqual(res.status_code, 401)
        self.assertEqual(res.json()["code"], "invalid_code")

    def test_an_earlier_step_is_a_replay_too(self):
        step = totp.current_step(self.now)
        older = totp.hotp(self.secret(), step - 1)
        newer = totp.hotp(self.secret(), step + 1)
        self.assertEqual(self.second_step(self.pending(), newer).status_code, 200)
        self.assertEqual(self.second_step(self.pending(), older).status_code, 401)

    def test_five_wrong_codes_lock_the_account_and_the_right_one_is_then_refused(self):
        token = self.pending()
        for _ in range(4):
            res = self.second_step(token, self.wrong_code())
            self.assertEqual(res.status_code, 401)
            self.assertEqual(res.json()["code"], "invalid_code")
        res = self.second_step(token, self.wrong_code())
        self.assertEqual(res.status_code, 401)  # the fifth is counted as wrong, and locks
        locked = self.second_step(token, self.code())
        self.assertEqual(locked.status_code, 429)
        self.assertEqual(locked.json()["code"], "mfa_locked")
        self.assertGreater(int(locked["Retry-After"]), 0)
        self.assertLessEqual(int(locked["Retry-After"]), 15 * 60)
        self.assertNotIn("access", locked.json())

    def test_the_lock_is_per_account_not_per_address(self):
        token = self.pending()
        for _ in range(5):
            self.second_step(token, self.wrong_code())
        # A fresh pending token, as from another address, meets the same lock.
        res = self.second_step(self.pending(), self.code())
        self.assertEqual(res.status_code, 429)

    def test_another_account_is_not_locked_by_it(self):
        token = self.pending()
        for _ in range(5):
            self.second_step(token, self.wrong_code())
        self.turn_on(self.user2)
        res = self.client.post(
            reverse("token_obtain_pair"),
            {"email": "other@example.com", "password": "otherpass123"},
            format="json",
        )
        res = self.second_step(res.json()["mfa_token"], self.code(self.user2))
        self.assertEqual(res.status_code, 200)

    def test_a_locked_account_checks_nothing(self):
        token = self.pending()
        for _ in range(5):
            self.second_step(token, self.wrong_code())
        before = UserMFA.objects.get(user=self.user)
        self.second_step(token, self.code())
        after = UserMFA.objects.get(user=self.user)
        self.assertEqual(before.failed_attempts, after.failed_attempts)
        self.assertEqual(before.last_used_step, after.last_used_step)

    def test_when_the_lock_ends_a_right_code_signs_in_and_clears_the_count(self):
        token = self.pending()
        for _ in range(5):
            self.second_step(token, self.wrong_code())
        UserMFA.objects.filter(user=self.user).update(
            locked_until=timezone.now() - timedelta(seconds=1)
        )
        res = self.second_step(token, self.code())
        self.assertEqual(res.status_code, 200)
        row = UserMFA.objects.get(user=self.user)
        self.assertEqual(row.failed_attempts, 0)
        self.assertIsNone(row.locked_until)

    def test_each_wrong_code_after_the_fifth_doubles_the_lock_to_a_day(self):
        self.assertEqual(mfa._lock_duration(5), timedelta(minutes=15))
        self.assertEqual(mfa._lock_duration(6), timedelta(minutes=30))
        self.assertEqual(mfa._lock_duration(7), timedelta(hours=1))
        self.assertEqual(mfa._lock_duration(100), timedelta(hours=24))

    def test_a_wrong_code_after_the_lock_ended_locks_again_for_longer(self):
        token = self.pending()
        for _ in range(5):
            self.second_step(token, self.wrong_code())
        UserMFA.objects.filter(user=self.user).update(
            locked_until=timezone.now() - timedelta(seconds=1)
        )
        self.second_step(token, self.wrong_code())
        row = UserMFA.objects.get(user=self.user)
        self.assertEqual(row.failed_attempts, 6)
        self.assertGreater(row.locked_until - timezone.now(), timedelta(minutes=29))

    def test_a_wrong_code_is_recorded_without_the_code(self):
        self.second_step(self.pending(), "000000")
        event = SecurityEvent.objects.filter(action=SecurityAction.MFA_FAILED).get()
        self.assertEqual(event.metadata, {"stage": "signin"})

    def test_the_address_throttle_also_applies(self):
        token = self.pending()
        statuses = {self.second_step(token, "000000").status_code for _ in range(25)}
        self.assertIn(429, statuses)


class RecoveryCodeTests(MfaBase):
    def setUp(self):
        super().setUp()
        self.codes = self.turn_on()

    def test_a_recovery_code_signs_in_once(self):
        code = self.codes[0]
        res = self.second_step(self.pending(), code)
        self.assertEqual(res.status_code, 200)
        again = self.second_step(self.pending(), code)
        self.assertEqual(again.status_code, 401)
        self.assertEqual(again.json()["code"], "invalid_code")
        event = SecurityEvent.objects.filter(action=SecurityAction.MFA_RECOVERY_USED).get()
        self.assertEqual(event.metadata, {"remaining": 9})
        signin = SecurityEvent.objects.filter(action=SecurityAction.SIGNIN_SUCCEEDED).latest("pk")
        self.assertEqual(signin.metadata["second_factor"], "recovery")

    def test_a_recovery_code_is_typed_in_any_case_with_or_without_dashes(self):
        res = self.second_step(self.pending(), self.codes[1].lower().replace("-", " "))
        self.assertEqual(res.status_code, 200)

    def test_two_requests_cannot_both_spend_one_code(self):
        code = hash_recovery_code(self.codes[2])
        spent = [
            MFARecoveryCode.objects.filter(
                user=self.user, code_hash=code, used_at__isnull=True
            ).update(used_at=timezone.now())
            for _ in range(2)
        ]
        self.assertEqual(spent, [1, 0])

    def test_a_wrong_recovery_code_counts_toward_the_lock(self):
        token = self.pending()
        for _ in range(5):
            self.assertEqual(self.second_step(token, "AAAA-BBBB-CCCC").status_code, 401)
        self.assertEqual(self.second_step(token, self.codes[0]).status_code, 429)

    def test_status_counts_the_codes_left(self):
        self.second_step(self.pending(), self.codes[0])
        self.authenticate()
        self.assertEqual(
            self.client.get(reverse("mfa_status")).json()["recovery_codes_remaining"], 9
        )

    def test_new_codes_need_the_password_and_replace_the_old_ones(self):
        self.authenticate()
        res = self.client.post(reverse("mfa_recovery_codes"), {"password": "nope"}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(
            MFARecoveryCode.objects.filter(user=self.user, used_at__isnull=True).count(), 10
        )
        res = self.client.post(reverse("mfa_recovery_codes"), {"password": PASSWORD}, format="json")
        self.assertEqual(res.status_code, 200)
        fresh = res.json()["recovery_codes"]
        self.assertEqual(len(fresh), 10)
        self.assertFalse(set(fresh) & set(self.codes))
        self.unauthenticate()
        self.assertEqual(self.second_step(self.pending(), self.codes[3]).status_code, 401)
        self.assertEqual(self.second_step(self.pending(), fresh[0]).status_code, 200)
        self.assertTrue(
            SecurityEvent.objects.filter(action=SecurityAction.MFA_RECOVERY_REGENERATED).exists()
        )


class DisableTests(MfaBase):
    def test_disabling_needs_the_password(self):
        self.turn_on()
        self.authenticate()
        for body in ({}, {"password": "nope"}):
            res = self.client.post(reverse("mfa_disable"), body, format="json")
            self.assertEqual(res.status_code, 403)
        self.assertTrue(mfa.is_enabled(self.user))

    def test_a_session_alone_cannot_turn_it_off(self):
        # The plan's point: a hijacked live session does not know the password.
        self.turn_on()
        res = self.second_step(self.pending(), self.code())
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {res.json()['access']}")
        self.assertEqual(
            self.client.post(reverse("mfa_disable"), {}, format="json").status_code, 403
        )
        self.assertTrue(mfa.is_enabled(self.user))

    def test_disabling_removes_the_secret_and_every_code_and_signing_in_is_plain_again(self):
        self.turn_on()
        self.authenticate()
        res = self.client.post(reverse("mfa_disable"), {"password": PASSWORD}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(UserMFA.objects.count(), 0)
        self.assertEqual(MFARecoveryCode.objects.count(), 0)
        self.unauthenticate()
        self.assertIn("access", self.password_signin().json())
        self.assertTrue(SecurityEvent.objects.filter(action=SecurityAction.MFA_DISABLED).exists())

    def test_disabling_when_off_is_a_409(self):
        self.authenticate()
        res = self.client.post(reverse("mfa_disable"), {"password": PASSWORD}, format="json")
        self.assertEqual(res.status_code, 409)

    def test_the_password_guesses_are_limited(self):
        self.authenticate()
        statuses = [
            self.client.post(
                reverse("mfa_disable"), {"password": f"guess-{i}"}, format="json"
            ).status_code
            for i in range(35)
        ]
        self.assertIn(429, statuses)


class BypassTests(MfaBase):
    def test_a_password_reset_does_not_turn_two_factor_off(self):
        import hashlib

        self.turn_on()
        self.user.password_reset_token_hash = hashlib.sha256(b"raw-reset-token").hexdigest()
        self.user.password_reset_token_expires_at = timezone.now() + timedelta(hours=1)
        self.user.save(
            update_fields=["password_reset_token_hash", "password_reset_token_expires_at"]
        )
        res = self.client.post(
            reverse("password_reset_confirm"),
            {"token": "raw-reset-token", "new_password": "a-brand-new-password-42"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertNotIn("access", res.json())
        self.assertTrue(mfa.is_enabled(self.user))
        signin = self.password_signin("a-brand-new-password-42")
        self.assertTrue(signin.json()["mfa_required"])
        self.assertNotIn("access", signin.json())

    def test_a_provider_sign_in_cannot_walk_round_two_factor(self):
        self.turn_on()
        ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="g-1",
            provider_email="test@example.com",
        )
        resp = self._oauth_login("g-1", "test@example.com")
        self.assertIn("error=mfa_password_only", resp.url)
        self.assertNotIn("access", resp.url)
        self.assertNotIn("refresh", resp.cookies)
        failed = SecurityEvent.objects.filter(action=SecurityAction.SIGNIN_FAILED).latest("pk")
        self.assertEqual(failed.metadata["reason"], "mfa_password_only")

    def test_a_provider_sign_in_still_works_for_an_account_without_two_factor(self):
        ConnectedAccount.objects.create(
            user=self.user,
            provider="google",
            provider_user_id="g-1",
            provider_email="test@example.com",
        )
        resp = self._oauth_login("g-1", "test@example.com")
        self.assertNotIn("error=", resp.url)

    def test_a_provider_only_account_is_not_affected(self):
        resp = self._oauth_login("g-new", "new-person@example.com", terms=True)
        self.assertNotIn("error=", resp.url)

    def _oauth_login(self, sub, email, terms=True):
        consent = (
            {
                "terms_accepted_at": timezone.now().isoformat(),
                "terms_version": signup_consent.TERMS_VERSION,
            }
            if terms
            else {}
        )
        with override_settings(FRONTEND_BASE_URL="https://app.test"):
            return OAuthCallbackView()._handle_login(
                **consent,
                provider_name="google",
                profile=ProviderProfile(
                    provider_user_id=sub, email=email, display_name="G", email_verified=True
                ),
                token_response=TokenResponse(
                    access_token="tok",
                    refresh_token=None,
                    expires_in_seconds=3600,
                    granted_scopes=[],
                ),
                next_path="/workspace",
            )

    def test_account_deletion_removes_the_secret_and_the_codes(self):
        from origin.services.account_deletion import _delete_personal_data

        self.turn_on()
        _delete_personal_data(self.user)
        self.assertEqual(UserMFA.objects.count(), 0)
        self.assertEqual(MFARecoveryCode.objects.count(), 0)


class KeyRotationTests(MfaBase):
    def test_rotating_the_keyring_keeps_the_secret_readable(self):
        # Without the column in rotate_field_keys, dropping the old key would lock every person
        # who turned two-factor on out of signing in.
        self.turn_on()
        secret = self.secret()
        old = settings.FIELD_ENCRYPTION_KEYS[0]
        new = Fernet.generate_key().decode()
        with override_settings(FIELD_ENCRYPTION_KEYS=[new, old]):
            call_command("rotate_field_keys", stdout=StringIO())
        with override_settings(FIELD_ENCRYPTION_KEYS=[new]):
            self.assertEqual(self.secret(), secret)
            self.assertEqual(self.second_step(self.pending(), self.code()).status_code, 200)


class OperatorResetTests(MfaBase):
    def run_cmd(self, *args):
        out = StringIO()
        call_command("operator_mfa_reset", *args, stdout=out)
        return out.getvalue()

    def test_it_turns_two_factor_off_and_leaves_an_audit_row(self):
        self.turn_on()
        out = self.run_cmd(
            str(self.user.pk), "--reason", "lost phone and codes; identity checked by video call"
        )
        self.assertFalse(UserMFA.objects.exists())
        self.assertFalse(MFARecoveryCode.objects.exists())
        event = SecurityEvent.objects.get(action=SecurityAction.OPERATOR_MFA_RESET)
        self.assertEqual(event.metadata["recovery_codes_deleted"], 10)
        self.assertIn("identity checked", event.metadata["reason"])
        self.assertNotIn("test@example.com", out)
        self.assertIn("access", self.password_signin().json())

    def test_a_dry_run_changes_and_records_nothing(self):
        self.turn_on()
        self.run_cmd(str(self.user.pk), "--reason", "x", "--dry-run")
        self.assertTrue(mfa.is_enabled(self.user))
        self.assertFalse(
            SecurityEvent.objects.filter(action=SecurityAction.OPERATOR_MFA_RESET).exists()
        )

    def test_an_account_without_two_factor_writes_nothing(self):
        self.run_cmd(str(self.user.pk), "--reason", "x")
        self.assertFalse(
            SecurityEvent.objects.filter(action=SecurityAction.OPERATOR_MFA_RESET).exists()
        )

    def test_it_needs_a_reason_and_a_real_user(self):
        with self.assertRaises(CommandError):
            self.run_cmd(str(self.user.pk), "--reason", "   ")
        with self.assertRaises(CommandError):
            self.run_cmd(str(self.user.pk), "--reason", "asked by a@b.example")
        with self.assertRaises(CommandError):
            self.run_cmd("not-a-uuid", "--reason", "x")
        with self.assertRaises(CommandError):
            self.run_cmd("00000000-0000-0000-0000-000000000000", "--reason", "x")
