"""MIP-996: a body that is not a JSON object is a 400 on sign-in, not a 500.

`POST /user/signin/` reads `email` and `password` ahead of the serializer, for its
email-verification gate. A body that is not an object reads as `{}` there (no gate), and the
serializer, which still gets the body itself, refuses it in its own words: the answer an
empty `[]`, `0` or `""` body already got. Nothing may come out of a refused body: no token,
no refresh cookie, no audit row, no `last_login`.

A number is a different problem. The serializer reads `password: 12345678` as the text
"12345678" and signs in with it, but `check_password` raises on a number, which the gate
called for an unverified account (a 500). Skipping the gate for a number instead would have
let it sign in past `email_not_verified`, so the gate reads a number as the same text the
serializer does.

The sign-in throttles run before the view, so a refused body counts against them exactly as
the 500 did and as a wrong password does; that is pinned too.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.services import throttles

from .security_clock import MidMinute
from .test_base import BaseAPITestCase

User = get_user_model()

# Empty-ish: they read as "no body" and were already an answer.
EMPTY_ISH = ("null", "[]", "0", '""', "false", "{}", "")

SIGNIN = "/api/v2/user/signin/"

PASSWORD = "12345678"
WRONG_TYPE = "Invalid data. Expected a dictionary, but got {}."


class SignInBodyShapeTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = APIClient(raise_request_exception=False)
        # The base test user is unverified; this one signs in.
        self.user.set_password(PASSWORD)
        self.user.is_email_verified = True
        self.user.save(update_fields=["password", "is_email_verified"])
        self.unverified = User.objects.create_user(
            username="unverified",
            email="unverified@example.com",
            password=PASSWORD,
            is_email_verified=False,
        )

    def send(self, raw):
        # The sign-in throttles count in the cache; this test is not about them.
        cache.clear()
        return self.client.generic("POST", SIGNIN, raw, content_type="application/json")

    def state(self):
        return {
            "events": list(SecurityEvent.objects.order_by("pk").values_list("pk", flat=True)),
            "tokens": OutstandingToken.objects.count(),
            "last_login": list(User.objects.order_by("pk").values_list("last_login", flat=True)),
        }

    def assertGotNothing(self, resp, before, msg):
        """No session of any kind came out of this request."""
        self.assertNotIn("refresh", resp.cookies, msg)
        body = resp.json()
        for key in ("access", "refresh", "user_id"):
            self.assertNotIn(key, body, msg)
        self.assertEqual(self.state(), before, msg)

    def test_a_body_that_is_not_an_object_is_the_serializers_400_and_issues_nothing(self):
        before = self.state()
        for raw, kind in (
            ("[1]", "list"),
            ('"abc"', "str"),
            ("5", "int"),
            ("true", "bool"),
            ("1.5", "float"),
            ('[{"email": "test@example.com", "password": "12345678"}]', "list"),
        ):
            resp = self.send(raw)
            self.assertEqual(resp.status_code, 400, raw)
            self.assertEqual(resp.json(), {"non_field_errors": [WRONG_TYPE.format(kind)]}, raw)
            self.assertGotNothing(resp, before, raw)

    def test_an_empty_body_keeps_the_answer_it_always_had(self):
        # The answers on main, which the serializer gives and this change does not touch.
        required = {
            "email": ["This field is required."],
            "password": ["This field is required."],
        }
        answers = {
            "null": {"non_field_errors": ["No data provided"]},
            "[]": {"non_field_errors": [WRONG_TYPE.format("list")]},
            "0": {"non_field_errors": [WRONG_TYPE.format("int")]},
            '""': {"non_field_errors": [WRONG_TYPE.format("str")]},
            "false": {"non_field_errors": [WRONG_TYPE.format("bool")]},
            "{}": required,
            "": required,
        }
        self.assertEqual(set(answers), set(EMPTY_ISH))
        for raw, answer in answers.items():
            resp = self.send(raw)
            self.assertEqual((resp.status_code, resp.json()), (400, answer), repr(raw))

    def test_a_valid_sign_in_still_answers_what_it_did(self):
        resp = self.send(f'{{"email": "test@example.com", "password": "{PASSWORD}"}}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["email"], "test@example.com")
        self.assertIn("refresh", resp.cookies)
        self.assertTrue(
            SecurityEvent.objects.filter(action=SecurityAction.SIGNIN_SUCCEEDED).exists()
        )

    def test_the_gate_still_refuses_an_unverified_account(self):
        resp = self.send(f'{{"email": "unverified@example.com", "password": "{PASSWORD}"}}')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["detail"], "email_not_verified")

    def test_a_number_is_checked_as_the_text_the_serializer_will_read_it_as(self):
        # `password: 12345678` signs in as "12345678", so for an unverified account it has to
        # meet the same gate the text does. `check_password` raises on a number (a 500 until
        # now), and skipping the gate for one would hand it a session past the gate. The gate
        # records the refused sign-in, as it does for the text: one row each, no session.
        for raw in (
            f'{{"email": "unverified@example.com", "password": {PASSWORD}}}',
            f'{{"email": "unverified@example.com", "password": "{PASSWORD}"}}',
        ):
            before = self.state()
            resp = self.send(raw)
            self.assertEqual(
                (resp.status_code, resp.json()["detail"]), (403, "email_not_verified"), raw
            )
            self.assertNotIn("refresh", resp.cookies, raw)
            after = self.state()
            self.assertEqual(len(after["events"]), len(before["events"]) + 1, raw)
            self.assertEqual((after["tokens"], after["last_login"]), (0, before["last_login"]))
            self.assertEqual(
                SecurityEvent.objects.latest("pk").action, SecurityAction.SIGNIN_FAILED, raw
            )

    def test_a_number_still_signs_a_verified_account_in_as_it_did(self):
        resp = self.send(f'{{"email": "test@example.com", "password": {PASSWORD}}}')
        self.assertEqual(resp.status_code, 200)
        self.assertIn("access", resp.json())

    def test_a_password_that_is_not_text_or_a_number_is_the_serializers_400(self):
        for value in ("true", "[1]", '{"a": 1}'):
            for email in ("unverified@example.com", "test@example.com", "nobody@example.com"):
                before = self.state()
                resp = self.send(f'{{"email": "{email}", "password": {value}}}')
                self.assertEqual(resp.status_code, 400, (email, value))
                self.assertEqual(resp.json(), {"password": ["Not a valid string."]})
                self.assertGotNothing(resp, before, (email, value))

    def test_a_wrong_number_is_a_wrong_password_and_is_recorded_as_one(self):
        before = SecurityEvent.objects.filter(action=SecurityAction.SIGNIN_FAILED).count()
        resp = self.send('{"email": "unverified@example.com", "password": 5}')
        self.assertEqual(resp.status_code, 401)
        after = SecurityEvent.objects.filter(action=SecurityAction.SIGNIN_FAILED).count()
        self.assertEqual(after, before + 1)

    def test_an_email_that_is_not_text_is_not_a_500(self):
        for value in ("5", "true", "[1]", '{"a": 1}'):
            resp = self.send(f'{{"email": {value}, "password": "{PASSWORD}"}}')
            self.assertIn(resp.status_code, (400, 401), value)


class SignInThrottleTests(MidMinute, BaseAPITestCase):
    """The sign-in throttles run before the view, on every request whatever its body. A
    refused body is counted, exactly as the 500 it replaces was and as a wrong password is."""

    clock_modules = (throttles,)

    def test_a_refused_body_counts_against_the_burst_limit(self):
        cache.clear()
        self.addCleanup(cache.clear)
        client = APIClient(raise_request_exception=False)
        codes = [
            client.generic("POST", SIGNIN, "[1]", content_type="application/json").status_code
            for _ in range(21)
        ]
        self.assertEqual(codes, [400] * 20 + [429])
