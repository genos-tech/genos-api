"""The GitHub App install / authorize flow through "Connect GitHub" (MIP-2087, design MIP-2057 s.3).

All GitHub traffic is mocked (the network guard would fail a real call): `requests.post` answers
the token exchange, `requests.get` answers `/user` and `/user/installations` by URL.

The rules these pin:
  * off (`GITHUB_APP_ENABLED` false) means off: the old `repo` flow is unchanged and the App routes
    answer as if they did not exist;
  * `installation_id` in the callback URL is only a hint: a value GitHub does not list for the
    person's own token links nothing and stores nothing;
  * a callback with no state links nothing (the failure page);
  * the team link is written from the signed state, for a person who may manage that team.
"""

from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common.github_app_models import GithubAppInstallation, GithubAppInstallationTeam
from origin.models.common.security_event_models import SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount
from origin.services import crypto
from origin.views.common.oauth_views import (
    GITHUB_APP_BIND_COOKIE,
    _bind_hash,
    _sign_state,
    _verify_state,
)

User = get_user_model()
BIND = "the-bind-cookie-value"

APP = dict(
    GITHUB_APP_ENABLED=True,
    GITHUB_APP_ID="4242",
    GITHUB_APP_CLIENT_ID="Iv1.appclient",
    GITHUB_APP_CLIENT_SECRET="app-secret-SECRET",
    GITHUB_APP_SLUG="genos-test-app",
    BACKEND_BASE_URL="https://api.example.test",
    FRONTEND_BASE_URL="https://app.example.test",
)
OLD = dict(
    GITHUB_APP_ENABLED=False,
    GITHUB_OAUTH_CLIENT_ID="legacy-client",
    GITHUB_OAUTH_CLIENT_SECRET="legacy-secret",
    BACKEND_BASE_URL="https://api.example.test",
    FRONTEND_BASE_URL="https://app.example.test",
)

USER_TOKEN = "ghu_user-token-SECRET-1"
REFRESH = "ghr_refresh-token-SECRET-2"
INITIATE = "/api/v2/oauth/{}/initiate/"
CALLBACK = "/api/v2/oauth/github_app/callback/"
POST = "requests.post"
GET = "requests.get"


def _resp(body, status=200):
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def _installation(
    installation_id, login="genos-tech", type_="Organization", account_id=5000, **extra
):
    return {
        "id": installation_id,
        "app_id": 4242,
        "account": {"login": login, "type": type_, "id": account_id},
        "repository_selection": "selected",
        "suspended_at": None,
        **extra,
    }


class GitHubStub:
    """What GitHub answers: the person's id, and the installations their token can see."""

    def __init__(self, installations=(), github_user_id=9001):
        self.installations = list(installations)
        self.github_user_id = github_user_id
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/user/installations"):
            return _resp(
                {"total_count": len(self.installations), "installations": self.installations}
            )
        if url.endswith("/user"):
            return _resp({"id": self.github_user_id, "login": "octo", "name": "Octo"})
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return _resp(
            {
                "access_token": USER_TOKEN,
                "refresh_token": REFRESH,
                "expires_in": 28800,
                "refresh_token_expires_in": 15811200,
            }
        )

    def __enter__(self):
        self._patches = [
            mock.patch(GET, side_effect=self.get),
            mock.patch(POST, side_effect=self.post),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


class _Base(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="owner", email="owner@example.com", password="pw123456"
        )
        self.team = TeamMaster.objects.create(
            team_name="Genos", team_email="genos@example.com", owner=self.owner
        )
        TeamMembers.objects.create(team=self.team, attendee=self.owner)
        self.client.force_authenticate(user=self.owner)

    def _viewer(self, team=None):
        viewer = User.objects.create_user(
            username="viewer", email="viewer@example.com", password="pw123456"
        )
        TeamMembers.objects.create(team=team or self.team, attendee=viewer, member_role="viewer")
        return viewer

    def _state(self, user=None, team=None, **kw):
        kw.setdefault("provider", "github_app")
        kw.setdefault("bind", _bind_hash(BIND))
        return _sign_state(
            intent="connect",
            user_id=str((user or self.owner).id),
            next_path=kw.pop("next_path", "/workspace/integrations"),
            team_id=str((team or self.team).team_id),
            **kw,
        )

    def _callback(self, cookie=BIND, **params):
        """GitHub sends the browser back: an unauthenticated top-level GET carrying the flow's
        cookie (set at initiate) unless `cookie` is None."""
        anon = APIClient()
        if cookie is not None:
            anon.cookies[GITHUB_APP_BIND_COOKIE] = cookie
        return anon.get(CALLBACK, params)

    def _link(self, installation_id=7001, team=None):
        row = GithubAppInstallation.objects.create(
            installation_id=installation_id, account_login="genos-tech", account_type="Organization"
        )
        GithubAppInstallationTeam.objects.create(installation=row, team=team or self.team)
        return row

    def assertFailure(self, resp, reason):
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"https://app.example.test/oauth/success?error={reason}")

    def assertNothingStored(self):
        self.assertFalse(ConnectedAccount.objects.filter(provider="github_app").exists())
        self.assertFalse(GithubAppInstallation.objects.exists())
        self.assertFalse(GithubAppInstallationTeam.objects.exists())


