"""The `github_credential(user, repo)` seam (MIP-2089, design MIP-2057 sections 5-7).

Which stored GitHub account (so which token) a call made for a person uses:

  1. the App user token, when `GITHUB_APP_ENABLED` is on, the person has one, and the App covers
     the repo for them (or no repo is named);
  2. the legacy OAuth token otherwise: switch off, no App connection, or a repo the App does not
     cover (an active installation of the owner's account, linked to a team the person is in, that
     covers every repo or lists this one);
  3. the App token anyway when there is no legacy one.

Plus the App user token's refresh under the PR 1 row lock, reached through the seam, the views and
the agent tools using it, and the installation token for background work.
"""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.models.common.github_app_models import (
    GithubAppInstallation,
    GithubAppInstallationTeam,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount
from origin.services import crypto
from origin.services.github_app import InstallationUnavailable
from origin.services.github_credential import (
    APP,
    LEGACY,
    CredentialResolver,
    github_credential,
    installation_token,
)
from origin.services.oauth.base import TokenResponse
from origin.services.oauth.github_app import GitHubAppOAuthProvider
from origin.views.common import github_views

User = get_user_model()

ON = dict(
    GITHUB_APP_ENABLED=True,
    GITHUB_APP_ID="12345",
    GITHUB_APP_CLIENT_ID="Iv1.appclient",
    GITHUB_APP_CLIENT_SECRET="app-secret-SECRET",
)
REQUESTS_GET = "origin.services.github_credential.requests.get"


def _listing(names, status=200):
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = {"repositories": [{"name": n} for n in names]}
    return resp


class SeamBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(
            username="cred", email="cred@test.com", password="x", is_email_verified=True
        )
        self.team = TeamMaster.objects.create(
            team_name="Cred Team", team_email="cred@team.com", owner=self.user
        )

    def legacy(self):
        return ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="1",
            access_token_encrypted=crypto.encrypt("gho_legacy"),
        )

    def app(self, **over):
        fields = dict(
            user=self.user,
            provider="github_app",
            provider_user_id="1",
            access_token_encrypted=crypto.encrypt("ghu_app"),
            refresh_token_encrypted=crypto.encrypt("ghr_app"),
            access_token_expires_at=timezone.now() + timedelta(hours=4),
        )
        fields.update(over)
        return ConnectedAccount.objects.create(**fields)

    def installation(self, login="acme", selection="all", suspended=False, team=None, number=900):
        inst = GithubAppInstallation.objects.create(
            installation_id=number,
            account_login=login,
            account_type="Organization",
            repository_selection=selection,
            suspended_at=timezone.now() if suspended else None,
        )
        if team is not False:
            GithubAppInstallationTeam.objects.create(
                installation=inst, team=team or self.team, linked_by=self.user
            )
        return inst


@override_settings(GITHUB_APP_ENABLED=False)
class TestSwitchOff(SeamBase):
    def test_no_connection_is_none(self):
        self.assertIsNone(github_credential(self.user))
        self.assertIsNone(github_credential(self.user, ("acme", "rocket")))
        self.assertIsNone(github_credential(None))

    def test_legacy_is_used(self):
        legacy = self.legacy()
        got = github_credential(self.user, ("acme", "rocket"))
        self.assertEqual((got.kind, got.account.pk), (LEGACY, legacy.pk))

    def test_an_app_row_is_ignored_while_the_switch_is_off(self):
        legacy = self.legacy()
        self.app()
        self.installation()
        got = github_credential(self.user, ("acme", "rocket"))
        self.assertEqual((got.kind, got.account.pk), (LEGACY, legacy.pk))

    def test_an_app_row_alone_is_not_a_credential_while_the_switch_is_off(self):
        self.app()
        self.assertIsNone(github_credential(self.user))


