"""Google and GitHub (OAuth App) connect: the state is bound to the starting browser (MIP-2170).

The signed state alone is a bearer ticket. An attacker who starts a connect can hand the provider
URL to a victim; without a binding the callback would store the VICTIM's provider token on the
ATTACKER's account. The same HttpOnly-cookie mechanism as the GitHub App flow (MIP-2087) closes it,
and the post-connect `next` goes through the same-site check.
"""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount
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
            provider_user_id="p-123",
            email="victim@example.com",
            display_name="Victim",
            email_verified=True,
        )


@override_settings(**SETTINGS)
class ConnectStateBindingTests(TestCase):
    PROVIDERS = ("google", "github")

    def setUp(self):
        self.attacker = User.objects.create_user(
            username="attacker", email="attacker@example.com", password="pw123456"
        )
        self.api = APIClient()
        self.api.force_authenticate(user=self.attacker)
        self.fake = _FakeProvider()
        patcher = patch.object(oauth_views, "get_provider", return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _initiate(self, provider, **body):
        res = self.api.post(
            f"/api/v2/oauth/{provider}/initiate/", {"intent": "connect", **body}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        state = parse_qs(urlparse(res.data["url"]).query)["state"][0]
        return res, state

    def _callback(self, provider, state, cookie=None):
        """The provider sends a browser back: an unauthenticated top-level GET."""
        browser = APIClient()
        if cookie is not None:
            browser.cookies[f"{provider}_bind"] = cookie
        return browser.get(f"/api/v2/oauth/{provider}/callback/", {"code": "c", "state": state})

    def _accounts(self):
        return ConnectedAccount.objects.filter(provider__in=self.PROVIDERS).count()

    def test_initiate_sets_an_httponly_cookie_scoped_to_the_flow(self):
        for provider in self.PROVIDERS:
            with self.subTest(provider=provider):
                res, state = self._initiate(provider)
                morsel = res.cookies[f"{provider}_bind"]
                self.assertTrue(morsel["httponly"])
                self.assertEqual(morsel["path"], f"/api/v2/oauth/{provider}/")
                # The cookie holds the secret; the state holds only its hash.
                self.assertNotIn(morsel.value, state)
                self.assertNotIn(morsel.value, res.data["url"])

    def test_the_happy_path_connects_and_drops_the_cookie(self):
        for provider in self.PROVIDERS:
            with self.subTest(provider=provider):
                ConnectedAccount.objects.filter(provider=provider).delete()
                res, state = self._initiate(provider, next="/workspace/integrations?x=1")
                cookie = res.cookies[f"{provider}_bind"].value
                resp = self._callback(provider, state, cookie=cookie)
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(
                    resp["Location"], "https://app.example.test/workspace/integrations?x=1"
                )
                account = ConnectedAccount.objects.get(provider=provider)
                self.assertEqual(account.user_id, self.attacker.id)
                # One use: the callback expires the cookie.
                self.assertEqual(resp.cookies[f"{provider}_bind"]["max-age"], 0)

    def test_a_state_replayed_from_another_browser_connects_nothing(self):
        for provider in self.PROVIDERS:
            with self.subTest(provider=provider):
                _, state = self._initiate(provider)
                # The victim's browser never received the attacker's cookie.
                resp = self._callback(provider, state, cookie=None)
                self.assertIn("error=invalid_state", resp["Location"])
                self.assertEqual(self.fake.exchanged, [])
                self.assertEqual(self._accounts(), 0)

    def test_a_wrong_cookie_connects_nothing(self):
        for provider in self.PROVIDERS:
            with self.subTest(provider=provider):
                _, state = self._initiate(provider)
                resp = self._callback(provider, state, cookie="some-other-value")
                self.assertIn("error=invalid_state", resp["Location"])
                self.assertEqual(self.fake.exchanged, [])
                self.assertEqual(self._accounts(), 0)

    def test_the_cookie_of_one_provider_does_not_open_the_other(self):
        res, _ = self._initiate("google")
        _, github_state = self._initiate("github")
        browser = APIClient()
        browser.cookies["github_bind"] = res.cookies["google_bind"].value
        resp = browser.get("/api/v2/oauth/github/callback/", {"code": "c", "state": github_state})
        self.assertIn("error=invalid_state", resp["Location"])
        self.assertEqual(self._accounts(), 0)

    def test_a_state_with_no_binding_at_all_is_refused(self):
        # A connect state minted before this change (or forged by a bug) carries no `bind`.
        state = oauth_views._sign_state(
            intent="connect", user_id=str(self.attacker.id), next_path="/workspace/integrations"
        )
        resp = self._callback("google", state, cookie="anything")
        self.assertIn("error=invalid_state", resp["Location"])
        self.assertEqual(self._accounts(), 0)

    def test_a_hostile_next_is_replaced_at_initiate(self):
        for provider in self.PROVIDERS:
            for hostile in (
                "@evil.example",
                ".evil.example/x",
                "//evil.example",
                "/\\evil.example",
            ):
                with self.subTest(provider=provider, next=hostile):
                    ConnectedAccount.objects.filter(provider=provider).delete()
                    res, state = self._initiate(provider, next=hostile)
                    payload = oauth_views._verify_state(state)
                    self.assertEqual(payload["next"], "/workspace/integrations")
                    resp = self._callback(
                        provider, state, cookie=res.cookies[f"{provider}_bind"].value
                    )
                    self.assertEqual(
                        resp["Location"], "https://app.example.test/workspace/integrations"
                    )

    def test_a_hostile_next_inside_a_signed_state_is_replaced_at_the_callback(self):
        # Defence in depth: the callback does not trust what the state says.
        for hostile in ("@evil.example", ".evil.example/x", "//evil.example"):
            with self.subTest(next=hostile):
                ConnectedAccount.objects.filter(provider="google").delete()
                state = oauth_views._sign_state(
                    intent="connect",
                    user_id=str(self.attacker.id),
                    next_path=hostile,
                    bind=oauth_views._bind_hash("b"),
                )
                resp = self._callback("google", state, cookie="b")
                self.assertEqual(
                    resp["Location"], "https://app.example.test/workspace/integrations"
                )

    @override_settings(
        GITHUB_APP_ENABLED=True,
        GITHUB_APP_CLIENT_ID="app-id",
        GITHUB_APP_CLIENT_SECRET="app-secret",
        GITHUB_APP_SLUG="genos-app",
    )
    def test_with_the_github_app_on_only_the_app_cookie_is_set(self):
        # Connect GitHub posts to `github` and is answered by the App's flow (MIP-2087).
        team = TeamMaster.objects.create(
            team_name="Genos", team_email="genos@example.com", owner=self.attacker
        )
        TeamMembers.objects.create(team=team, attendee=self.attacker)
        res = self.api.post("/api/v2/oauth/github/initiate/", {"intent": "connect"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertIn("github_app_bind", res.cookies)
        self.assertNotIn("github_bind", res.cookies)

    def test_sign_in_is_not_affected(self):
        """The login flow has no signed-in person to protect and sends no cookie."""
        res = APIClient().get("/api/v2/oauth/google/initiate/", {"intent": "login"})
        self.assertEqual(res.status_code, 302)
        self.assertNotIn("google_bind", res.cookies)
        state = parse_qs(urlparse(res["Location"]).query)["state"][0]
        self.assertNotIn("bind", oauth_views._verify_state(state))