@override_settings(**APP)
class InitiateTests(_Base):
    def _post(self, provider="github", **body):
        return self.client.post(
            INITIATE.format(provider), {"intent": "connect", **body}, format="json"
        )

    def test_a_team_with_no_installation_gets_the_install_url_with_a_signed_state(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        url = urlparse(resp.json()["url"])
        self.assertEqual(
            (url.scheme, url.netloc, url.path),
            ("https", "github.com", "/apps/genos-test-app/installations/new"),
        )
        payload = _verify_state(parse_qs(url.query)["state"][0])
        self.assertEqual(payload["provider"], "github_app")
        self.assertEqual(payload["user_id"], str(self.owner.id))
        self.assertEqual(payload["team_id"], str(self.team.team_id))
        self.assertEqual(payload["intent"], "connect")

    def test_a_team_that_has_an_installation_gets_the_authorize_url_without_scope(self):
        self._link()
        url = urlparse(self._post().json()["url"])
        self.assertEqual((url.netloc, url.path), ("github.com", "/login/oauth/authorize"))
        query = parse_qs(url.query)
        self.assertEqual(query["client_id"], ["Iv1.appclient"])
        # The callback URL registered for the App, exactly.
        self.assertEqual(
            query["redirect_uri"], ["https://api.example.test/api/v2/oauth/github_app/callback/"]
        )
        self.assertNotIn("scope", query)
        self.assertEqual(_verify_state(query["state"][0])["team_id"], str(self.team.team_id))

    def test_a_suspended_installation_does_not_count_so_the_install_page_is_offered(self):
        row = self._link()
        row.suspended_at = row.ts_created_at
        row.save()
        self.assertEqual(
            urlparse(self._post().json()["url"]).path, "/apps/genos-test-app/installations/new"
        )

    def test_the_button_posts_to_github_and_the_app_flow_answers(self):
        # `redirectToOAuthConnect('github')` is unchanged: only the URL it is handed differs.
        self.assertIn("/apps/genos-test-app/", self._post("github").json()["url"])
        self.assertIn("/apps/genos-test-app/", self._post("github_app").json()["url"])

    def test_an_explicit_team_id_is_used(self):
        other = TeamMaster.objects.create(
            team_name="Second", team_email="second@example.com", owner=self.owner
        )
        TeamMembers.objects.create(team=other, attendee=self.owner)
        resp = self._post(team_id=str(other.team_id))
        state = parse_qs(urlparse(resp.json()["url"]).query)["state"][0]
        self.assertEqual(_verify_state(state)["team_id"], str(other.team_id))

    def test_a_person_in_two_teams_must_say_which(self):
        other = TeamMaster.objects.create(
            team_name="Second", team_email="second@example.com", owner=self.owner
        )
        TeamMembers.objects.create(team=other, attendee=self.owner)
        resp = self._post()
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "team_required")

    def test_a_team_the_person_is_not_in_looks_like_a_missing_one(self):
        stranger_team = TeamMaster.objects.create(
            team_name="Elsewhere",
            team_email="else@example.com",
            owner=User.objects.create_user(
                username="x", email="x@example.com", password="pw123456"
            ),
        )
        for value in (
            str(stranger_team.team_id),
            "not-a-uuid",
            "00000000-0000-0000-0000-000000000000",
        ):
            with self.subTest(team_id=value):
                resp = self._post(team_id=value)
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.json()["detail"], "team_not_found")

    def test_a_viewer_cannot_start_it(self):
        self.client.force_authenticate(user=self._viewer())
        resp = self._post()
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()["detail"], "team_not_allowed")

    def test_an_editor_can(self):
        editor = User.objects.create_user(
            username="editor", email="editor@example.com", password="pw123456"
        )
        TeamMembers.objects.create(team=self.team, attendee=editor, member_role="editor")
        self.client.force_authenticate(user=editor)
        self.assertEqual(self._post().status_code, 200)

    def test_it_needs_a_signed_in_person(self):
        resp = APIClient().post(INITIATE.format("github"), {"intent": "connect"}, format="json")
        self.assertIn(resp.status_code, (401, 403))

    def test_the_app_can_never_sign_anyone_in(self):
        resp = APIClient().get(INITIATE.format("github_app"), {"intent": "login"})
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("Location", resp)

    def test_a_hostile_next_is_replaced(self):
        for value in ("//evil.example", "https://evil.example", "@evil.example", "/\\evil.example"):
            with self.subTest(next=value):
                state = parse_qs(urlparse(self._post(next=value).json()["url"]).query)["state"][0]
                self.assertEqual(_verify_state(state)["next"], "/workspace/integrations")
        state = parse_qs(urlparse(self._post(next="/workspace/tasks").json()["url"]).query)[
            "state"
        ][0]
        self.assertEqual(_verify_state(state)["next"], "/workspace/tasks")

    @override_settings(GITHUB_APP_SLUG="")
    def test_an_unconfigured_app_answers_503_not_a_broken_url(self):
        self.assertEqual(self._post("github_app").status_code, 503)


