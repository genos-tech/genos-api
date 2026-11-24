"""OAuth sign-in (login intent): the state is bound to the starting browser (MIP-2188).

Login CSRF (RFC 6749 section 10.12): an attacker starts a Google sign-in for their own account,
approves it, stops at the redirect and sends the callback link to a victim. Without a binding the
victim's browser is signed in to the ATTACKER's account. The HttpOnly bind cookie set on the
top-level initiate redirect closes it (reusing the helpers of MIP-2170), and the post-login `next`
goes through the same-site check.
"""

from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.services.oauth.base import OAuthProvider, ProviderProfile, TokenResponse
from origin.views.common import oauth_views

User = get_user_model()

SETTINGS = dict(
    GOOGLE_OAUTH_CLIENT_ID="goog-id",
    GOOGLE_OAUTH_CLIENT_SECRET="goog-secret",
    GITHUB_OAUTH_CLIENT_ID="gh-id",
    GITHUB_OAUTH_CLIENT_SECRET="gh-secret",
    GITHUB_APP_ENABLED=False,
    FRONTEND_BASE_URL="https://app.example.test",
)


class _FakeProvider(OAuthProvider):
    name = "fake"

    def __init__(self):
        self.exchanged = []

    def authorize_url(self, *, state, intent, redirect_uri):
        return f"https://provider.test/auth?state={state}"

    def exchange_code(self, *, code, redirect_uri):
        self.exchanged.append(code)
        return TokenResponse(
            access_token="at", refresh_token="rt", expires_in_seconds=3600, granted_scopes=["s"]
        )

    def refresh(self, *, refresh_token):
        raise NotImplementedError

    def fetch_profile(self, *, access_token):
        return ProviderProfile(
            provider_user_id="p-attacker",
            email="attacker@example.com",
            display_name="Attacker",
            email_verified=True,
        )