@override_settings(**ON)
class TestSwitchOn(SeamBase):
    def test_no_connection_is_none(self):
        self.assertIsNone(github_credential(self.user, ("acme", "rocket")))

    def test_legacy_only_stays_legacy(self):
        legacy = self.legacy()
        self.installation()  # an installation without the person's App token proves nothing
        got = github_credential(self.user, ("acme", "rocket"))
        self.assertEqual((got.kind, got.account.pk), (LEGACY, legacy.pk))

    def test_app_only_is_the_app_even_for_an_uncovered_repo(self):
        # Nothing else to try: GitHub's own 404 answers a repo the App cannot reach.
        app = self.app()
        got = github_credential(self.user, ("elsewhere", "repo"))
        self.assertEqual((got.kind, got.account.pk), (APP, app.pk))

    def test_a_call_about_the_person_uses_the_app(self):
        self.legacy()
        app = self.app()
        got = github_credential(self.user)
        self.assertEqual((got.kind, got.account.pk), (APP, app.pk))

    def test_a_covered_repo_uses_the_app(self):
        self.legacy()
        app = self.app()
        self.installation(selection="all")
        got = github_credential(self.user, ("ACME", "Rocket"))  # GitHub names are case-blind
        self.assertEqual((got.kind, got.account.pk), (APP, app.pk))

    def test_a_repo_of_an_account_without_an_installation_falls_back_to_legacy(self):
        legacy = self.legacy()
        self.app()
        self.installation(login="acme")
        got = github_credential(self.user, ("someone-else", "rocket"))
        self.assertEqual((got.kind, got.account.pk), (LEGACY, legacy.pk))

    def test_a_suspended_installation_does_not_cover(self):
        legacy = self.legacy()
        self.app()
        self.installation(suspended=True)
        self.assertEqual(github_credential(self.user, ("acme", "rocket")).account.pk, legacy.pk)

    def test_an_installation_linked_to_a_team_the_person_is_not_in_does_not_cover(self):
        legacy = self.legacy()
        self.app()
        stranger = User.objects.create_user(
            username="stranger", email="stranger@test.com", password="x", is_email_verified=True
        )
        other = TeamMaster.objects.create(
            team_name="Other", team_email="other@team.com", owner=stranger
        )
        self.installation(team=other)
        self.assertEqual(github_credential(self.user, ("acme", "rocket")).account.pk, legacy.pk)

    def test_an_installation_with_no_team_link_does_not_cover(self):
        legacy = self.legacy()
        self.app()
        self.installation(team=False)
        self.assertEqual(github_credential(self.user, ("acme", "rocket")).account.pk, legacy.pk)

    def test_a_team_member_is_covered_through_the_team_not_only_the_owner(self):
        member = User.objects.create_user(
            username="member", email="member@test.com", password="x", is_email_verified=True
        )
        TeamMembers.objects.create(team=self.team, attendee=member)
        ConnectedAccount.objects.create(
            user=member,
            provider="github",
            provider_user_id="9",
            access_token_encrypted=crypto.encrypt("gho_m"),
        )
        app = ConnectedAccount.objects.create(
            user=member,
            provider="github_app",
            provider_user_id="9",
            access_token_encrypted=crypto.encrypt("ghu_m"),
            refresh_token_encrypted=crypto.encrypt("ghr_m"),
            access_token_expires_at=timezone.now() + timedelta(hours=4),
        )
        self.installation()
        self.assertEqual(github_credential(member, ("acme", "rocket")).account.pk, app.pk)


@override_settings(**ON)
class TestSelectedRepositories(SeamBase):
    """An installation on selected repositories covers a repo only when GitHub lists it for the
    person (`GET /user/installations/{id}/repositories`, with their own token)."""

    def setUp(self):
        super().setUp()
        self.legacy_row = self.legacy()
        self.app_row = self.app()
        self.installation(selection="selected", number=901)

    @patch(REQUESTS_GET)
    def test_a_listed_repo_is_covered_and_an_unlisted_one_is_not(self, get):
        get.return_value = _listing(["rocket", "Other"])
        self.assertEqual(github_credential(self.user, ("acme", "rocket")).kind, APP)
        self.assertEqual(github_credential(self.user, ("acme", "other")).kind, APP)
        self.assertEqual(github_credential(self.user, ("acme", "hidden")).kind, LEGACY)
        # The listing was asked with the person's own App token, for that installation.
        first = get.call_args_list[0]
        self.assertIn("/user/installations/901/repositories", first.args[0])
        self.assertEqual(first.kwargs["headers"]["Authorization"], "Bearer ghu_app")

    @patch(REQUESTS_GET)
    def test_the_listing_is_cached(self, get):
        get.return_value = _listing(["rocket"])
        for _ in range(3):
            self.assertEqual(github_credential(self.user, ("acme", "rocket")).kind, APP)
        self.assertEqual(get.call_count, 1)

    @patch(REQUESTS_GET)
    def test_a_resolver_for_a_walk_asks_once_for_many_repos(self, get):
        get.return_value = _listing(["a", "b", "c"])
        resolver = CredentialResolver(self.user)
        kinds = [resolver.for_repo(("acme", name)).kind for name in ("a", "b", "c", "d")]
        self.assertEqual(kinds, [APP, APP, APP, LEGACY])
        self.assertEqual(get.call_count, 1)

    @patch(REQUESTS_GET)
    def test_a_failed_listing_falls_back_and_is_not_cached(self, get):
        get.return_value = _listing([], status=500)
        self.assertEqual(github_credential(self.user, ("acme", "rocket")).kind, LEGACY)
        get.return_value = _listing(["rocket"])
        self.assertEqual(github_credential(self.user, ("acme", "rocket")).kind, APP)

    @patch(REQUESTS_GET, side_effect=requests.Timeout("slow"))
    def test_a_timeout_falls_back(self, _get):
        self.assertEqual(github_credential(self.user, ("acme", "rocket")).kind, LEGACY)

    @patch(REQUESTS_GET)
    def test_a_listing_longer_than_the_pages_read_cannot_tell_so_falls_back(self, get):
        get.return_value = _listing([f"r{i}" for i in range(100)])  # every page full
        self.assertEqual(github_credential(self.user, ("acme", "r1000")).kind, LEGACY)
        self.assertEqual(get.call_count, 5)

    @patch(REQUESTS_GET)
    def test_a_dead_app_connection_cannot_tell_so_falls_back(self, get):
        ConnectedAccount.objects.filter(pk=self.app_row.pk).update(
            access_token_expires_at=timezone.now() - timedelta(minutes=1),
            refresh_token_encrypted="",
        )
        self.assertEqual(github_credential(self.user, ("acme", "rocket")).kind, LEGACY)
        get.assert_not_called()