@override_settings(**OLD)
class SwitchOffTests(_Base):
    def test_off_the_connect_button_still_gets_the_legacy_repo_flow(self):
        resp = self.client.post(INITIATE.format("github"), {"intent": "connect"}, format="json")
        self.assertEqual(resp.status_code, 200)
        url = resp.json()["url"]
        self.assertIn("client_id=legacy-client", url)
        self.assertIn("scope=", url)
        self.assertNotIn("apps/", url)
        # No team in the legacy state.
        state = parse_qs(urlparse(url).query)["state"][0]
        self.assertNotIn("team_id", _verify_state(state))

    def test_off_the_app_routes_answer_as_if_they_did_not_exist(self):
        resp = self.client.post(INITIATE.format("github_app"), {"intent": "connect"}, format="json")
        self.assertEqual(resp.status_code, 404)
        with GitHubStub([_installation(7001)]) as gh:
            cb = self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertFailure(cb, "unknown_provider")
        self.assertEqual(gh.calls, [])  # GitHub was never contacted
        self.assertNothingStored()


@override_settings(**APP)
class CallbackTests(_Base):
    def test_after_an_install_the_confirmed_installation_is_linked_to_the_team(self):
        state = self._state(next_path="/workspace/integrations")
        with GitHubStub([_installation(7001), _installation(7002, login="other")]) as gh:
            resp = self._callback(
                code="the-code", state=state, installation_id="7001", setup_action="install"
            )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "https://app.example.test/workspace/integrations")

        # The code was exchanged with the App's client (not the OAuth App's).
        post = [c for c in gh.calls if c[0] == "POST"][0]
        self.assertEqual(post[2]["data"]["client_id"], "Iv1.appclient")
        self.assertEqual(post[2]["data"]["code"], "the-code")
        # The check used the NEW user token.
        get_inst = [c for c in gh.calls if c[1].endswith("/user/installations")][0]
        self.assertEqual(get_inst[2]["headers"]["Authorization"], f"Bearer {USER_TOKEN}")

        # Only the installation named in the URL AND listed by GitHub is linked.
        row = GithubAppInstallation.objects.get()
        self.assertEqual(
            (row.installation_id, row.account_login, row.account_type, row.repository_selection),
            (7001, "genos-tech", "Organization", "selected"),
        )
        link = GithubAppInstallationTeam.objects.get()
        self.assertEqual(
            (link.installation_id, link.team_id, link.linked_by_id),
            (row.id, self.team.team_id, self.owner.id),
        )

        # The github_app connection: a separate row, tokens encrypted, never a login identity.
        account = ConnectedAccount.objects.get(provider="github_app")
        self.assertEqual(account.user_id, self.owner.id)
        self.assertEqual(account.provider_user_id, "9001")
        self.assertFalse(account.is_login_identity)
        self.assertEqual(account.scopes, [])
        self.assertEqual(crypto.decrypt(account.access_token_encrypted), USER_TOKEN)
        self.assertEqual(crypto.decrypt(account.refresh_token_encrypted), REFRESH)
        self.assertIsNotNone(account.access_token_expires_at)
        self.assertNotIn(USER_TOKEN, account.access_token_encrypted)
        # The sign-in identity row is untouched (there is none here, and none was made).
        self.assertFalse(ConnectedAccount.objects.filter(provider="github").exists())

    def test_the_audit_trail_names_the_installation_and_the_team(self):
        with GitHubStub([_installation(7001)]):
            self._callback(code="c", state=self._state(), installation_id="7001")
        event = SecurityEvent.objects.filter(target_type="github_app_installation").get()
        self.assertEqual(event.target_id, "7001")
        self.assertEqual(event.metadata["team_id"], str(self.team.team_id))
        self.assertNotIn(USER_TOKEN, str(event.metadata) + event.target_label)

    def test_a_spoofed_installation_id_links_nothing_and_stores_nothing(self):
        # Someone opens the callback with an installation id that GitHub does not list for the
        # person's own token (another account's installation, or made up).
        for hint in ("9999", "7002", "0", "-1", "7001 ", "1e3", "٧٠٠١", "7001;DROP", "", "9" * 40):
            with self.subTest(installation_id=hint):
                with GitHubStub([_installation(7001)]):
                    resp = self._callback(code="c", state=self._state(), installation_id=hint)
                self.assertFailure(resp, "installation_not_confirmed")
                self.assertNothingStored()

    def test_the_hint_cannot_widen_what_is_linked_past_what_the_token_sees(self):
        with GitHubStub([]):
            resp = self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertFailure(resp, "installation_not_confirmed")
        self.assertNothingStored()

    def test_an_installation_of_another_app_is_never_linked(self):
        foreign = _installation(7001, app_id=1111)
        with GitHubStub([foreign]):
            resp = self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertFailure(resp, "installation_not_confirmed")
        self.assertNothingStored()

    def test_the_authorize_path_links_what_github_lists_for_the_person(self):
        # No installation_id: a second press of Connect GitHub (the team already had one, or the
        # install redirect carried no state the first time).
        mine = _installation(7002, login="me", type_="User", account_id=9001)
        with GitHubStub([_installation(7001), mine]):
            resp = self._callback(code="c", state=self._state())
        self.assertEqual(resp["Location"], "https://app.example.test/workspace/integrations")
        self.assertEqual(
            sorted(GithubAppInstallation.objects.values_list("installation_id", "account_type")),
            [(7001, "Organization"), (7002, "User")],
        )
        self.assertEqual(GithubAppInstallationTeam.objects.filter(team=self.team).count(), 2)

    def test_nothing_listed_means_nothing_linked(self):
        with GitHubStub([]):
            resp = self._callback(code="c", state=self._state())
        self.assertFailure(resp, "no_installation")
        self.assertNothingStored()

    def test_a_callback_with_no_state_links_nothing(self):
        with GitHubStub([_installation(7001)]) as gh:
            resp = self._callback(code="c", installation_id="7001", setup_action="install")
        self.assertFailure(resp, "github_app_state_missing")
        self.assertEqual(gh.calls, [])  # not even the token exchange
        self.assertNothingStored()

    def test_a_forged_or_expired_state_links_nothing(self):
        good = self._state()
        for bad in (good + "x", good[:-4], "garbage", "a:b:c"):
            with self.subTest(state=bad[:12]):
                with GitHubStub([_installation(7001)]) as gh:
                    resp = self._callback(code="c", state=bad, installation_id="7001")
                self.assertFailure(resp, "invalid_state")
                self.assertEqual(gh.calls, [])
                self.assertNothingStored()
        with (
            GitHubStub([_installation(7001)]),
            mock.patch("origin.views.common.oauth_views.STATE_TOKEN_MAX_AGE_SECONDS", -1),
        ):
            self.assertFailure(
                self._callback(code="c", state=good, installation_id="7001"), "invalid_state"
            )
        self.assertNothingStored()

    def test_a_state_minted_for_another_provider_is_refused(self):
        # The legacy GitHub flow's state (no provider, no team) must not connect the App.
        legacy = _sign_state(intent="connect", user_id=str(self.owner.id), next_path="/x")
        with GitHubStub([_installation(7001)]):
            self.assertFailure(
                self._callback(code="c", state=legacy, installation_id="7001"), "invalid_state"
            )
        google = self._state(provider="google")
        with GitHubStub([_installation(7001)]):
            self.assertFailure(
                self._callback(code="c", state=google, installation_id="7001"), "invalid_state"
            )
        self.assertNothingStored()

    def test_the_apps_state_is_not_valid_at_the_legacy_github_callback(self):
        with override_settings(**OLD), GitHubStub([]) as gh:
            resp = APIClient().get(
                "/api/v2/oauth/github/callback/", {"code": "c", "state": self._state()}
            )
        self.assertFailure(resp, "invalid_state")
        self.assertEqual(gh.calls, [])

    def test_a_login_state_is_refused(self):
        login = _sign_state(intent="login", user_id=None, next_path="/x", provider="github_app")
        with GitHubStub([_installation(7001)]) as gh:
            resp = self._callback(code="c", state=login)
        self.assertFailure(resp, "invalid_state")
        self.assertEqual(gh.calls, [])
        self.assertEqual(User.objects.count(), 1)  # no account was created

    def test_a_role_lost_between_initiate_and_callback_links_nothing(self):
        editor = User.objects.create_user(
            username="editor", email="editor@example.com", password="pw123456"
        )
        member = TeamMembers.objects.create(team=self.team, attendee=editor, member_role="editor")
        state = self._state(user=editor)
        member.member_role = "viewer"
        member.save()
        with GitHubStub([_installation(7001)]):
            resp = self._callback(code="c", state=state, installation_id="7001")
        self.assertFailure(resp, "team_not_allowed")
        self.assertNothingStored()

    def test_a_person_who_left_the_team_links_nothing(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.owner).delete()
        outsider = User.objects.create_user(
            username="left", email="left@example.com", password="pw123456"
        )
        with GitHubStub([_installation(7001)]):
            resp = self._callback(
                code="c", state=self._state(user=outsider), installation_id="7001"
            )
        self.assertFailure(resp, "team_not_allowed")
        self.assertNothingStored()

    def test_a_deleted_team_links_nothing(self):
        TeamMaster.objects.filter(pk=self.team.pk).update(is_deleted=True)
        with GitHubStub([_installation(7001)]):
            resp = self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertFailure(resp, "team_not_allowed")
        self.assertNothingStored()

    def test_an_inactive_person_links_nothing(self):
        state = self._state()
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        with GitHubStub([_installation(7001)]) as gh:
            resp = self._callback(code="c", state=state, installation_id="7001")
        self.assertFailure(resp, "not_authenticated")
        self.assertEqual(gh.calls, [])
        self.assertNothingStored()

    def test_denied_consent_is_the_existing_failure(self):
        self.assertFailure(
            self._callback(error="access_denied", state=self._state()), "consent_denied"
        )

    def test_a_github_failure_is_a_provider_error_and_stores_nothing(self):
        class Broken(GitHubStub):
            def get(self, url, **kw):
                if url.endswith("/user/installations"):
                    return _resp({"message": "token " + USER_TOKEN}, status=500)
                return super().get(url, **kw)

        with (
            Broken([_installation(7001)]),
            self.assertLogs("origin.services.github_app_link") as logs,
        ):
            resp = self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertFailure(resp, "provider_error")
        self.assertNothingStored()
        self.assertNotIn(USER_TOKEN, "\n".join(logs.output))

    def test_a_token_exchange_failure_never_logs_the_secret(self):
        class NoToken(GitHubStub):
            def post(self, url, **kw):
                return _resp({"error": "bad_verification_code", "error_description": "x"})

        with (
            NoToken([_installation(7001)]),
            self.assertLogs("origin.views.common.oauth_views") as logs,
        ):
            resp = self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertFailure(resp, "provider_error")
        self.assertNothingStored()
        self.assertNotIn("app-secret-SECRET", "\n".join(logs.output))

    def test_connecting_again_updates_and_keeps_the_original_link(self):
        with GitHubStub([_installation(7001)]):
            self._callback(code="c", state=self._state(), installation_id="7001")
        first_link = GithubAppInstallationTeam.objects.get()
        editor = User.objects.create_user(
            username="editor", email="editor@example.com", password="pw123456"
        )
        TeamMembers.objects.create(team=self.team, attendee=editor, member_role="editor")
        renamed = _installation(
            7001, login="genos-tech-renamed", suspended_at="2026-11-23T10:00:00Z"
        )
        with GitHubStub([renamed], github_user_id=9002):
            self._callback(code="c", state=self._state(user=editor), installation_id="7001")
        self.assertEqual(GithubAppInstallation.objects.count(), 1)
        row = GithubAppInstallation.objects.get()
        self.assertEqual(row.account_login, "genos-tech-renamed")
        # `suspended_at` belongs to the installation webhooks after creation: a listing that says
        # "suspended" (or "not") does not rewrite it.
        self.assertIsNone(row.suspended_at)
        # One link, still the first person's (the pair is unique; the editor's connect is a no-op).
        link = GithubAppInstallationTeam.objects.get()
        self.assertEqual((link.pk, link.linked_by_id), (first_link.pk, self.owner.id))
        # Both people hold their own github_app connection.
        self.assertEqual(ConnectedAccount.objects.filter(provider="github_app").count(), 2)

    def test_one_installation_can_serve_two_teams(self):
        second = TeamMaster.objects.create(
            team_name="Second", team_email="second@example.com", owner=self.owner
        )
        TeamMembers.objects.create(team=second, attendee=self.owner)
        with GitHubStub([_installation(7001)]):
            self._callback(code="c", state=self._state(), installation_id="7001")
            self._callback(code="c", state=self._state(team=second), installation_id="7001")
        self.assertEqual(GithubAppInstallation.objects.count(), 1)
        self.assertEqual(
            set(GithubAppInstallationTeam.objects.values_list("team_id", flat=True)),
            {self.team.team_id, second.team_id},
        )

    def test_a_github_identity_connected_by_someone_else_is_refused_and_links_nothing(self):
        other = User.objects.create_user(
            username="other", email="other@example.com", password="pw123456"
        )
        ConnectedAccount.objects.create(
            user=other,
            provider="github_app",
            provider_user_id="9001",
            access_token_encrypted=crypto.encrypt("x"),
        )
        with GitHubStub([_installation(7001)]):
            resp = self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertFailure(resp, "already_connected_to_other_user")
        self.assertFalse(GithubAppInstallation.objects.exists())
        self.assertFalse(GithubAppInstallationTeam.objects.exists())

    def test_a_second_github_identity_for_the_same_person_is_refused(self):
        ConnectedAccount.objects.create(
            user=self.owner,
            provider="github_app",
            provider_user_id="1234",
            access_token_encrypted=crypto.encrypt("x"),
        )
        with GitHubStub([_installation(7001)]):
            resp = self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertFailure(resp, "provider_already_connected")
        self.assertFalse(GithubAppInstallation.objects.exists())

    def test_the_legacy_sign_in_identity_row_is_never_touched(self):
        legacy = ConnectedAccount.objects.create(
            user=self.owner,
            provider="github",
            provider_user_id="9001",
            is_login_identity=True,
            scopes=["read:user", "user:email"],
            access_token_encrypted=crypto.encrypt("gho_legacy"),
        )
        before = (legacy.access_token_encrypted, legacy.scopes, legacy.is_login_identity)
        with GitHubStub([_installation(7001)]):
            self._callback(code="c", state=self._state(), installation_id="7001")
        legacy.refresh_from_db()
        self.assertEqual(
            (legacy.access_token_encrypted, legacy.scopes, legacy.is_login_identity), before
        )
        self.assertEqual(ConnectedAccount.objects.filter(user=self.owner).count(), 2)

    def test_a_hostile_next_in_a_state_goes_to_the_default(self):
        state = _sign_state(
            intent="connect",
            user_id=str(self.owner.id),
            next_path="@evil.example",
            provider="github_app",
            team_id=str(self.team.team_id),
            bind=_bind_hash(BIND),
        )
        with GitHubStub([_installation(7001)]):
            resp = self._callback(code="c", state=state, installation_id="7001")
        self.assertEqual(resp["Location"], "https://app.example.test/workspace/integrations")

    def test_an_installation_with_an_odd_account_type_is_skipped(self):
        with GitHubStub([_installation(7001, type_="Bot"), _installation(7002)]):
            self._callback(code="c", state=self._state())
        self.assertEqual(
            list(GithubAppInstallation.objects.values_list("installation_id", flat=True)), [7002]
        )

    def test_a_listed_suspension_does_not_rewrite_an_existing_row(self):
        row = self._link()
        row.suspended_at = row.ts_created_at
        row.save()
        with GitHubStub([_installation(7001)]):
            self._callback(code="c", state=self._state(), installation_id="7001")
        row.refresh_from_db()
        self.assertIsNotNone(row.suspended_at)

    def test_a_new_row_takes_the_suspension_github_lists(self):
        with GitHubStub([_installation(7001, suspended_at="2026-11-23T10:00:00Z")]):
            self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertIsNotNone(GithubAppInstallation.objects.get().suspended_at)


