"""A provider's email address is only an identity when the provider says it is verified (MIP-1766).

Google's userinfo carries an `email_verified` claim and the sign-in code used to
ignore it, creating the account with `is_email_verified=True` whatever the claim
said. These tests pin the claim being read (and a missing claim counting as not
verified), and that an unverified address neither creates an account nor works
as an identity for a sign-in.
"""

from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.user_models import ConnectedAccount
from origin.services import signup_consent
from origin.services.oauth.base import ProviderProfile, TokenResponse
from origin.services.oauth.github import GitHubOAuthProvider
from origin.services.oauth.google import GoogleOAuthProvider
from origin.views.common.oauth_views import OAuthCallbackView

User = get_user_model()

TOKENS = TokenResponse(
    access_token="tok",
    refresh_token=None,
    expires_in_seconds=3600,
    granted_scopes=["openid", "email", "profile"],
)


def _response(payload):
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


class GoogleProfileTests(TestCase):
    def _profile(self, **claims):
        payload = {"sub": "g-1", "email": "p@example.com", "name": "P", **claims}
        with patch("origin.services.oauth.google.requests.get", return_value=_response(payload)):
            return GoogleOAuthProvider().fetch_profile(access_token="at")

    def test_a_verified_address_is_verified(self):
        self.assertTrue(self._profile(email_verified=True).email_verified)

    def test_an_unverified_address_is_not_verified(self):
        self.assertFalse(self._profile(email_verified=False).email_verified)

    def test_a_missing_claim_is_not_verified(self):
        self.assertFalse(self._profile().email_verified)

    def test_a_null_or_odd_claim_is_not_verified(self):
        for value in (None, "", "false", "no", 0, 1, "yes"):
            with self.subTest(value=value):
                self.assertFalse(self._profile(email_verified=value).email_verified)

    def test_the_string_form_of_true_is_verified(self):
        self.assertTrue(self._profile(email_verified="true").email_verified)


class GitHubProfileTests(TestCase):
    def _profile(self, user, emails=None):
        def fake_get(url, **kwargs):
            if url.endswith("/user/emails"):
                return _response(emails or [])
            return _response(user)

        with patch("origin.services.oauth.github.requests.get", side_effect=fake_get):
            return GitHubOAuthProvider().fetch_profile(access_token="at")

    def test_the_public_email_is_verified(self):
        profile = self._profile({"id": 7, "login": "x", "email": "pub@example.com"})
        self.assertEqual(profile.email, "pub@example.com")
        self.assertTrue(profile.email_verified)

    def test_the_fallback_takes_only_the_primary_verified_address(self):
        profile = self._profile(
            {"id": 7, "login": "x", "email": None},
            emails=[
                {"email": "a@example.com", "primary": False, "verified": True},
                {"email": "b@example.com", "primary": True, "verified": False},
            ],
        )
        self.assertIsNone(profile.email)

        profile = self._profile(
            {"id": 7, "login": "x", "email": None},
            emails=[{"email": "c@example.com", "primary": True, "verified": True}],
        )
        self.assertEqual(profile.email, "c@example.com")
        self.assertTrue(profile.email_verified)


@override_settings(FRONTEND_BASE_URL="https://app.test")
class LoginWithUnverifiedEmailTests(TestCase):
    def _login(self, sub, email, *, verified, terms=True):
        consent = (
            {
                "terms_accepted_at": timezone.now().isoformat(),
                "terms_version": signup_consent.TERMS_VERSION,
            }
            if terms
            else {}
        )
        return OAuthCallbackView()._handle_login(
            **consent,
            provider_name="google",
            profile=ProviderProfile(
                provider_user_id=sub,
                email=email,
                display_name="G",
                email_verified=verified,
            ),
            token_response=TOKENS,
            next_path="/workspace",
        )

    def _failed_events(self):
        return SecurityEvent.objects.filter(action=SecurityAction.SIGNIN_FAILED)

    def test_a_verified_new_account_is_created_verified(self):
        resp = self._login("g-ok", "ok@example.com", verified=True)
        self.assertNotIn("error=", resp.url)
        user = User.objects.with_email("ok@example.com").get()
        self.assertTrue(user.is_email_verified)

    def test_an_unverified_address_creates_no_account(self):
        resp = self._login("g-bad", "victim@example.com", verified=False)
        self.assertIn("error=email_not_verified", resp.url)
        self.assertFalse(User.objects.with_email("victim@example.com").exists())
        self.assertFalse(ConnectedAccount.objects.filter(provider_user_id="g-bad").exists())
        self.assertEqual(
            self._failed_events().filter(metadata__reason="email_not_verified").count(), 1
        )

    def test_an_unverified_address_does_not_reveal_whether_it_is_taken(self):
        """The refusal must read the same whether or not an account holds the address."""
        User.objects.create_user(username="v", email="victim@example.com", password="pw123456")
        resp = self._login("g-bad", "victim@example.com", verified=False)
        self.assertIn("error=email_not_verified", resp.url)
        self.assertNotIn("email_in_use", resp.url)
        self.assertEqual(User.objects.with_email("victim@example.com").count(), 1)
        self.assertFalse(ConnectedAccount.objects.filter(provider_user_id="g-bad").exists())

    def test_an_unverified_address_is_refused_before_consent_is_asked(self):
        resp = self._login("g-bad", "x@example.com", verified=False, terms=False)
        self.assertIn("error=email_not_verified", resp.url)

    def test_a_provider_that_gave_no_address_is_not_refused_for_it(self):
        """No address means nothing to trust; the placeholder-address account is unchanged."""
        resp = self._login("g-none", None, verified=False)
        self.assertNotIn("error=", resp.url)
        self.assertTrue(User.objects.with_email("google_g-none@noemail.local").exists())

    @patch("origin.views.common.oauth_views.OAuthCallbackView._issue_jwt")
    def test_an_unverified_address_is_not_an_identity_for_a_calendar_only_row(self, mock_jwt):
        """The 'identity that is the account's own address' exception needs a verified one."""
        owner = User.objects.create_user(username="o", email="own@example.com", password="pw123456")
        ConnectedAccount.objects.create(
            user=owner,
            provider="google",
            provider_user_id="cal-sub",
            access_token_encrypted="placeholder",
        )
        resp = self._login("cal-sub", "own@example.com", verified=False)
        self.assertIn("not_a_login_account", resp.url)
        mock_jwt.assert_not_called()

    @patch(
        "origin.views.common.oauth_views.OAuthCallbackView._issue_jwt",
        return_value=("a", "r"),
    )
    def test_a_verified_address_still_is_an_identity_for_a_calendar_only_row(self, _jwt):
        owner = User.objects.create_user(username="o", email="own@example.com", password="pw123456")
        ConnectedAccount.objects.create(
            user=owner,
            provider="google",
            provider_user_id="cal-sub",
            access_token_encrypted="placeholder",
        )
        resp = self._login("cal-sub", "own@example.com", verified=True)
        self.assertNotIn("error=", resp.url)

    @patch(
        "origin.views.common.oauth_views.OAuthCallbackView._issue_jwt",
        return_value=("a", "r"),
    )
    def test_the_login_identity_signs_in_whatever_the_claim_says(self, _jwt):
        """Sign-in by the stored provider id is unchanged; the claim only governs the address."""
        resp = self._login("g-first", "keep@example.com", verified=True)
        self.assertNotIn("error=", resp.url)
        resp = self._login("g-first", "keep@example.com", verified=False)
        self.assertNotIn("error=", resp.url)
