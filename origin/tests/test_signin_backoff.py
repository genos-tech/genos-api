"""Per-account sign-in backoff and the password-spray alert (MIP-2237)."""

from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from origin.models.common.ops_models import OpsAlert
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.services import signin_backoff, throttles
from origin.tests.test_base import BaseAPITestCase

_LOCMEM = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "test-signin-backoff",
    }
}
URL = "/api/v2/user/signin/"


@override_settings(CACHES=_LOCMEM, SIGNIN_BACKOFF_FAILURES=3, SIGNIN_SPRAY_ACCOUNTS=4)
class SignInBackoffTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        for user in (self.user, self.user2):
            user.is_email_verified = True
            user.save(update_fields=["is_email_verified"])

    def _fail(self, email="test@example.com", n=1):
        return [self.client.post(URL, {"email": email, "password": "wrong"}) for _ in range(n)]

    def _good(self, email="test@example.com", password="testpass123"):
        return self.client.post(URL, {"email": email, "password": password})

    def test_account_is_refused_after_n_failures_even_with_the_right_password(self):
        self.assertEqual({r.status_code for r in self._fail(n=3)}, {401})
        locked = self._good()
        self.assertEqual(locked.status_code, 429)
        self.assertGreaterEqual(int(locked["Retry-After"]), 1)

    def test_other_accounts_are_not_affected(self):
        self._fail(n=3)
        self.assertEqual(self._good("other@example.com", "otherpass123").status_code, 200)

    def test_lock_grows_with_further_failures(self):
        self._fail(n=3)
        first = int(self._good()["Retry-After"])
        # a refused attempt is not a guess: the lock is unchanged
        self.assertLessEqual(int(self._good()["Retry-After"]), first)
        key = signin_backoff._account_key("test@example.com")
        cache.set(f"signin_lock:{key}", 0)  # lock lapsed
        self._fail()
        self.assertGreater(int(self._good()["Retry-After"]), first)

    def test_same_answer_whether_or_not_the_account_exists(self):
        self._fail("ghost@example.com", n=3)
        ghost = self._good("ghost@example.com", "whatever")
        self._fail("test@example.com", n=3)
        real = self._good("test@example.com", "whatever")
        self.assertEqual(ghost.status_code, 429)
        self.assertEqual(real.status_code, 429)
        self.assertEqual(ghost.json().keys(), real.json().keys())

    def test_case_and_spacing_of_the_address_do_not_dodge_the_counter(self):
        self._fail("TEST@example.com", n=2)
        self._fail(" test@Example.com ", n=1)
        self.assertEqual(self._good().status_code, 429)

    def test_no_plaintext_email_in_the_cache_keys(self):
        self._fail(n=1)
        # LocMemCache keeps its keys in a private dict as ":1:<key>".
        names = list(cache._cache.keys())
        self.assertTrue(any("signin_fail:" in n for n in names))
        self.assertFalse(any("test@example.com" in n for n in names))

    def test_a_successful_sign_in_clears_the_counter(self):
        self._fail(n=2)
        self.assertEqual(self._good().status_code, 200)
        self._fail(n=2)  # would be 4 failures without the reset
        self.assertEqual(self._good().status_code, 200)

    def test_password_reset_clears_the_lock(self):
        self._fail(n=3)
        self.assertEqual(self._good().status_code, 429)
        signin_backoff.clear("test@example.com")
        self.assertEqual(self._good().status_code, 200)

    def test_confirmed_password_reset_clears_the_lock(self):
        self._fail(n=3)
        self.assertEqual(self._good().status_code, 429)
        with mock.patch("origin.views.common.auth_views.send_templated_email") as sent:
            self.client.post(
                "/api/v2/user/password-reset/request/", {"email": "test@example.com"}, format="json"
            )
            token = sent.call_args.kwargs["context"]["reset_url"].split("token=")[1]
        res = self.client.post(
            "/api/v2/user/password-reset/confirm/",
            {"token": token, "new_password": "a-brand-new-passphrase-9"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._good(password="a-brand-new-passphrase-9").status_code, 200)

    def test_fails_closed_when_the_cache_is_unreadable(self):
        with mock.patch.object(throttles, "throttle_cache", side_effect=throttles.CacheDown):
            res = signin_backoff.check("test@example.com")
        self.assertEqual(res.status_code, 429)
        self.assertEqual(res["Retry-After"], "60")

    def test_non_text_email_does_not_crash(self):
        res = self.client.post(URL, {"email": ["a"], "password": "x"}, format="json")
        self.assertIn(res.status_code, (400, 401))

    def test_spray_alert_after_failures_across_many_accounts(self):
        for i in range(3):
            self._fail(f"u{i}@example.com")
        self.assertFalse(SecurityEvent.objects.filter(action=SecurityAction.ALERT_SIGNIN_SPRAY))
        self._fail("u3@example.com")
        alerts = SecurityEvent.objects.filter(action=SecurityAction.ALERT_SIGNIN_SPRAY)
        self.assertEqual(alerts.count(), 1)
        self.assertEqual(alerts[0].metadata["accounts"], 4)
        self.assertNotIn("example.com", str(alerts[0].metadata))
        # mailed through the ops-alert path, once for the service
        self.assertEqual(
            OpsAlert.objects.filter(key="security:alert.signin_spray:signin").count(), 1
        )
        # more accounts or repeat failures on one address do not raise it again
        self._fail("u4@example.com")
        self._fail("u0@example.com", n=2)
        self.assertEqual(
            SecurityEvent.objects.filter(action=SecurityAction.ALERT_SIGNIN_SPRAY).count(), 1
        )

    def test_repeat_failures_on_one_account_are_not_a_spray(self):
        self._fail("u0@example.com", n=10)
        self.assertFalse(SecurityEvent.objects.filter(action=SecurityAction.ALERT_SIGNIN_SPRAY))