@override_settings(**APP)
class BrowserBindingTests(_Base):
    """The state is a bearer ticket unless it is tied to the browser that started the flow: an
    attacker who starts a connect for THEIR team could hand the GitHub URL to a victim, and the
    victim's own authorization would store the victim's token and link the victim's installations
    under the attacker's account and team."""

    def test_initiate_sets_a_scoped_httponly_cookie_and_the_state_carries_only_its_hash(self):
        resp = self.client.post(INITIATE.format("github"), {"intent": "connect"}, format="json")
        cookie = resp.cookies[GITHUB_APP_BIND_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], "/api/v2/oauth/github_app/")
        self.assertEqual(int(cookie["max-age"]), 600)
        state = parse_qs(urlparse(resp.json()["url"]).query)["state"][0]
        bind = _verify_state(state)["bind"]
        self.assertEqual(bind, _bind_hash(cookie.value))
        self.assertNotIn(cookie.value, state)  # the secret itself never rides in the URL

    def test_the_legacy_flow_sets_no_cookie(self):
        with override_settings(**OLD):
            resp = self.client.post(INITIATE.format("github"), {"intent": "connect"}, format="json")
        self.assertNotIn(GITHUB_APP_BIND_COOKIE, resp.cookies)

    def test_a_browser_without_the_cookie_links_nothing(self):
        # The victim of a handed-over URL: a valid state, a valid code, but not the cookie.
        with GitHubStub([_installation(7001)]) as gh:
            resp = self._callback(cookie=None, code="victims-code", state=self._state())
        self.assertFailure(resp, "github_app_state_missing")
        self.assertEqual(gh.calls, [])  # not even the token exchange
        self.assertNothingStored()

    def test_a_browser_with_another_cookie_links_nothing(self):
        for value in ("another-value", "", _bind_hash(BIND), BIND + " "):
            with self.subTest(cookie=value[:8]):
                with GitHubStub([_installation(7001)]) as gh:
                    resp = self._callback(cookie=value, code="c", state=self._state())
                self.assertFailure(resp, "github_app_state_missing")
                self.assertEqual(gh.calls, [])
                self.assertNothingStored()

    def test_a_state_without_a_bind_is_refused(self):
        state = self._state(bind=None)
        with GitHubStub([_installation(7001)]) as gh:
            resp = self._callback(code="c", state=state)
        self.assertFailure(resp, "github_app_state_missing")
        self.assertEqual(gh.calls, [])

    def test_the_cookie_is_dropped_after_one_use_success_or_failure(self):
        with GitHubStub([_installation(7001)]):
            ok = self._callback(code="c", state=self._state(), installation_id="7001")
            bad = self._callback(code="c", state=self._state(), installation_id="9999")
        for resp in (ok, bad):
            self.assertEqual(resp.cookies[GITHUB_APP_BIND_COOKIE]["max-age"], 0)

    def test_the_matching_browser_links(self):
        with GitHubStub([_installation(7001)]):
            resp = self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertEqual(resp["Location"], "https://app.example.test/workspace/integrations")
        self.assertEqual(GithubAppInstallationTeam.objects.count(), 1)


