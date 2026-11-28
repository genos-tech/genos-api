"""MIP-2232: the open account endpoints do not say whether an address has an account, and the
three views that had no per-IP limit now have one.

* Signup answers the same 201 for a new and a registered address; the owner gets a different email.
* The reset / resend / signup emails go out AFTER the response, so the time to answer does not
  depend on whether an account exists (a send that blocks must not hold the response).
* verify-email, the OAuth callback and token refresh answer 429 past their per-IP rate.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from origin.services import email as email_service
from origin.services import throttles
from origin.tests.test_base import BaseAPITestCase
from origin.views.common import auth_views, oauth_views

SIGNUP = "/api/v2/user/signup/"
RESET = "/api/v2/user/password-reset/request/"
RESEND = "/api/v2/user/verify-email/resend/"


def _body(email, **extra):
    body = {
        "username": "newperson",
        "email": email,
        "password": "a-long-enough-pass1",
        "accepted_terms": True,
    }
    body.update(extra)
    return body


class _Inline:
    def __init__(self, target=None, **kwargs):
        self.target = target

    def start(self):
        self.target()


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


class TestSignupAnswersTheSameForARegisteredAddress(_Base):
    def test_new_and_registered_addresses_get_the_same_answer(self):
        new = self.client.post(SIGNUP, _body("fresh@example.com"), format="json")
        known = self.client.post(SIGNUP, _body("TEST@example.com"), format="json")
        self.assertEqual(new.status_code, 201)
        self.assertEqual(known.status_code, 201)
        self.assertEqual(new.json()["message"], known.json()["message"])
        self.assertEqual(set(new.json()), set(known.json()))
        self.assertEqual(known.json()["email"], "TEST@example.com")

    def test_the_registered_owner_gets_an_existing_account_email_not_a_second_account(self):
        self.user.is_email_verified = True
        self.user.save(update_fields=["is_email_verified"])
        from origin.models.common.user_models import CustomUser

        before = CustomUser.objects.count()
        mail.outbox.clear()
        self.client.post(SIGNUP, _body("test@example.com"), format="json")
        self.assertEqual(CustomUser.objects.count(), before)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["test@example.com"])
        self.assertIn("already", mail.outbox[0].subject.lower())
        # No verification link and no token in it.
        self.assertNotIn("verify-email", mail.outbox[0].body)

    def test_a_new_address_still_gets_the_verification_email(self):
        mail.outbox.clear()
        self.client.post(SIGNUP, _body("fresh@example.com"), format="json")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("verify-email?token=", mail.outbox[0].body)

    def test_a_bad_field_is_refused_the_same_for_a_registered_address(self):
        bad_password = {"password": "a"}
        a = self.client.post(SIGNUP, _body("fresh@example.com", **bad_password), format="json")
        b = self.client.post(SIGNUP, _body("test@example.com", **bad_password), format="json")
        self.assertEqual((a.status_code, b.status_code), (400, 400))
        self.assertEqual(a.json().keys(), b.json().keys())

    def test_a_bad_invite_token_is_refused_the_same_for_a_registered_address(self):
        a = self.client.post(SIGNUP, _body("fresh@example.com", invite_token="nope"), format="json")
        b = self.client.post(SIGNUP, _body("test@example.com", invite_token="nope"), format="json")
        self.assertEqual((a.status_code, b.status_code), (400, 400))

    def test_the_echoed_address_is_normalised_the_same_way(self):
        a = self.client.post(SIGNUP, _body("x@EXAMPLE.com"), format="json").json()["email"]
        b = self.client.post(SIGNUP, _body("TEST@EXAMPLE.com"), format="json").json()["email"]
        self.assertEqual(a, "x@example.com")
        self.assertEqual(b, "TEST@example.com")

    def test_an_unverified_owner_gets_a_fresh_verification_link_instead(self):
        self.user.is_email_verified = False
        self.user.save(update_fields=["is_email_verified"])
        mail.outbox.clear()
        self.client.post(SIGNUP, _body("test@example.com"), format="json")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("verify-email?token=", mail.outbox[0].body)

    def test_a_refusal_for_terms_is_still_first(self):
        res = self.client.post(
            SIGNUP, _body("test@example.com", accepted_terms=False), format="json"
        )
        self.assertEqual(res.status_code, 400)


@override_settings(EMAIL_SEND_AFTER_RESPONSE=True)
class TestTheEmailDoesNotHoldTheResponse(_Base):
    """The send is handed to a thread that the test captures instead of starting. If a view sent
    inside the request, the (blocking) send would already have run when the response came back.
    Running the captured thread afterwards proves the mail is still sent."""

    def _post(self, url, body):
        started = []

        class _Captured:
            def __init__(self, target=None, **kwargs):
                self.target = target

            def start(self):
                started.append(self.target)

        sent = []

        def blocking_send(**kwargs):
            sent.append(kwargs)  # stands for a slow SMTP/HTTP call

        with (
            mock.patch.object(auth_views, "send_templated_email", side_effect=blocking_send),
            mock.patch.object(email_service, "threading", SimpleNamespace(Thread=_Captured)),
        ):
            res = self.client.post(url, body, format="json")
            sent_during_request = len(sent)
            for target in started:  # the thread runs later, with the same patch
                target()
        return res, sent_during_request, started, sent

    def _assert_after_response(self, url, body, status_code):
        res, during, started, sent = self._post(url, body)
        self.assertEqual(res.status_code, status_code)
        self.assertEqual(during, 0, "the response waited for the email")
        self.assertEqual(len(started), 1)
        started[0]()  # the thread runs later
        self.assertEqual(len(sent), 1)

    def test_reset_for_an_existing_account_sends_after_the_response(self):
        self._assert_after_response(RESET, {"email": "test@example.com"}, 200)

    def test_reset_for_an_unknown_address_sends_nothing_and_answers_the_same(self):
        res, during, started, sent = self._post(RESET, {"email": "nobody@example.com"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual((during, len(started), len(sent)), (0, 0, 0))

    def test_signup_for_a_registered_address_sends_after_the_response(self):
        self._assert_after_response(SIGNUP, _body("test@example.com"), 201)

    def test_signup_for_a_new_address_sends_after_the_response_too(self):
        self._assert_after_response(SIGNUP, _body("fresh@example.com"), 201)

    def test_resend_for_an_unverified_account_sends_after_the_response(self):
        self.user.is_email_verified = False
        self.user.save(update_fields=["is_email_verified"])
        self._assert_after_response(RESEND, {"email": "test@example.com"}, 200)

    def test_a_failing_send_is_reported_and_never_raises(self):
        errors = []

        def boom():
            raise RuntimeError("boom")

        with mock.patch.object(email_service, "threading", SimpleNamespace(Thread=_Inline)):
            email_service.run_after_response(boom, on_error=errors.append)
        self.assertEqual(len(errors), 1)


class TestThrottlesOnTheOpenViews(_Base):
    def setUp(self):
        super().setUp()
        # TokenRefreshThrottle fails open: a cache error in an earlier test leaves a 5 s breaker
        # during which it allows everything (MIP-2534). The window itself slides, so the clock
        # needs no pinning.
        throttles._down_until = 0.0

    def _codes(self, scope_cls, call, n=2):
        with mock.patch.object(scope_cls, "rate", "1/min"):
            return [call().status_code for _ in range(n)]

    def test_verify_email_is_throttled(self):
        url = reverse("verify_email")
        codes = self._codes(
            auth_views.VerifyEmailThrottle, lambda: self.client.get(url, {"token": "nope"})
        )
        self.assertEqual(codes, [400, 429])

    def test_the_oauth_callback_is_throttled(self):
        codes = self._codes(
            auth_views.OAuthCallbackThrottle,
            lambda: self.client.get("/api/v2/oauth/not-a-provider/callback/"),
        )
        self.assertEqual(codes[-1], 429)
        self.assertNotEqual(codes[0], 429)
        self.assertIs(
            oauth_views.OAuthCallbackView.throttle_classes[0], auth_views.OAuthCallbackThrottle
        )

    def test_token_refresh_is_throttled(self):
        url = reverse("token_refresh")
        codes = self._codes(auth_views.TokenRefreshThrottle, lambda: self.client.post(url))
        self.assertEqual(codes, [403, 429])

    def test_refresh_fails_open_but_the_other_two_fail_closed(self):
        self.assertFalse(auth_views.TokenRefreshThrottle.fail_closed)
        self.assertTrue(auth_views.VerifyEmailThrottle.fail_closed)
        self.assertTrue(auth_views.OAuthCallbackThrottle.fail_closed)
