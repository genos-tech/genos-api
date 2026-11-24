"""MIP-2238: signup runs the password validators; a breached-password check sits behind a switch."""

import hashlib
from unittest import mock

import requests
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common import user_models
from origin.services import password_policy
from origin.services.password_policy import PwnedPasswordValidator

SIGNUP = "/api/v2/user/signup/"
GOOD = "t7#Quartz-meadow-48"


def _sha1(pw):
    return hashlib.sha1(pw.encode()).hexdigest().upper()


def _reply(pw, count="12", extra=""):
    suffix = _sha1(pw)[5:]
    return mock.Mock(
        status_code=200, text=f"{extra}{suffix}:{count}\r\n0018A45C4D1DEF81644B54AB7F969B88D65:1"
    )


class SignupValidatorsTests(TestCase):
    def _post(self, **over):
        body = {
            "username": "newperson",
            "email": "newperson@example.com",
            "password": GOOD,
            "accepted_terms": True,
        }
        body.update(over)
        return APIClient().post(SIGNUP, body, format="json")

    def test_common_password_refused(self):
        r = self._post(password="password123")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["code"], "password_rejected")
        self.assertIn("password_too_common", r.json()["password_errors"])
        self.assertFalse(user_models.CustomUser.objects.filter(username="newperson").exists())

    def test_numeric_only_refused(self):
        r = self._post(password="48271956304")
        self.assertEqual(r.status_code, 400)
        self.assertIn("password_entirely_numeric", r.json()["password_errors"])

    def test_similar_to_username_refused(self):
        r = self._post(username="quartzmeadow", password="quartzmeadow1")
        self.assertEqual(r.status_code, 400)
        self.assertIn("password_too_similar", r.json()["password_errors"])

    def test_too_short_has_code(self):
        r = self._post(password="a1!b")
        self.assertEqual(r.status_code, 400)
        self.assertIn("password_too_short", r.json()["password_errors"])

    def test_good_password_accepted(self):
        with mock.patch("origin.views.common.auth_views._send_verification_email"):
            r = self._post()
        self.assertEqual(r.status_code, 201, r.content)

    def test_other_400_keeps_old_shape(self):
        r = self._post(email="not-an-email")
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("code", r.json())


@override_settings(PASSWORD_BREACH_CHECK=True)
class PwnedValidatorTests(SimpleTestCase):
    def test_breached_password_refused_and_only_prefix_sent(self):
        with mock.patch("origin.services.password_policy.requests.get") as get:
            get.return_value = _reply("hunter2-Correct")
            with self.assertRaises(ValidationError) as ctx:
                PwnedPasswordValidator().validate("hunter2-Correct")
        self.assertEqual(ctx.exception.code, "password_breached")
        url = get.call_args.args[0]
        digest = _sha1("hunter2-Correct")
        self.assertTrue(url.endswith("/" + digest[:5]))
        self.assertNotIn(digest[5:], url)
        self.assertNotIn("hunter2", url)
        self.assertEqual(get.call_args.kwargs["timeout"], 2.0)

    def test_not_in_reply_is_accepted(self):
        with mock.patch("origin.services.password_policy.requests.get") as get:
            get.return_value = mock.Mock(
                status_code=200, text="0018A45C4D1DEF81644B54AB7F969B88D65:3"
            )
            PwnedPasswordValidator().validate(GOOD)

    def test_padding_row_with_zero_count_is_not_a_match(self):
        with mock.patch("origin.services.password_policy.requests.get") as get:
            get.return_value = _reply(GOOD, count="0")
            PwnedPasswordValidator().validate(GOOD)

    def test_fails_open_on_timeout(self):
        with mock.patch(
            "origin.services.password_policy.requests.get", side_effect=requests.Timeout()
        ):
            with self.assertLogs("origin.services.password_policy", "WARNING") as logs:
                PwnedPasswordValidator().validate("anything-at-all-1")
        self.assertIn("Timeout", logs.output[0])
        self.assertNotIn("anything-at-all-1", logs.output[0])

    def test_fails_open_on_error_status(self):
        with mock.patch("origin.services.password_policy.requests.get") as get:
            get.return_value = mock.Mock(status_code=503, text="")
            with self.assertLogs("origin.services.password_policy", "WARNING"):
                PwnedPasswordValidator().validate("anything-at-all-1")

    @override_settings(PASSWORD_BREACH_CHECK=False)
    def test_off_by_default_makes_no_call(self):
        with mock.patch("origin.services.password_policy.requests.get") as get:
            PwnedPasswordValidator().validate("password123")
        get.assert_not_called()

    def test_setting_defaults_off(self):
        from apis import settings as s

        self.assertFalse(s.PASSWORD_BREACH_CHECK)


@override_settings(PASSWORD_BREACH_CHECK=True)
class BreachSwitchEndToEndTests(TestCase):
    def test_signup_refuses_breached(self):
        with mock.patch("origin.services.password_policy.requests.get") as get:
            get.return_value = _reply(GOOD)
            r = APIClient().post(
                SIGNUP,
                {
                    "username": "newperson",
                    "email": "newperson@example.com",
                    "password": GOOD,
                    "accepted_terms": True,
                },
                format="json",
            )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["password_errors"], ["password_breached"])

    def test_reset_refuses_breached_with_code(self):
        import secrets

        user = user_models.CustomUser.objects.create_user(
            email="resetter@example.com", username="resetter", password=GOOD
        )
        raw = secrets.token_urlsafe(32)
        from datetime import timedelta

        from django.utils import timezone

        user.password_reset_token_hash = hashlib.sha256(raw.encode()).hexdigest()
        user.password_reset_token_expires_at = timezone.now() + timedelta(hours=1)
        user.save()
        new_pw = "Zx9-orbit-lantern-71"
        with mock.patch("origin.services.password_policy.requests.get") as get:
            get.return_value = _reply(new_pw)
            r = APIClient().post(
                "/api/v2/user/password-reset/confirm/",
                {"token": raw, "new_password": new_pw},
                format="json",
            )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertEqual(r.json()["code"], "password_rejected")
        self.assertEqual(r.json()["password_errors"], ["password_breached"])
        self.assertTrue(password_policy.public_code("min_length") == "password_too_short")