@override_settings(**APP)
class WhoMayLinkTests(_Base):
    """`/user/installations` also lists installations a person reaches through a single repository
    (an outside collaborator), so being listed is not ownership of the installation."""

    def test_a_user_account_installation_of_someone_else_is_never_linked(self):
        # Mallory (GitHub id 9001) is a collaborator on a repo of the personal account 1234.
        theirs = _installation(7001, login="alice", type_="User", account_id=1234)
        for hint in ({"installation_id": "7001"}, {}):
            with self.subTest(hint=hint):
                with GitHubStub([theirs]):
                    resp = self._callback(code="c", state=self._state(), **hint)
                self.assertFailure(
                    resp,
                    "installation_not_confirmed" if hint else "no_installation",
                )
                self.assertNothingStored()

    def test_a_user_account_installation_of_the_person_is_linked(self):
        mine = _installation(7001, login="octo", type_="User", account_id=9001)
        with GitHubStub([mine]):
            self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertEqual(GithubAppInstallationTeam.objects.get().installation.installation_id, 7001)

    def test_a_user_installation_with_no_account_id_is_not_linked(self):
        odd = _installation(7001, login="octo", type_="User")
        odd["account"].pop("id")
        with GitHubStub([odd]):
            resp = self._callback(code="c", state=self._state())
        self.assertFailure(resp, "no_installation")

    def _acme_team(self):
        acme_owner = User.objects.create_user(
            username="acme", email="acme@example.com", password="pw123456"
        )
        acme = TeamMaster.objects.create(
            team_name="Acme", team_email="acme@example.com", owner=acme_owner
        )
        TeamMembers.objects.create(team=acme, attendee=acme_owner)
        self._link(7001, team=acme)
        return acme

    def test_an_organization_installation_adopted_by_another_team_is_not_pulled_in(self):
        # Acme's team holds the installation; Mallory, who is not in it, presses Connect.
        self._acme_team()
        for hint in ({"installation_id": "7001"}, {}):
            with self.subTest(hint=hint):
                with GitHubStub([_installation(7001, login="acme")]):
                    resp = self._callback(code="c", state=self._state(), **hint)
                self.assertFailure(
                    resp,
                    "installation_not_confirmed" if hint else "no_installation",
                )
                self.assertFalse(GithubAppInstallationTeam.objects.filter(team=self.team).exists())
                self.assertFalse(ConnectedAccount.objects.filter(provider="github_app").exists())

    def test_the_other_listed_installations_still_link_when_one_is_refused(self):
        self._acme_team()
        listed = [_installation(7001, login="acme"), _installation(7002, login="mine-org")]
        with GitHubStub(listed):
            self._callback(code="c", state=self._state())
        self.assertEqual(
            list(
                GithubAppInstallationTeam.objects.filter(team=self.team).values_list(
                    "installation__installation_id", flat=True
                )
            ),
            [7002],
        )

    def test_a_person_in_a_team_that_holds_it_may_link_it_to_their_other_team(self):
        acme = self._acme_team()
        TeamMembers.objects.create(team=acme, attendee=self.owner)
        with GitHubStub([_installation(7001, login="acme")]):
            self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertEqual(
            set(GithubAppInstallationTeam.objects.values_list("team_id", flat=True)),
            {acme.team_id, self.team.team_id},
        )

    def test_an_unlinked_organization_installation_can_be_claimed(self):
        with GitHubStub([_installation(7001, login="acme")]):
            self._callback(code="c", state=self._state(), installation_id="7001")
        self.assertEqual(GithubAppInstallationTeam.objects.get().team_id, self.team.team_id)
