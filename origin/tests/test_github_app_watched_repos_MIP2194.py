"""The reconcile cron, the PR scans and the grant revocation read the GitHub App (MIP-2194).

Until now "the repos Genos watches" was the rows of `GithubWebhookRegistration` and the cron read
every repo with the registrar's legacy token, so the broad `repo` grant of every registrar had to
live. With `GITHUB_APP_ENABLED`:

  * `reconcile_pr_deliveries` also watches the repos the App lists for a linked installation (no
    registration row needed) and reads a covered repo with a read-only installation token; a repo
    the App does not cover, or whose token cannot be minted, still uses the registrar's token;
  * the PR-column scans walk the registered repos plus the App's repos for the requester's teams;
  * `revoke_legacy_github_grants` stops holding back a registrar whose every hook is migrated and
    whose repos the App covers, and still holds back one with a live hook or an uncovered repo.

Switch off, every one of these reads exactly what it read before. All GitHub traffic is mocked.
"""

from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common.github_app_models import (
    GithubAppInstallation,
    GithubAppInstallationRepository,
    GithubAppInstallationTeam,
)
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_delivery_models import TaskDeliveryState
from origin.models.task.task_models import TaskMaster
from origin.services import github_app_migration as mig
from origin.services.github_watched_repos import app_repos, merge_repos
from origin.tests import test_github_app_migration as migration_tests
from origin.tests import test_reconcile_pr_deliveries as reconcile_tests
from origin.tests.github_pull_fakes import pull, pulls_response

CMD = reconcile_tests.CMD
APP_ON = dict(GITHUB_APP_ENABLED=True, BACKEND_BASE_URL="https://api.example.test")
INSTALLATION_TOKEN = "ghs_installation-token-SECRET"
REGISTRAR_HEADERS = {"Authorization": "Bearer registrar-token"}