def _fresh(access="ghu_new", refresh="ghr_new"):
    return TokenResponse(
        access_token=access, refresh_token=refresh, expires_in_seconds=28800, granted_scopes=[]
    )


@override_settings(**ON)
class TestTheAppTokenRefreshesUnderTheLock(SeamBase):
    def test_the_credential_token_is_the_current_one_when_valid(self):
        self.app()
        with patch.object(GitHubAppOAuthProvider, "refresh") as refresh:
            self.assertEqual(github_credential(self.user).token(), "ghu_app")
        refresh.assert_not_called()

    def test_an_expired_token_is_refreshed_and_the_rotated_refresh_token_stored(self):
        row = self.app(access_token_expires_at=timezone.now() - timedelta(minutes=1))
        with patch.object(GitHubAppOAuthProvider, "refresh", return_value=_fresh()) as refresh:
            self.assertEqual(github_credential(self.user).token(), "ghu_new")
        refresh.assert_called_once_with(refresh_token="ghr_app")
        stored = ConnectedAccount.objects.get(pk=row.pk)
        self.assertEqual(crypto.decrypt(stored.refresh_token_encrypted), "ghr_new")

    def test_the_refresh_runs_inside_a_row_lock(self):
        self.app(access_token_expires_at=timezone.now() - timedelta(minutes=1))
        seen = {}

        def fake_refresh(*, refresh_token):
            from django.db import connection

            seen["in_atomic"] = connection.in_atomic_block
            return _fresh()

        with patch.object(GitHubAppOAuthProvider, "refresh", side_effect=fake_refresh):
            with patch(
                "origin.services.oauth.tokens.ConnectedAccount.objects.select_for_update",
                wraps=ConnectedAccount.objects.select_for_update,
            ) as locker:
                github_credential(self.user).token()
        locker.assert_called_once()
        self.assertTrue(seen["in_atomic"])

    def test_a_credential_built_before_another_request_refreshed_does_not_spend_the_dead_token(
        self,
    ):
        self.app(access_token_expires_at=timezone.now() - timedelta(minutes=1))
        stale = github_credential(self.user)  # loaded before the winner refreshes
        with patch.object(GitHubAppOAuthProvider, "refresh", return_value=_fresh()):
            github_credential(self.user).token()
        with patch.object(GitHubAppOAuthProvider, "refresh") as refresh:
            self.assertEqual(stale.token(), "ghu_new")
        refresh.assert_not_called()

    def test_the_view_credential_refreshes_through_the_same_lock(self):
        # `_connected_account` is the views' face on the seam; its token goes through
        # `get_valid_access_token` as before.
        self.app(access_token_expires_at=timezone.now() - timedelta(minutes=1))
        account = github_views._connected_account(self.user)
        self.assertEqual(account.provider, "github_app")
        with patch.object(GitHubAppOAuthProvider, "refresh", return_value=_fresh()) as refresh:
            credential = github_views._GithubCredential(account).resolve()
        self.assertEqual(credential.token, "ghu_new")
        refresh.assert_called_once()