@override_settings(**SETTINGS)
class LoginStateBindingTests(TestCase):
    PROVIDERS = ("google", "github")

    def setUp(self):
        self.fake = _FakeProvider()
        patcher = patch.object(oauth_views, "get_provider", return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _initiate(self, provider, **query):
        res = APIClient().get(f"/api/v2/oauth/{provider}/initiate/", {"intent": "login", **query})
        self.assertEqual(res.status_code, 302)
        state = parse_qs(urlparse(res["Location"]).query)["state"][0]
        return res, state

    def _callback(self, provider, state, cookie=None):
        """The provider sends a browser back: an unauthenticated top-level GET."""
        browser = APIClient()
        if cookie is not None:
            browser.cookies[f"{provider}_bind"] = cookie
        return browser.get(f"/api/v2/oauth/{provider}/callback/", {"code": "c", "state": state})

    def _users(self):
        return User.objects.with_email("attacker@example.com").count()

    def test_initiate_sets_an_httponly_cookie_scoped_to_the_flow(self):
        for provider in self.PROVIDERS:
            with self.subTest(provider=provider):
                res, state = self._initiate(provider)
                morsel = res.cookies[f"{provider}_bind"]
                self.assertTrue(morsel["httponly"])
                self.assertEqual(morsel["path"], f"/api/v2/oauth/{provider}/")
                self.assertEqual(morsel["samesite"], "Lax")
                # The cookie holds the secret; the state holds only its hash.
                self.assertNotIn(morsel.value, state)
                self.assertNotIn(morsel.value, res["Location"])

    def test_a_callback_link_replayed_in_another_browser_signs_nobody_in(self):
        for provider in self.PROVIDERS:
            with self.subTest(provider=provider):
                _, state = self._initiate(provider)
                # The victim's browser never received the attacker's cookie.
                resp = self._callback(provider, state, cookie=None)
                self.assertIn("error=invalid_state", resp["Location"])
                self.assertNotIn("access=", resp["Location"])
                self.assertNotIn("refresh_token", resp.cookies)
                self.assertEqual(self.fake.exchanged, [])
                self.assertEqual(self._users(), 0)

    def test_a_wrong_cookie_signs_nobody_in(self):
        for provider in self.PROVIDERS:
            with self.subTest(provider=provider):
                _, state = self._initiate(provider)
                resp = self._callback(provider, state, cookie="some-other-value")
                self.assertIn("error=invalid_state", resp["Location"])
                self.assertEqual(self.fake.exchanged, [])
                self.assertEqual(self._users(), 0)

    def test_a_login_state_with_no_binding_at_all_is_refused(self):
        # A state minted before this change (or forged by a bug) carries no `bind`.
        state = oauth_views._sign_state(intent="login", user_id=None, next_path="/jointeam")
        resp = self._callback("google", state, cookie="anything")
        self.assertIn("error=invalid_state", resp["Location"])
        self.assertEqual(self.fake.exchanged, [])

    def test_the_same_browser_signs_in_and_the_cookie_is_dropped(self):
        for provider in self.PROVIDERS:
            with self.subTest(provider=provider):
                User.objects.with_email("attacker@example.com").delete()
                res, state = self._initiate(
                    provider, next="/workspace/home?x=1", accepted_terms="true"
                )
                cookie = res.cookies[f"{provider}_bind"].value
                resp = self._callback(provider, state, cookie=cookie)
                self.assertEqual(resp.status_code, 302)
                location = resp["Location"]
                self.assertTrue(location.startswith("https://app.example.test/oauth/success#"))
                fragment = parse_qs(urlparse(location).fragment)
                self.assertEqual(fragment["next"], ["/workspace/home?x=1"])
                # No credential in a URL anyone could forge (MIP-2219): the session is the
                # refresh cookie, which the bounce page exchanges for an access token.
                self.assertEqual(set(fragment), {"next"})
                self.assertNotIn("access", location)
                self.assertTrue(resp.cookies["refresh"].value)
                self.assertTrue(resp.cookies["refresh"]["httponly"])
                self.assertEqual(self._users(), 1)
                # One use: the callback expires the cookie.
                self.assertEqual(resp.cookies[f"{provider}_bind"]["max-age"], 0)

    def test_a_refused_callback_also_drops_the_cookie(self):
        _, state = self._initiate("google")
        resp = self._callback("google", state, cookie="wrong")
        self.assertEqual(resp.cookies["google_bind"]["max-age"], 0)

    def test_the_cookie_of_one_provider_does_not_open_the_other(self):
        res, _ = self._initiate("google")
        _, github_state = self._initiate("github")
        resp = self._callback("github", github_state, cookie=res.cookies["google_bind"].value)
        self.assertIn("error=invalid_state", resp["Location"])
        self.assertEqual(self._users(), 0)

    def test_a_hostile_next_is_replaced_at_initiate(self):
        for hostile in ("//evil.example", "/\\evil.example", "https://evil.example", "evil"):
            with self.subTest(next=hostile):
                User.objects.with_email("attacker@example.com").delete()
                res, state = self._initiate("google", next=hostile, accepted_terms="true")
                self.assertEqual(oauth_views._verify_state(state)["next"], "/jointeam")
                resp = self._callback("google", state, cookie=res.cookies["google_bind"].value)
                fragment = parse_qs(urlparse(resp["Location"]).fragment)
                self.assertEqual(fragment["next"], ["/jointeam"])

    def test_a_hostile_next_inside_a_signed_state_is_replaced_at_the_callback(self):
        # Defence in depth: the callback does not trust what the state says.
        for hostile in ("//evil.example", "/\\evil.example", "https://evil.example"):
            with self.subTest(next=hostile):
                User.objects.with_email("attacker@example.com").delete()
                state = oauth_views._sign_state(
                    intent="login",
                    user_id=None,
                    next_path=hostile,
                    terms_accepted=True,
                    bind=oauth_views._bind_hash("b"),
                )
                resp = self._callback("google", state, cookie="b")
                fragment = parse_qs(urlparse(resp["Location"]).fragment)
                self.assertEqual(fragment["next"], ["/jointeam"])

    def test_the_success_url_never_carries_a_protocol_relative_next(self):
        for hostile in ("//evil.example", "/\\evil.example", "https://evil.example", ""):
            with self.subTest(next=hostile):
                url = oauth_views._frontend_success_url(next_path=hostile)
                self.assertIn(f"next={quote('/jointeam', safe='')}", url)
        url = oauth_views._frontend_success_url(next_path="/workspace/x")
        self.assertIn("next=%2Fworkspace%2Fx", url)