class _Capturing:
    """`requests.get` stand-in that records the headers of each call."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.headers = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        self.headers.append(kwargs.get("headers"))
        return reconcile_tests._Resp(200, self.answers[int(url.rsplit("/", 1)[1])])


def _install(team, owner, *repos, user=None, installation_id=7001, selection="selected"):
    inst = GithubAppInstallation.objects.create(
        installation_id=installation_id,
        account_login=owner,
        account_type="Organization",
        repository_selection=selection,
    )
    for name in repos:
        GithubAppInstallationRepository.objects.create(
            installation=inst, full_name=f"{owner}/{name}".lower()
        )
    GithubAppInstallationTeam.objects.create(installation=inst, team=team, linked_by=user)
    return inst


@override_settings(**APP_ON)
class TestReconcileUsesTheApp(reconcile_tests.ReconcileCase):
    def _run_with(self, github, *, token=INSTALLATION_TOKEN):
        with (
            mock.patch(f"{CMD}.requests.get", github),
            mock.patch(f"{CMD}.installation_token", return_value=token) as minted,
            mock.patch(f"{CMD}.hook_admin_headers", return_value=REGISTRAR_HEADERS) as legacy,
        ):
            call_command("reconcile_pr_deliveries")
        return minted, legacy

    def test_a_covered_repo_is_read_with_an_installation_token_not_the_registrars(self):
        _install(self.team, "owner", "repo", user=self.user)
        self._link(self.task, 42)
        github = _Capturing({42: reconcile_tests._pr(42, merged=True)})

        minted, legacy = self._run_with(github)

        self.assertEqual(self._status(), "Closed")
        minted.assert_called_once()
        self.assertEqual(minted.call_args.args[:2], ("owner", "repo"))
        legacy.assert_not_called()
        self.assertEqual(github.headers[0]["Authorization"], f"Bearer {INSTALLATION_TOKEN}")

    def test_a_covered_repo_needs_no_registration_row(self):
        GithubWebhookRegistration.objects.all().delete()
        _install(self.team, "owner", "repo", user=self.user)
        row = self._link(self.task, 42)
        github = _Capturing({42: reconcile_tests._pr(42, merged=True)})

        minted, legacy = self._run_with(github)

        self.assertEqual(self._status(), "Closed")
        row.refresh_from_db()
        self.assertEqual(row.state, TaskDeliveryState.MERGED)
        legacy.assert_not_called()
        self.assertEqual(len(github.calls), 1)

    def test_an_uncovered_repo_keeps_the_registrars_token(self):
        # The App has no installation for this owner: nothing changes for the legacy hook.
        self._link(self.task, 42)
        github = _Capturing({42: reconcile_tests._pr(42, merged=True)})

        minted, legacy = self._run_with(github)

        self.assertEqual(self._status(), "Closed")
        minted.assert_not_called()
        legacy.assert_called_once()
        self.assertEqual(github.headers[0], REGISTRAR_HEADERS)

    def test_a_covered_repo_whose_token_cannot_be_minted_falls_back_to_the_registrar(self):
        _install(self.team, "owner", "repo", user=self.user)
        self._link(self.task, 42)
        github = _Capturing({42: reconcile_tests._pr(42, merged=True)})

        minted, legacy = self._run_with(github, token=None)

        self.assertEqual(self._status(), "Closed")
        legacy.assert_called_once()
        self.assertEqual(github.headers[0], REGISTRAR_HEADERS)

    def test_a_repo_the_app_lists_but_that_cannot_be_read_is_skipped_with_a_warning(self):
        GithubWebhookRegistration.objects.all().delete()
        _install(self.team, "owner", "repo", user=self.user)
        self._link(self.task, 42)
        github = _Capturing({42: reconcile_tests._pr(42, merged=True)})

        with self.assertLogs("origin", level="WARNING") as logs:
            self._run_with(github, token=None)  # no CommandError: not an ERROR

        self.assertEqual(github.calls, [])
        self.assertTrue(any("not checked" in line for line in logs.output))
        self.assertEqual(self._status(), "Open")

    def test_only_the_teams_the_app_serves_or_the_registrars_are_written_for(self):
        # An installation linked to another team must not make this team's task close from a
        # repo that team does not own: the other team's deliveries are the only ones applied.
        stranger = reconcile_tests.User.objects.create_user(
            username="stranger2",
            email="stranger2@test.com",
            password="testpass123",
            is_email_verified=True,
            auto_close_on_pr_merge=True,
        )
        other_team = TeamMaster.objects.create(
            team_name="Other2", team_email="other2@team.com", owner=stranger
        )
        GithubWebhookRegistration.objects.all().delete()
        _install(other_team, "owner", "repo", user=stranger)
        mine = self._link(self.task, 42)
        github = _Capturing({42: reconcile_tests._pr(42, merged=True)})

        self._run_with(github)

        self.assertEqual(github.calls, [], "no open PR in a team the App serves: GitHub not asked")
        self.assertEqual(self._status(), "Open")
        mine.refresh_from_db()
        self.assertEqual(mine.state, TaskDeliveryState.OPEN)

    def test_a_failing_token_mint_does_not_stop_the_run_or_the_next_repo(self):
        # A bad App key raises something other than GithubAppError: one repo must not abort the
        # cron for every repo after it.
        _install(self.team, "owner", "repo", user=self.user)
        self._link(self.task, 42)
        github = _Capturing({42: reconcile_tests._pr(42, merged=True)})
        with (
            mock.patch(f"{CMD}.requests.get", github),
            mock.patch(f"{CMD}.installation_token", side_effect=ValueError("bad key")),
            mock.patch(f"{CMD}.hook_admin_headers", return_value=REGISTRAR_HEADERS),
            self.assertLogs("origin", level="WARNING") as logs,
        ):
            call_command("reconcile_pr_deliveries")  # does not raise
        self.assertEqual(self._status(), "Closed")  # fell back to the registrar's token
        self.assertEqual(github.headers[0], REGISTRAR_HEADERS)
        self.assertTrue(any("ValueError" in line for line in logs.output))
        self.assertFalse(any("bad key" in line for line in logs.output))

    @override_settings(GITHUB_APP_ENABLED=False)
    def test_with_the_switch_off_an_installation_is_ignored(self):
        GithubWebhookRegistration.objects.all().delete()
        _install(self.team, "owner", "repo", user=self.user)
        self._link(self.task, 42)
        github = _Capturing({42: reconcile_tests._pr(42, merged=True)})

        minted, legacy = self._run_with(github)

        self.assertEqual(github.calls, [])
        self.assertEqual(self._status(), "Open")
        minted.assert_not_called()

    @override_settings(GITHUB_APP_ENABLED=False)
    def test_with_the_switch_off_the_registrars_token_is_used_as_before(self):
        _install(self.team, "owner", "repo", user=self.user)
        self._link(self.task, 42)
        github = _Capturing({42: reconcile_tests._pr(42, merged=True)})

        minted, legacy = self._run_with(github)

        self.assertEqual(self._status(), "Closed")
        legacy.assert_called_once()
        self.assertEqual(github.headers[0], REGISTRAR_HEADERS)


class TestWatchedRepos(TestCase):
    def setUp(self):
        self.user = reconcile_tests.User.objects.create_user(
            username="watcher", email="watcher@test.com", password="testpass123"
        )
        self.team = TeamMaster.objects.create(
            team_name="Watch", team_email="watch@team.com", owner=self.user
        )
        self.other = TeamMaster.objects.create(
            team_name="Watch2", team_email="watch2@team.com", owner=self.user
        )

    @override_settings(**APP_ON)
    def test_lists_the_repos_of_active_linked_installations_only(self):
        _install(self.team, "Acme", "API", "web", user=self.user)
        suspended = _install(self.other, "Zed", "x", user=self.user, installation_id=7002)
        from django.utils import timezone

        GithubAppInstallation.objects.filter(pk=suspended.pk).update(suspended_at=timezone.now())
        self.assertEqual(app_repos(), [("acme", "api"), ("acme", "web")])
        self.assertEqual(app_repos([self.team.pk]), [("acme", "api"), ("acme", "web")])
        self.assertEqual(app_repos([self.other.pk]), [])
        self.assertEqual(app_repos([]), [])

    @override_settings(GITHUB_APP_ENABLED=False)
    def test_is_empty_with_the_switch_off(self):
        _install(self.team, "acme", "api", user=self.user)
        self.assertEqual(app_repos(), [])

    def test_merge_keeps_the_first_spelling_once(self):
        self.assertEqual(
            merge_repos([("Acme", "API")], [("acme", "api"), ("acme", "web")]),
            [("Acme", "API"), ("acme", "web")],
        )


@override_settings(**APP_ON)
class TestScansWalkTheAppsRepos(TestCase):
    def setUp(self):
        cache.clear()
        self.enterContext(
            mock.patch("origin.views.common.github_views.get_valid_access_token", return_value="t")
        )
        self.client = APIClient()
        self.user = reconcile_tests.User.objects.create_user(
            username="scan-user",
            email="scan@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.team = TeamMaster.objects.create(
            team_name="Scan Team", team_email="scan@team.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Scan",
            owner=self.user,
            project_system_user=self.user,
            code="GEN",
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title="T",
            status="Open",
            project_task_number=42,
        )
        ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="9",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        cache.clear()

    def _paths(self):
        paths = []

        def side_effect(_account, path, params=None):
            paths.append(path)
            return pulls_response([pull(7, "feature/GEN-42-x", title="X")])

        return paths, side_effect

    def test_a_repo_with_no_registration_is_scanned_when_the_app_lists_it(self):
        _install(self.team, "acme", "rocket", user=self.user)
        paths, side_effect = self._paths()
        with mock.patch("origin.views.common.github_views._github_get", side_effect=side_effect):
            resp = self.client.get(f"/api/v2/github/pulls/for-tasks/?task_ids={self.task.task_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(paths, ["/repos/acme/rocket/pulls"])
        self.assertEqual(len(resp.json()["pulls_by_task"][str(self.task.task_id)]), 1)

    def test_the_single_task_view_and_the_branch_view_walk_it_too(self):
        _install(self.team, "acme", "rocket", user=self.user)
        for route in (
            f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}",
            f"/api/v2/github/branches/for-task/?task_id={self.task.task_id}",
        ):
            cache.clear()
            paths, side_effect = self._paths()
            with mock.patch(
                "origin.views.common.github_views._github_get", side_effect=side_effect
            ):
                resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, route)
            self.assertEqual(paths, ["/repos/acme/rocket/pulls"], route)

    def test_a_repo_registered_and_listed_is_walked_once(self):
        GithubWebhookRegistration.objects.create(
            owner="Acme", repo="Rocket", hook_id=1, registered_by=self.user
        )
        _install(self.team, "acme", "rocket", user=self.user)
        paths, side_effect = self._paths()
        with mock.patch("origin.views.common.github_views._github_get", side_effect=side_effect):
            self.client.get(f"/api/v2/github/pulls/for-tasks/?task_ids={self.task.task_id}")
        self.assertEqual(len(paths), 1)

    def test_an_installation_linked_to_another_teams_is_not_walked(self):
        stranger = reconcile_tests.User.objects.create_user(
            username="scan-stranger", email="scan-stranger@test.com", password="testpass123"
        )
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@team.com", owner=stranger
        )
        _install(other_team, "secret-org", "vault", user=stranger)
        paths, side_effect = self._paths()
        with mock.patch("origin.views.common.github_views._github_get", side_effect=side_effect):
            self.client.get(f"/api/v2/github/pulls/for-tasks/?task_ids={self.task.task_id}")
        self.assertEqual(paths, [])

    @override_settings(GITHUB_APP_ENABLED=False)
    def test_with_the_switch_off_only_registered_repos_are_walked(self):
        _install(self.team, "acme", "rocket", user=self.user)
        paths, side_effect = self._paths()
        with mock.patch("origin.views.common.github_views._github_get", side_effect=side_effect):
            self.client.get(f"/api/v2/github/pulls/for-tasks/?task_ids={self.task.task_id}")
        self.assertEqual(paths, [])


REVOKE = migration_tests.REVOKE


@override_settings(
    GITHUB_OAUTH_CLIENT_ID="Iv1.oauthapp",
    GITHUB_OAUTH_CLIENT_SECRET="oauth-secret",
    **APP_ON,
)
class TestRevokeRuleForRegistrars(migration_tests.RevokeBase):
    def setUp(self):
        self.moved = self._user("moved")
        self._legacy_account(self.moved)
        self._app_account(self.moved)
        self.team = self._team("Acme", self.moved)

    def _migrate(self, reg):
        GithubWebhookRegistration.objects.filter(pk=reg.pk).update(
            accepts_shared_secret=False, secret=None
        )

    def _eligible_count(self):
        out, _ = self._run_revoke()
        return int(out.split(" legacy grant(s) eligible")[0]), out

    def test_a_registrar_whose_hook_is_migrated_and_repo_covered_is_no_longer_held_back(self):
        reg = self._registration("acme", "api", self.moved, connect=False)
        self._migrate(reg)
        inst = self._installation(repos=("acme/api",))
        self._link(inst, self.team, self.moved)

        count, out = self._eligible_count()
        self.assertEqual(count, 1, out)
        self.assertIn("0 held back", out)
        with mock.patch(REVOKE) as delete:
            delete.return_value = migration_tests._resp(204)
            self._run_revoke("--apply")
        delete.assert_called_once()

    def test_a_registrar_with_a_live_hook_is_held_back_even_when_the_repo_is_covered(self):
        # The hook still exists at GitHub: re-keying or deleting it needs the registrar's token.
        self._registration("acme", "api", self.moved, connect=False)
        inst = self._installation(repos=("acme/api",))
        self._link(inst, self.team, self.moved)

        count, out = self._eligible_count()
        self.assertEqual(count, 0, out)
        self.assertIn("1 held back", out)

    def test_a_registrar_with_a_migrated_hook_but_an_uncovered_repo_is_held_back(self):
        reg = self._registration("acme", "api", self.moved, connect=False)
        self._migrate(reg)  # no installation: the cron falls back to the registrar's token

        count, out = self._eligible_count()
        self.assertEqual(count, 0, out)

    def test_one_uncovered_repo_among_covered_ones_holds_the_registrar_back(self):
        a = self._registration("acme", "api", self.moved, hook_id=1, connect=False)
        b = self._registration("acme", "web", self.moved, hook_id=2, connect=False)
        self._migrate(a)
        self._migrate(b)
        inst = self._installation(repos=("acme/api",))  # web is not listed
        self._link(inst, self.team, self.moved)

        count, _ = self._eligible_count()
        self.assertEqual(count, 0)

    def test_an_installation_linked_only_to_another_team_does_not_release_the_registrar(self):
        # The cron would read this repo with the other team's installation, which can unlink
        # tomorrow: the registrar's own team is not served, so their token stays.
        reg = self._registration("acme", "api", self.moved, connect=False)
        self._migrate(reg)
        stranger = self._user("stranger")
        inst = self._installation(repos=("acme/api",))
        self._link(inst, self._team("Elsewhere", stranger), stranger)

        count, _ = self._eligible_count()
        self.assertEqual(count, 0)

    def test_a_suspended_installation_no_longer_covers(self):
        from django.utils import timezone

        reg = self._registration("acme", "api", self.moved, connect=False)
        self._migrate(reg)
        inst = self._installation(repos=("acme/api",))
        self._link(inst, self.team, self.moved)
        GithubAppInstallation.objects.filter(pk=inst.pk).update(suspended_at=timezone.now())

        count, _ = self._eligible_count()
        self.assertEqual(count, 0)

    @override_settings(GITHUB_APP_ENABLED=False)
    def test_with_the_switch_off_every_registrar_is_held_back(self):
        reg = self._registration("acme", "api", self.moved, connect=False)
        self._migrate(reg)
        inst = self._installation(repos=("acme/api",))
        self._link(inst, self.team, self.moved)

        count, _ = self._eligible_count()
        self.assertEqual(count, 0)

    def test_include_registrars_still_overrides(self):
        self._registration("acme", "api", self.moved, connect=False)
        plan = mig.plan_grant_revocation(include_registrars=True)
        self.assertEqual(len(plan.eligible), 1)
        self.assertEqual(plan.registrars, [])