@override_settings(**ON)
class TestViewsAndToolsUseTheSeam(SeamBase):
    def test_connected_account_passes_the_repo_to_the_seam(self):
        legacy = self.legacy()
        app = self.app()
        self.installation()
        self.assertEqual(github_views._connected_account(self.user, ("acme", "r")).pk, app.pk)
        self.assertEqual(github_views._connected_account(self.user, ("other", "r")).pk, legacy.pk)
        self.assertEqual(github_views._connected_account(self.user).pk, app.pk)

    @patch("origin.search_engine.agent.tools.list_pr_files._github_get")
    def test_an_agent_tool_reads_with_the_app_token_for_a_covered_repo(self, get):
        from origin.search_engine.agent.tools.base import ToolContext
        from origin.search_engine.agent.tools.list_pr_files import LIST_PR_FILES

        self.legacy()
        app = self.app()
        self.installation()
        resp = MagicMock(spec=requests.Response)
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = []
        get.return_value = resp
        LIST_PR_FILES.run(
            {"pr_url": "https://github.com/acme/rocket/pull/3"},
            ToolContext(user_id=str(self.user.id), team_id=str(self.team.team_id)),
        )
        self.assertEqual(get.call_args.args[0].pk, app.pk)
        # ... and with the legacy one for a repo the App does not cover.
        LIST_PR_FILES.run(
            {"pr_url": "https://github.com/other/rocket/pull/3"},
            ToolContext(user_id=str(self.user.id), team_id=str(self.team.team_id)),
        )
        self.assertEqual(get.call_args.args[0].provider, "github")


@override_settings(**ON)
class TestAWalkMixesTheTwoTokens(SeamBase):
    """The PR column walks every registered repo: a repo the App covers is read with the App
    token, another with the legacy token, in the same request, and the pulls walk finds the
    task's branch through each repo's pulls list (no branches call)."""

    def test_each_repo_is_read_with_its_own_credential(self):
        from rest_framework.test import APIClient

        from origin.models.common.user_models import GithubWebhookRegistration
        from origin.models.project.prj_models import ProjectMaster, ProjectMembers
        from origin.models.task.task_models import TaskMaster
        from origin.tests.github_pull_fakes import pull, pulls_response

        self.legacy()
        self.app()
        self.installation(login="acme", selection="all")
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Core",
            owner=self.user,
            project_system_user=self.user,
            code="GEN",
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(
            team=self.team,
            project=project,
            assignee=self.user,
            reporter=self.user,
            title="t",
            status="Open",
            project_task_number=42,
        )
        for owner, repo, hook in (("acme", "rocket", 1), ("other", "thing", 2)):
            GithubWebhookRegistration.objects.create(
                owner=owner, repo=repo, hook_id=hook, registered_by=self.user
            )
        seen = {}

        def fake_get(url, headers=None, params=None, timeout=None):
            path = url.removeprefix(github_views.GITHUB_API_BASE)
            seen[path] = headers["Authorization"]
            owner, repo = path.split("/")[2:4]
            return pulls_response(
                [pull(5 if owner == "acme" else 6, "GEN-42-x", owner=owner, repo=repo)]
            )

        client = APIClient()
        client.force_authenticate(user=self.user)
        with patch.object(github_views.requests, "get", fake_get):
            body = client.get(f"/api/v2/github/pulls/for-task/?task_id={task.task_id}").json()
        self.assertEqual(
            seen,
            {
                "/repos/acme/rocket/pulls": "Bearer ghu_app",
                "/repos/other/thing/pulls": "Bearer gho_legacy",
            },
        )
        self.assertEqual({p["repo"] for p in body["pulls"]}, {"rocket", "thing"})


