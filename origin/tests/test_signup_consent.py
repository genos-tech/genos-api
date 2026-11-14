"""Signup needs an explicit yes to the Terms and the Privacy Policy, and records it.

Two creation paths enforce it: the email signup view and the first OAuth
sign-in. Everything else that makes a user (tests, the demo, system users)
is untouched.
"""

from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from origin.models.common.user_models import ConnectedAccount
from origin.services import signup_consent
from origin.services.account_export import build_export
from origin.services.oauth.base import OAuthProvider, ProviderProfile, TokenResponse
from origin.views.common import oauth_views

User = get_user_model()

SIGNUP = "/api/v2/user/signup/"


def _body(**extra):
    body = {"username": "newuser", "email": "new@example.com", "password": "newpass123"}
    body.update(extra)
    return body


class EmailSignupConsentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        # The signup throttle counts refusals too and its counters outlive a
        # test; leaving them would starve the signup tests that run after us.
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = patch("origin.views.common.auth_views.send_templated_email")
        self.mock_send = patcher.start()
        self.addCleanup(patcher.stop)

    def _refused(self, res):
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["code"], "terms_not_accepted")
        # No partial account, no verification mail.
        self.assertFalse(User.objects.with_email("new@example.com").exists())
        self.assertEqual(self.mock_send.call_count, 0)

    def test_missing_flag_is_refused(self):
        self._refused(self.client.post(SIGNUP, _body(), format="json"))

    def test_false_flag_is_refused(self):
        self._refused(self.client.post(SIGNUP, _body(accepted_terms=False), format="json"))

    def test_values_that_are_not_an_explicit_yes_are_refused(self):
        for value in (None, "", "false", "no", "0", 0, [], {}, "yes please", 2):
            with self.subTest(value=value):
                self._refused(self.client.post(SIGNUP, _body(accepted_terms=value), format="json"))

    def test_the_refusal_names_the_code_and_creates_nothing_for_a_valid_body(self):
        before = User.objects.count()
        res = self.client.post(SIGNUP, _body(), format="json")
        self.assertEqual(res.json()["code"], signup_consent.TERMS_NOT_ACCEPTED_CODE)
        self.assertEqual(User.objects.count(), before)

    def test_consent_is_checked_before_the_rest_of_the_body(self):
        # An invalid email with no consent is still the consent refusal: the
        # consent is the first thing the person must do.
        res = self.client.post(SIGNUP, _body(email="not-an-email"), format="json")
        self.assertEqual(res.json()["code"], "terms_not_accepted")

    def test_acceptance_is_recorded_with_time_and_server_version(self):
        before = timezone.now()
        res = self.client.post(SIGNUP, _body(accepted_terms=True), format="json")
        self.assertEqual(res.status_code, 201)
        user = User.objects.with_email("new@example.com").get()
        self.assertGreaterEqual(user.terms_accepted_at, before)
        self.assertLessEqual(user.terms_accepted_at, timezone.now())
        self.assertEqual(user.terms_version, signup_consent.TERMS_VERSION)

    def test_form_encoded_true_counts(self):
        res = self.client.post(SIGNUP, _body(accepted_terms="true"))
        self.assertEqual(res.status_code, 201)
        self.assertIsNotNone(User.objects.with_email("new@example.com").get().terms_accepted_at)

    def test_a_client_cannot_choose_the_version_or_the_time(self):
        res = self.client.post(
            SIGNUP,
            _body(
                accepted_terms=True,
                terms_version="1999-fake",
                terms_accepted_at="1999-01-01T00:00:00Z",
            ),
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        user = User.objects.with_email("new@example.com").get()
        self.assertEqual(user.terms_version, signup_consent.TERMS_VERSION)
        self.assertGreater(user.terms_accepted_at.year, 2000)

    def test_an_invite_signup_needs_it_too_and_records_it(self):
        import hashlib

        from origin.models.common.invite_models import TeamInvite
        from origin.models.common.team_models import TeamMaster

        owner = User.objects.create_user(email="o@example.com", username="o", password="x" * 10)
        team = TeamMaster.objects.create(team_name="T", team_email="t@example.com", owner=owner)
        raw = "invite-token-raw-value"
        TeamInvite.objects.create(
            team=team,
            invited_email="new@example.com",
            invited_by=owner,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=1),
        )
        refused = self.client.post(SIGNUP, _body(invite_token=raw), format="json")
        self._refused(refused)
        ok = self.client.post(SIGNUP, _body(invite_token=raw, accepted_terms=True), format="json")
        self.assertEqual(ok.status_code, 201)
        user = User.objects.with_email("new@example.com").get()
        self.assertEqual(user.terms_version, signup_consent.TERMS_VERSION)

    def test_existing_accounts_and_direct_creation_are_untouched(self):
        # Tests, the demo, system users and management commands create users
        # directly: no consent needed, nothing recorded.
        user = User.objects.create_user(email="d@example.com", username="d", password="x" * 10)
        self.assertIsNone(user.terms_accepted_at)
        self.assertIsNone(user.terms_version)

    def test_sign_in_of_an_existing_account_needs_no_consent(self):
        User.objects.create_user(
            email="old@example.com",
            username="old",
            password="oldpass123",
            is_email_verified=True,
        )
        res = self.client.post(
            "/api/v2/user/signin/",
            {"email": "old@example.com", "password": "oldpass123"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)

    def test_the_export_carries_the_record(self):
        self.client.post(SIGNUP, _body(accepted_terms=True), format="json")
        user = User.objects.with_email("new@example.com").get()
        account = build_export(user)["account"]
        self.assertEqual(account["terms_version"], signup_consent.TERMS_VERSION)
        self.assertEqual(account["terms_accepted_at"], user.terms_accepted_at.isoformat())

    def test_a_verification_mail_still_goes_out(self):
        res = self.client.post(SIGNUP, _body(accepted_terms=True), format="json")
        self.assertEqual(res.json()["message"], "verification_email_sent")
        self.assertEqual(self.mock_send.call_count, 1)
        self.assertEqual(len(mail.outbox), 0)  # sent through the patched helper


class _FakeProvider(OAuthProvider):
    name = "google"

    def authorize_url(self, *, state, intent, redirect_uri):
        return f"https://provider.test/auth?state={state}"

    def exchange_code(self, *, code, redirect_uri):
        return TokenResponse(
            access_token="at", refresh_token=None, expires_in_seconds=3600, granted_scopes=[]
        )

    def refresh(self, *, refresh_token):
        raise NotImplementedError

    def fetch_profile(self, *, access_token):
        return ProviderProfile(
            provider_user_id="g-123", email="oauth-new@example.com", display_name="Oauth New"
        )


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID="goog-client-id",
    GOOGLE_OAUTH_CLIENT_SECRET="goog-secret",
    FRONTEND_BASE_URL="https://app.test",
)
class OAuthSignupConsentTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = patch.object(oauth_views, "get_provider", return_value=_FakeProvider())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _initiate(self, **query):
        res = self.client.get("/api/v2/oauth/google/initiate/", {"intent": "login", **query})
        self.assertEqual(res.status_code, 302)
        return parse_qs(urlparse(res["Location"]).query)["state"][0]

    def _callback(self, state):
        return self.client.get("/api/v2/oauth/google/callback/", {"code": "c", "state": state})

    def _exists(self):
        return User.objects.with_email("oauth-new@example.com").exists()

    def test_a_new_account_without_consent_is_refused_and_not_created(self):
        res = self._callback(self._initiate())
        self.assertEqual(res.status_code, 302)
        self.assertIn("error=terms_required", res["Location"])
        self.assertFalse(self._exists())
        self.assertFalse(ConnectedAccount.objects.filter(provider_user_id="g-123").exists())

    def test_a_false_flag_is_the_same_as_no_flag(self):
        for value in ("false", "0", "", "no"):
            with self.subTest(value=value):
                res = self._callback(self._initiate(accepted_terms=value))
                self.assertIn("error=terms_required", res["Location"])
                self.assertFalse(self._exists())

    def test_consent_given_before_the_redirect_is_carried_through_and_recorded(self):
        before = timezone.now()
        res = self._callback(self._initiate(accepted_terms="true"))
        self.assertEqual(res.status_code, 302)
        self.assertIn("/oauth/success#", res["Location"])
        user = User.objects.with_email("oauth-new@example.com").get()
        self.assertGreaterEqual(user.terms_accepted_at, before - timedelta(seconds=1))
        self.assertEqual(user.terms_version, signup_consent.TERMS_VERSION)

    def test_the_version_recorded_is_the_one_shown_when_consent_was_given(self):
        state = self._initiate(accepted_terms="true")
        with patch.object(signup_consent, "TERMS_VERSION", "2099-next"):
            self._callback(state)
        user = User.objects.with_email("oauth-new@example.com").get()
        self.assertEqual(user.terms_version, signup_consent.TERMS_VERSION)

    def test_an_existing_oauth_account_signs_in_without_consent(self):
        self._callback(self._initiate(accepted_terms="true"))
        user = User.objects.with_email("oauth-new@example.com").get()
        stamp = user.terms_accepted_at
        res = self._callback(self._initiate())
        self.assertIn("/oauth/success#", res["Location"])
        user.refresh_from_db()
        self.assertEqual(user.terms_accepted_at, stamp)

    def test_the_flag_cannot_be_smuggled_into_the_callback(self):
        state = self._initiate()
        res = self.client.get(
            "/api/v2/oauth/google/callback/",
            {"code": "c", "state": state, "accepted_terms": "true"},
        )
        self.assertIn("error=terms_required", res["Location"])
        self.assertFalse(self._exists())