@override_settings(**ON)
class TestADeadAppGrant(SeamBase):
    """A revoked or expired App grant (the user token rotates, so it CAN die, unlike the legacy
    one) must not crash the PR views, nor be retried under the row lock for every repo."""

    def dead_app(self):
        return self.app(
            access_token_expires_at=timezone.now() - timedelta(minutes=1),
            refresh_token_encrypted="",
        )

    def test_with_a_legacy_token_the_legacy_one_is_used(self):
        legacy = self.legacy()
        self.dead_app()
        self.installation()
        got = github_credential(self.user, ("acme", "rocket"))
        self.assertEqual((got.kind, got.account.pk), (LEGACY, legacy.pk))
        self.assertEqual(github_credential(self.user).kind, LEGACY)

    def test_the_refresh_is_tried_once_for_a_whole_walk(self):
        self.legacy()
        self.dead_app()
        self.installation()
        resolver = CredentialResolver(self.user)
        with patch(
            "origin.services.github_credential.get_valid_access_token", side_effect=Exception("x")
        ) as refresh:
            for name in ("a", "b", "c", "d"):
                resolver.for_repo(("acme", name))
        self.assertEqual(refresh.call_count, 1)

    def test_the_coverage_listing_is_asked_once_per_installation_for_a_walk(self):
        self.legacy()
        self.app()
        self.installation(selection="selected", number=902)
        resolver = CredentialResolver(self.user)
        with patch(REQUESTS_GET, return_value=_listing(["a", "b"])) as get:
            for name in ("a", "b", "c"):
                resolver.for_repo(("acme", name))
        self.assertEqual(get.call_count, 1)
        # and the cache lookup itself is made once, not once per repo
        with patch("origin.services.github_credential.cache.get", wraps=cache.get) as cached:
            again = CredentialResolver(self.user)
            for name in ("a", "b", "c"):
                again.for_repo(("acme", name))
        self.assertEqual(cached.call_count, 1)

    def _body(self, route):
        from rest_framework.test import APIClient

        from origin.models.common.user_models import GithubWebhookRegistration
        from origin.models.project.prj_models import ProjectMaster, ProjectMembers
        from origin.models.task.task_models import TaskMaster

        if not hasattr(self, "_task"):
            project = ProjectMaster.objects.create(
                team=self.team,
                project_name="Core",
                owner=self.user,
                project_system_user=self.user,
                code="GEN",
            )
            ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
            self._task = TaskMaster.objects.create(
                team=self.team,
                project=project,
                assignee=self.user,
                reporter=self.user,
                title="t",
                status="Open",
                project_task_number=42,
            )
            GithubWebhookRegistration.objects.create(
                owner="acme", repo="rocket", hook_id=1, registered_by=self.user
            )
        client = APIClient()
        client.force_authenticate(user=self.user)
        return client.get(route.format(task=self._task.task_id))

    @patch.object(github_views.requests, "get")
    def test_the_pr_views_answer_instead_of_failing_when_it_is_the_only_credential(self, get):
        self.dead_app()
        self.installation()
        for route in (
            "/api/v2/github/pulls/for-task/?task_id={task}",
            "/api/v2/github/branches/for-task/?task_id={task}",
            "/api/v2/github/pulls/for-tasks/?task_ids={task}",
        ):
            with self.subTest(route=route):
                cache.clear()
                resp = self._body(route)
                self.assertEqual(resp.status_code, 200)
        batch = self._body("/api/v2/github/pulls/for-tasks/?task_ids={task}").json()
        self.assertEqual(len(batch["incomplete"]), 1)  # "could not look", not "no PR"
        get.assert_not_called()  # no token, so GitHub was never asked

    @patch.object(github_views.requests, "get")
    def test_the_pr_detail_answers_a_502_not_a_500(self, get):
        from rest_framework.test import APIClient

        self.dead_app()
        client = APIClient()
        client.force_authenticate(user=self.user)
        resp = client.get("/api/v2/github/pulls/acme/rocket/7/")
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["upstream_status"], 401)
        get.assert_not_called()

    @patch.object(github_views.requests, "get")
    def test_my_pulls_answers_a_502_not_a_500(self, get):
        from rest_framework.test import APIClient

        self.dead_app()
        client = APIClient()
        client.force_authenticate(user=self.user)
        self.assertEqual(client.get("/api/v2/github/pulls/").status_code, 502)
        get.assert_not_called()


@override_settings(**ON)
class TestInstallationToken(SeamBase):
    @patch("origin.services.github_credential.github_app.get_installation_token")
    def test_narrowed_to_the_one_repo_for_a_linked_active_installation(self, mint):
        mint.return_value = "ghs_install"
        self.installation(number=555)
        self.assertEqual(installation_token("ACME", "rocket", [self.team.team_id]), "ghs_install")
        mint.assert_called_once_with(555, repositories=["rocket"])

    @patch("origin.services.github_credential.github_app.get_installation_token")
    def test_none_without_a_linked_active_installation(self, mint):
        self.installation(suspended=True)
        self.assertIsNone(installation_token("acme", "rocket", [self.team.team_id]))
        self.assertIsNone(installation_token("acme", "rocket", []))
        mint.assert_not_called()

    @patch("origin.services.github_credential.github_app.get_installation_token")
    def test_none_for_another_teams_installation(self, mint):
        stranger = User.objects.create_user(
            username="stranger", email="stranger@test.com", password="x", is_email_verified=True
        )
        other = TeamMaster.objects.create(
            team_name="Other", team_email="other@team.com", owner=stranger
        )
        self.installation(team=other)
        self.assertIsNone(installation_token("acme", "rocket", [self.team.team_id]))
        mint.assert_not_called()

    @patch(
        "origin.services.github_credential.github_app.get_installation_token",
        side_effect=InstallationUnavailable("gone"),
    )
    def test_none_when_the_token_cannot_be_minted(self, _mint):
        self.installation()
        self.assertIsNone(installation_token("acme", "rocket", [self.team.team_id]))

    @override_settings(GITHUB_APP_ENABLED=False)
    @patch("origin.services.github_credential.github_app.get_installation_token")
    def test_none_while_the_switch_is_off(self, mint):
        self.installation()
        self.assertIsNone(installation_token("acme", "rocket", [self.team.team_id]))
        mint.assert_not_called()


@override_settings(**ON)
class TestThePrHeadRefLookupUsesAnInstallationTokenFirst(SeamBase):
    """The webhook's `issue_comment` follow-up reads a PR with the registrar's identity today;
    with an installation linked to the registrar's team it reads with a one-repo installation
    token instead (design section 5: background work never holds a person's token)."""

    def setUp(self):
        super().setUp()
        from origin.models.common.user_models import GithubWebhookRegistration

        GithubWebhookRegistration.objects.create(
            owner="acme", repo="rocket", hook_id=1, registered_by=self.user
        )

    def _ok(self):
        resp = MagicMock(spec=requests.Response)
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {"head": {"ref": "feature/GEN-1-x"}}
        return resp

    @patch.object(github_views, "_github_get_with_token")
    @patch("origin.views.common.github_views.installation_token", return_value="ghs_install")
    def test_installation_token_when_one_is_linked(self, _tok, get_with_token):
        get_with_token.return_value = self._ok()
        self.legacy()
        self.assertEqual(github_views._fetch_pr_head_ref("acme", "rocket", 3), "feature/GEN-1-x")
        self.assertEqual(get_with_token.call_args.args[0], "ghs_install")

    @patch.object(github_views, "_github_get")
    @patch("origin.views.common.github_views.installation_token", return_value=None)
    def test_the_registrars_credential_otherwise(self, _tok, get):
        get.return_value = self._ok()
        legacy = self.legacy()
        self.assertEqual(github_views._fetch_pr_head_ref("acme", "rocket", 4), "feature/GEN-1-x")
        self.assertEqual(get.call_args.args[0].pk, legacy.pk)

    def test_nothing_without_any_connection(self):
        self.assertIsNone(github_views._fetch_pr_head_ref("acme", "rocket", 5))

    @patch.object(github_views, "_github_get")
    @patch.object(github_views, "_github_get_with_token")
    @patch("origin.views.common.github_views.installation_token", return_value="ghs_install")
    def test_a_failing_installation_read_falls_back_to_the_registrars_credential(
        self, _tok, with_token, get
    ):
        with_token.return_value = MagicMock(ok=False, status_code=404)
        get.return_value = self._ok()
        legacy = self.legacy()
        self.assertEqual(github_views._fetch_pr_head_ref("acme", "rocket", 6), "feature/GEN-1-x")
        self.assertEqual(get.call_args.args[0].pk, legacy.pk)

    @patch.object(github_views, "_github_get")
    @patch.object(github_views, "_github_get_with_token", side_effect=requests.Timeout("slow"))
    @patch("origin.views.common.github_views.installation_token", return_value="ghs_install")
    def test_an_installation_read_that_raises_falls_back_too(self, _tok, _with_token, get):
        get.return_value = self._ok()
        self.legacy()
        self.assertEqual(github_views._fetch_pr_head_ref("acme", "rocket", 7), "feature/GEN-1-x")

    @override_settings(GITHUB_APP_ENABLED=False)
    @patch.object(github_views, "_github_get")
    @patch("origin.views.common.github_views.installation_token")
    def test_no_installation_lookup_while_the_switch_is_off(self, tok, get):
        get.return_value = self._ok()
        self.legacy()
        github_views._fetch_pr_head_ref("acme", "rocket", 8)
        tok.assert_not_called()
