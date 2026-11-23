"""The GitHub App migration commands (MIP-2091, design MIP-2057 section 6).

`migrate_github_hooks_to_app` deletes the legacy per-repo hooks the App covers; `revoke_legacy_github_grants`
revokes the legacy `repo` grants; `GITHUB_CONNECT_REPO_SCOPE_REMOVED` stops Connect GitHub asking for `repo`.

The rules these pin:
  * both commands REPORT by default and change nothing (no GitHub call, no write) without `--apply`;
  * only a COVERED hook is deleted: every team it writes for is served by an active installation that
    covers the repo. A repo the App does not cover, a suspended or unlinked installation, a hook with one
    uncovered team and a hook with no team are left alone;
  * only the Genos hook is deleted (its URL path is the legacy webhook path);
  * a second run is a no-op (a hook GitHub no longer has counts as done);
  * a failure leaves the hook / the row in place and exits non-zero;
  * nothing printed or logged holds a token.

All GitHub traffic is mocked.
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from origin.models.common.github_app_models import (
    GithubAppInstallation,
    GithubAppInstallationRepository,
    GithubAppInstallationTeam,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.services import crypto
from origin.services import github_app_migration as mig
from origin.services.oauth.github import GitHubOAuthProvider
from origin.views.common.github_views import _repo_team_ids

User = get_user_model()

LEGACY_TOKEN = "gho_legacy-repo-token-SECRET"
HOOK_URL = "https://api.example.test/api/v2/github/webhook/"
APP = dict(GITHUB_APP_ENABLED=True)
GITHUB = "origin.services.github_app_migration.requests"


def _resp(status=200, body=None):
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    return resp


def _hook_body(url=HOOK_URL):
    return {"id": 99, "config": {"url": url}}


class Base(TestCase):
    def _user(self, name):
        return User.objects.create_user(
            username=name, email=f"{name}@example.com", password="pw123456"
        )

    def _team(self, name, owner):
        return TeamMaster.objects.create(
            team_name=name, team_email=f"{name}@example.com", owner=owner
        )

    def _legacy_account(
        self, user, *, scopes=("read:user", "user:email", "repo"), token=LEGACY_TOKEN
    ):
        return ConnectedAccount.objects.create(
            user=user,
            provider="github",
            provider_user_id=f"gh-{user.username}",
            scopes=list(scopes),
            access_token_encrypted=crypto.encrypt(token),
        )

    def _app_account(self, user):
        return ConnectedAccount.objects.create(
            user=user,
            provider="github_app",
            provider_user_id=f"app-{user.username}",
            access_token_encrypted=crypto.encrypt("ghu_app-token"),
        )

    def _registration(self, owner, repo, registrar, hook_id=99, connect=True):
        if connect and registrar is not None:
            ConnectedAccount.objects.get_or_create(
                user=registrar,
                provider="github",
                defaults=dict(
                    provider_user_id=f"gh-{registrar.username}",
                    scopes=["read:user", "user:email", "repo"],
                    access_token_encrypted=crypto.encrypt(LEGACY_TOKEN),
                ),
            )
        return GithubWebhookRegistration.objects.create(
            owner=owner, repo=repo, hook_id=hook_id, registered_by=registrar
        )

    def _installation(
        self, login="acme", *, installation_id=5001, selection="selected", repos=("acme/api",)
    ):
        inst = GithubAppInstallation.objects.create(
            installation_id=installation_id,
            account_login=login,
            account_type="Organization",
            repository_selection=selection,
        )
        for full_name in repos:
            GithubAppInstallationRepository.objects.create(
                installation=inst, full_name=full_name.lower()
            )
        return inst

    def _link(self, inst, team, user):
        return GithubAppInstallationTeam.objects.create(
            installation=inst, team=team, linked_by=user
        )

    def _run(self, *args, **kwargs):
        out, err = StringIO(), StringIO()
        call_command("migrate_github_hooks_to_app", *args, stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()


@override_settings(**APP)
class HookMigrationReportTests(Base):
    """The default run reports and changes nothing."""

    def setUp(self):
        self.user = self._user("registrar")
        self.team = self._team("Acme", self.user)
        self.reg = self._registration("acme", "api", self.user)
        inst = self._installation()
        self._link(inst, self.team, self.user)

    def test_default_run_reports_and_calls_github_never(self):
        with mock.patch(GITHUB) as gh:
            out, _ = self._run()
        gh.get.assert_not_called()
        gh.delete.assert_not_called()
        self.assertIn(f"team {self.team.team_id}: 1 legacy hook(s)", out)
        self.assertIn("covered      acme/api  would delete hook", out)
        self.assertIn("1 legacy hook(s): 1 covered, 0 partly covered", out)
        self.assertIn("Dry run: nothing was changed", out)
        self.reg.refresh_from_db()
        self.assertTrue(self.reg.accepts_shared_secret)

    def test_counts_only_prints_no_repository_name(self):
        out, _ = self._run("--counts-only")
        self.assertIn(f"team {self.team.team_id}: 1 legacy hook(s), 1 covered, 0 kept", out)
        self.assertNotIn("acme/api", out)

    def test_check_github_asks_read_only_and_says_what_it_found(self):
        with mock.patch(GITHUB) as gh:
            gh.RequestException = requests.RequestException
            gh.get.return_value = _resp(200, _hook_body())
            out, _ = self._run("--check-github")
        gh.get.assert_called_once()
        gh.delete.assert_not_called()
        self.assertIn("[GitHub: present]", out)
        self.assertIn("GitHub says: 1 present", out)

    def test_check_github_apply_together_is_refused(self):
        with self.assertRaises(CommandError):
            self._run("--check-github", "--apply")

    def test_off_switch_reads_nothing_covered_and_apply_refuses(self):
        with override_settings(GITHUB_APP_ENABLED=False):
            with mock.patch(GITHUB) as gh:
                out, _ = self._run()
                with self.assertRaises(CommandError):
                    self._run("--apply")
        gh.get.assert_not_called()
        gh.delete.assert_not_called()
        self.assertIn("GITHUB_APP_ENABLED is off", out)
        self.assertIn("not covered  acme/api", out)
        self.assertEqual(GithubWebhookRegistration.objects.count(), 1)

    def test_invalid_team_and_repo_arguments_are_refused(self):
        with self.assertRaises(CommandError):
            self._run("--team", "not-a-uuid")
        with self.assertRaises(CommandError):
            self._run("--repo", "no-slash")

    def test_team_and_repo_filters(self):
        other_user = self._user("other")
        other_team = self._team("Other", other_user)
        self._registration("acme", "web", other_user)
        out, _ = self._run("--team", str(self.team.team_id))
        self.assertIn("acme/api", out)
        self.assertNotIn("acme/web", out)
        out, _ = self._run("--repo", "ACME/WEB")
        self.assertIn(f"team {other_team.team_id}", out)
        self.assertNotIn("acme/api", out)


@override_settings(**APP)
class CoverageTests(Base):
    """Which hooks count as covered: the legacy drop's own rule."""

    def setUp(self):
        self.user = self._user("registrar")
        self.team = self._team("Acme", self.user)

    def _verdict(self, owner="acme", repo="api"):
        (plan,) = mig.plan_hook_migration(repo=f"{owner}/{repo}")
        return plan.verdict

    def test_listed_repo_of_a_linked_active_installation_is_covered(self):
        self._registration("acme", "api", self.user)
        self._link(self._installation(), self.team, self.user)
        self.assertEqual(self._verdict(), mig.COVERED)

    def test_all_repositories_installation_covers_any_repo_of_its_account(self):
        self._registration("acme", "anything", self.user)
        inst = self._installation(selection="all", repos=())
        self._link(inst, self.team, self.user)
        self.assertEqual(self._verdict(repo="anything"), mig.COVERED)

    def test_repo_the_installation_does_not_list_is_not_covered(self):
        self._registration("acme", "other-repo", self.user)
        self._link(self._installation(), self.team, self.user)
        self.assertEqual(self._verdict(repo="other-repo"), mig.NOT_COVERED)

    def test_all_repositories_of_another_account_does_not_cover_this_owner(self):
        self._registration("acme", "api", self.user)
        inst = self._installation(login="someone-else", selection="all", repos=())
        self._link(inst, self.team, self.user)
        self.assertEqual(self._verdict(), mig.NOT_COVERED)

    def test_installation_with_no_team_link_is_not_covered(self):
        self._registration("acme", "api", self.user)
        self._installation()
        self.assertEqual(self._verdict(), mig.NOT_COVERED)

    def test_suspended_installation_is_not_covered(self):
        from django.utils import timezone

        self._registration("acme", "api", self.user)
        inst = self._installation()
        self._link(inst, self.team, self.user)
        GithubAppInstallation.objects.filter(pk=inst.pk).update(suspended_at=timezone.now())
        self.assertEqual(self._verdict(), mig.NOT_COVERED)

    def test_installation_linked_to_another_team_does_not_cover_this_teams_hook(self):
        self._registration("acme", "api", self.user)
        other = self._user("other")
        other_team = self._team("Other", other)
        self._link(self._installation(), other_team, other)
        self.assertEqual(self._verdict(), mig.NOT_COVERED)

    def test_hook_with_one_covered_and_one_uncovered_team_is_partly_covered(self):
        second = self._team("Acme Two", self.user)  # the registrar owns both teams
        self._registration("acme", "api", self.user)
        self._link(self._installation(), self.team, self.user)
        (plan,) = mig.plan_hook_migration()
        self.assertEqual(plan.verdict, mig.PARTLY_COVERED)
        self.assertEqual(plan.uncovered_teams, (str(second.team_id),))

    def test_hook_whose_registrar_is_gone_writes_nothing_and_is_kept(self):
        self._registration("acme", "api", None)
        self._link(self._installation(), self.team, self.user)
        self.assertEqual(self._verdict(), mig.NO_TEAMS)

    def test_teams_match_the_legacy_endpoints_own_scope(self):
        member = self._user("member")
        team_b = self._team("B", member)
        TeamMembers.objects.create(team=team_b, attendee=self.user)
        self._registration("acme", "api", self.user)
        (plan,) = mig.plan_hook_migration()
        self.assertEqual(plan.teams, tuple(sorted(str(t) for t in _repo_team_ids("acme", "api"))))


@override_settings(**APP)
class HookMigrationApplyTests(Base):
    def setUp(self):
        self.user = self._user("registrar")
        self.team = self._team("Acme", self.user)
        self.reg = self._registration("acme", "api", self.user)
        self.inst = self._installation()
        self._link(self.inst, self.team, self.user)

    def _apply(self, gh, *args):
        gh.RequestException = requests.RequestException
        return self._run("--apply", *args)

    def test_apply_deletes_the_covered_hook_and_keeps_the_row(self):
        with (
            mock.patch(GITHUB) as gh,
            self.assertLogs("origin.management.commands", level="INFO") as cmd_logs,
        ):
            gh.get.return_value = _resp(200, _hook_body())
            gh.delete.return_value = _resp(204)
            out, err = self._apply(gh)
        gh.delete.assert_called_once()
        url = gh.delete.call_args.args[0]
        self.assertEqual(url, "https://api.github.com/repos/acme/api/hooks/99")
        self.assertEqual(
            gh.delete.call_args.kwargs["headers"]["Authorization"], f"Bearer {LEGACY_TOKEN}"
        )
        self.assertFalse(gh.delete.call_args.kwargs["allow_redirects"])
        self.assertIn("deleted        acme/api", out)
        self.assertIn("deleted 1, already gone 0, left 0 of 1 covered hook(s)", out)
        self.reg.refresh_from_db()
        self.assertFalse(self.reg.accepts_shared_secret)
        self.assertTrue(any("outcome=deleted" in line for line in cmd_logs.output))
        for text in (out, err, *cmd_logs.output):
            self.assertNotIn(LEGACY_TOKEN, text)

    def test_second_run_is_a_no_op(self):
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(200, _hook_body())
            gh.delete.return_value = _resp(204)
            self._apply(gh)
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(404)
            out, _ = self._apply(gh)
        gh.delete.assert_not_called()
        self.assertIn("deleted 0, already gone 1, left 0 of 1 covered hook(s)", out)
        self.assertEqual(GithubWebhookRegistration.objects.count(), 1)

    def test_uncovered_repos_are_left_alone(self):
        # Not listed by the installation, an unlinked installation, and a hook with an uncovered team.
        self._registration("acme", "not-installed", self.user)
        second = self._user("second")
        self._team("Second", second)
        self._registration("acme", "unlinked", second)
        self._installation(login="acme", installation_id=5002, repos=("acme/unlinked",))
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(200, _hook_body())
            gh.delete.return_value = _resp(204)
            out, _ = self._apply(gh)
        # Only the one covered hook was read and deleted.
        self.assertEqual(gh.get.call_count, 1)
        self.assertEqual(gh.delete.call_count, 1)
        self.assertIn("acme/not-installed", out)
        untouched = GithubWebhookRegistration.objects.exclude(pk=self.reg.pk)
        self.assertEqual(untouched.count(), 2)
        self.assertTrue(all(r.accepts_shared_secret for r in untouched))

    def test_partly_covered_hook_is_kept(self):
        self._team("Acme Two", self.user)
        with mock.patch(GITHUB) as gh:
            out, _ = self._apply(gh)
        gh.get.assert_not_called()
        gh.delete.assert_not_called()
        self.assertIn("kept: the App serves only some of its teams", out)

    def test_a_hook_pointing_elsewhere_is_not_deleted(self):
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(200, _hook_body("https://ci.example.com/hook"))
            with self.assertRaises(CommandError):
                self._apply(gh)
        gh.delete.assert_not_called()
        self.reg.refresh_from_db()
        self.assertTrue(self.reg.accepts_shared_secret)

    def test_hook_url_is_matched_by_path_so_a_changed_host_still_counts(self):
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(
                200, _hook_body("https://old-host.example.org/api/v2/github/webhook")
            )
            gh.delete.return_value = _resp(204)
            self._apply(gh)
        gh.delete.assert_called_once()

    def test_registrar_without_a_github_connection_fails_the_run_and_deletes_nothing(self):
        ConnectedAccount.objects.filter(user=self.user, provider="github").delete()
        with mock.patch(GITHUB) as gh:
            with self.assertRaises(CommandError):
                self._apply(gh)
        gh.get.assert_not_called()
        gh.delete.assert_not_called()
        self.reg.refresh_from_db()
        self.assertTrue(self.reg.accepts_shared_secret)

    def test_registrar_who_lost_admin_leaves_the_hook_and_fails_the_run(self):
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(200, _hook_body())
            gh.delete.return_value = _resp(403)
            with self.assertRaises(CommandError) as ctx:
                self._apply(gh)
        self.assertIn("1 covered hook(s) were not deleted", str(ctx.exception))
        self.reg.refresh_from_db()
        self.assertTrue(self.reg.accepts_shared_secret)

    def test_a_redirect_for_a_renamed_repo_is_never_followed(self):
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(301)
            with self.assertRaises(CommandError):
                self._apply(gh)
        self.assertFalse(gh.get.call_args.kwargs["allow_redirects"])
        gh.delete.assert_not_called()

    def test_github_unreachable_is_a_failure_not_a_crash(self):
        with mock.patch(GITHUB) as gh:
            gh.RequestException = requests.RequestException
            gh.get.side_effect = requests.ConnectTimeout("boom: token=" + LEGACY_TOKEN)
            with self.assertRaises(CommandError) as ctx:
                self._run("--apply")
        self.assertNotIn(LEGACY_TOKEN, str(ctx.exception))
        gh.delete.assert_not_called()

    def test_a_stale_plan_is_rechecked_before_the_write(self):
        (plan,) = mig.plan_hook_migration()
        self.assertEqual(plan.verdict, mig.COVERED)
        GithubAppInstallationTeam.objects.all().delete()  # the link went away after the plan
        with mock.patch(GITHUB) as gh:
            self.assertEqual(mig.migrate_hook(plan), "not_covered")
        gh.get.assert_not_called()
        gh.delete.assert_not_called()

    def test_migrate_hook_refuses_a_plan_that_is_not_covered(self):
        other = self._registration("acme", "other", self.user)
        plan = mig.classify(other)
        self.assertEqual(plan.verdict, mig.NOT_COVERED)
        with mock.patch(GITHUB) as gh:
            self.assertEqual(mig.migrate_hook(plan), "not_covered")
        gh.delete.assert_not_called()

    def test_a_repo_name_that_could_walk_the_api_path_is_refused(self):
        bad = GithubWebhookRegistration.objects.create(
            owner="acme", repo="..", hook_id=5, registered_by=self.user
        )
        self.assertEqual(mig.probe_hook(bad, {}), mig.INVALID_REPO)


@override_settings(GITHUB_APP_ENABLED=False)
class HookRestoreTests(Base):
    """`--restore`, the rollback: recreate the hooks GitHub no longer has."""

    def setUp(self):
        self.user = self._user("registrar")
        self._team("Acme", self.user)
        self.reg = self._registration("acme", "api", self.user, hook_id=99)
        GithubWebhookRegistration.objects.filter(pk=self.reg.pk).update(accepts_shared_secret=False)

    def _restore(self, gh, *args):
        gh.RequestException = requests.RequestException
        return self._run("--restore", *args)

    def test_report_asks_github_read_only_and_creates_nothing(self):
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(404)
            out, _ = self._restore(gh)
        gh.post.assert_not_called()
        self.assertIn("would_restore", out)
        self.assertIn("Dry run: nothing was changed", out)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.hook_id, 99)

    def test_apply_recreates_a_gone_hook_with_a_secret_of_its_own(self):
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(404)
            gh.post.return_value = _resp(201, {"id": 4242})
            out, _ = self._restore(gh, "--apply")
        body = gh.post.call_args.kwargs["json"]
        self.assertEqual(gh.post.call_args.args[0], "https://api.github.com/repos/acme/api/hooks")
        self.assertEqual(
            body["events"], ["pull_request", "issue_comment", "pull_request_review_comment"]
        )
        self.assertTrue(body["config"]["url"].endswith("/api/v2/github/webhook/"))
        self.assertGreaterEqual(len(body["config"]["secret"]), 32)
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.hook_id, 4242)
        self.assertEqual(self.reg.secret, body["config"]["secret"])
        self.assertFalse(self.reg.accepts_shared_secret)
        self.assertNotIn(body["config"]["secret"], out)
        self.assertNotIn(LEGACY_TOKEN, out)

    def test_a_hook_that_still_exists_is_left_alone(self):
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(200, _hook_body())
            out, _ = self._restore(gh, "--apply")
        gh.post.assert_not_called()
        self.assertIn("still_present", out)

    def test_apply_refuses_while_the_app_switch_is_on(self):
        with override_settings(GITHUB_APP_ENABLED=True):
            with mock.patch(GITHUB) as gh:
                with self.assertRaises(CommandError):
                    self._restore(gh, "--apply")
        gh.post.assert_not_called()

    def test_a_refusal_leaves_the_row_and_fails_the_run(self):
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(404)
            gh.post.return_value = _resp(403)
            with self.assertRaises(CommandError):
                self._restore(gh, "--apply")
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.hook_id, 99)

    def test_a_second_hook_with_our_url_is_not_a_failure(self):
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(404)
            gh.post.return_value = _resp(422)
            out, _ = self._restore(gh, "--apply")
        self.assertIn("already_hooked", out)

    def test_restore_after_apply_round_trips(self):
        with override_settings(GITHUB_APP_ENABLED=True):
            inst = self._installation()
            self._link(inst, TeamMaster.objects.get(team_name="Acme"), self.user)
            GithubWebhookRegistration.objects.filter(pk=self.reg.pk).update(
                accepts_shared_secret=True
            )
            with mock.patch(GITHUB) as gh:
                gh.get.return_value = _resp(200, _hook_body())
                gh.delete.return_value = _resp(204)
                self._run("--apply")
        with mock.patch(GITHUB) as gh:
            gh.get.return_value = _resp(404)
            gh.post.return_value = _resp(201, {"id": 7})
            self._restore(gh, "--apply")
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.hook_id, 7)


def _legacy_row(user):
    return ConnectedAccount.objects.get(user=user, provider="github")


class RevokeBase(Base):
    def _run_revoke(self, *args):
        out, err = StringIO(), StringIO()
        call_command("revoke_legacy_github_grants", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()


REVOKE = "origin.services.oauth.revoke.requests.delete"


@override_settings(GITHUB_OAUTH_CLIENT_ID="Iv1.oauthapp", GITHUB_OAUTH_CLIENT_SECRET="oauth-secret")
class RevokeGrantTests(RevokeBase):
    def setUp(self):
        self.moved = self._user("moved")
        self._legacy_account(self.moved)
        self._app_account(self.moved)

    def test_default_run_reports_and_revokes_nothing(self):
        before = _legacy_row(self.moved).access_token_encrypted
        with mock.patch(REVOKE) as delete:
            out, _ = self._run_revoke()
        delete.assert_not_called()
        self.assertIn("1 legacy grant(s) eligible", out)
        self.assertIn("Dry run: nothing was changed", out)
        self.assertEqual(_legacy_row(self.moved).access_token_encrypted, before)

    def test_apply_revokes_and_clears_the_token_but_keeps_the_identity_row(self):
        with (
            mock.patch(REVOKE) as delete,
            self.assertLogs("origin.services.oauth.revoke", level="INFO") as logs,
        ):
            delete.return_value = _resp(204)
            out, err = self._run_revoke("--apply")
        delete.assert_called_once()
        self.assertEqual(delete.call_args.kwargs["json"], {"access_token": LEGACY_TOKEN})
        self.assertEqual(delete.call_args.kwargs["auth"], ("Iv1.oauthapp", "oauth-secret"))
        self.assertIn("revoked 1, already invalid 0, not done 0 of 1", out)
        row = _legacy_row(self.moved)
        self.assertEqual(crypto.decrypt(row.access_token_encrypted), "")
        self.assertIsNone(row.refresh_token_encrypted)
        self.assertEqual(row.scopes, ["read:user", "user:email"])
        self.assertTrue(
            ConnectedAccount.objects.filter(user=self.moved, provider="github_app").exists()
        )
        for text in (out, err, *logs.output):
            self.assertNotIn(LEGACY_TOKEN, text)
            self.assertNotIn(str(self.moved.id), text)

    def test_a_grant_github_no_longer_knows_counts_as_done(self):
        with mock.patch(REVOKE) as delete:
            delete.return_value = _resp(404)
            out, _ = self._run_revoke("--apply")
        self.assertIn("already invalid 1", out)
        self.assertEqual(_legacy_row(self.moved).scopes, ["read:user", "user:email"])

    def test_second_run_is_a_no_op(self):
        with mock.patch(REVOKE) as delete:
            delete.return_value = _resp(204)
            self._run_revoke("--apply")
        with mock.patch(REVOKE) as delete:
            out, _ = self._run_revoke("--apply")
        delete.assert_not_called()
        self.assertIn("0 legacy grant(s) eligible", out)

    def test_a_failure_leaves_the_row_and_fails_the_run(self):
        before = _legacy_row(self.moved).access_token_encrypted
        with mock.patch(REVOKE) as delete:
            delete.return_value = _resp(500)
            with self.assertRaises(CommandError) as ctx:
                self._run_revoke("--apply")
        self.assertIn("1 grant(s) were not revoked", str(ctx.exception))
        row = _legacy_row(self.moved)
        self.assertEqual(row.access_token_encrypted, before)
        self.assertIn("repo", row.scopes)

    def test_people_who_have_not_moved_are_left_alone_unless_asked(self):
        stay = self._user("stay")
        self._legacy_account(stay)
        with mock.patch(REVOKE) as delete:
            delete.return_value = _resp(204)
            out, _ = self._run_revoke("--apply")
        self.assertEqual(delete.call_count, 1)
        self.assertIn("1 with no GitHub App connection (not touched)", out)
        self.assertIn("repo", _legacy_row(stay).scopes)
        with mock.patch(REVOKE) as delete:
            delete.return_value = _resp(204)
            self._run_revoke("--apply", "--include-unmoved")
        self.assertEqual(delete.call_count, 1)
        self.assertNotIn("repo", _legacy_row(stay).scopes)

    def test_a_sign_in_only_token_has_no_repo_grant_to_revoke(self):
        signin = self._user("signin")
        self._legacy_account(signin, scopes=("read:user", "user:email"))
        self._app_account(signin)
        with mock.patch(REVOKE) as delete:
            delete.return_value = _resp(204)
            self._run_revoke("--apply")
        self.assertEqual(delete.call_count, 1)  # only `moved`

    def test_a_registrar_of_a_live_hook_is_held_back_unless_asked(self):
        self._registration("acme", "api", self.moved, connect=False)
        with mock.patch(REVOKE) as delete:
            out, _ = self._run_revoke("--apply")
        delete.assert_not_called()
        self.assertIn("0 legacy grant(s) eligible; 1 held back", out)
        self.assertIn("repo", _legacy_row(self.moved).scopes)
        with mock.patch(REVOKE) as delete:
            delete.return_value = _resp(204)
            self._run_revoke("--apply", "--include-registrars")
        delete.assert_called_once()

    def test_limit_revokes_a_first_few(self):
        other = self._user("moved2")
        self._legacy_account(other)
        self._app_account(other)
        with mock.patch(REVOKE) as delete:
            delete.return_value = _resp(204)
            out, _ = self._run_revoke("--apply", "--limit", "1")
        self.assertEqual(delete.call_count, 1)
        self.assertIn("2 legacy grant(s) eligible, 1 in this run", out)
        with mock.patch(REVOKE) as delete:
            delete.return_value = _resp(204)
            self._run_revoke("--apply", "--limit", "1")
        self.assertEqual(delete.call_count, 1)

    def test_a_token_stored_meanwhile_is_not_erased(self):
        stale = _legacy_row(self.moved)
        ConnectedAccount.objects.filter(pk=stale.pk).update(
            access_token_encrypted=crypto.encrypt("gho_new-sign-in-token")
        )
        with mock.patch(REVOKE) as delete:
            delete.return_value = _resp(204)
            outcome = mig.revoke_legacy_grant(stale)
        self.assertEqual(outcome, mig.STALE)
        self.assertEqual(
            crypto.decrypt(_legacy_row(self.moved).access_token_encrypted), "gho_new-sign-in-token"
        )

    def test_an_unreadable_token_is_skipped_not_fatal(self):
        ConnectedAccount.objects.filter(user=self.moved, provider="github").update(
            access_token_encrypted="not-a-fernet-token"
        )
        with mock.patch(REVOKE) as delete:
            with self.assertRaises(CommandError):
                self._run_revoke("--apply")
        delete.assert_not_called()

    def test_missing_oauth_app_credentials_refuse_apply(self):
        with override_settings(GITHUB_OAUTH_CLIENT_SECRET=""):
            with mock.patch(REVOKE) as delete:
                with self.assertRaises(CommandError):
                    self._run_revoke("--apply")
        delete.assert_not_called()

    def test_limit_below_one_is_refused(self):
        with self.assertRaises(CommandError):
            self._run_revoke("--limit", "0")


class RepoScopeSwitchTests(TestCase):
    """`GITHUB_CONNECT_REPO_SCOPE_REMOVED`: off by default; on, Connect GitHub never asks for `repo`."""

    def _scopes(self, intent):
        url = GitHubOAuthProvider().authorize_url(
            state="s", intent=intent, redirect_uri="https://api.example.test/cb"
        )
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(url).query)["scope"][0].split()

    def test_off_by_default_connect_asks_for_repo_and_login_does_not(self):
        self.assertEqual(self._scopes("connect"), ["read:user", "user:email", "repo"])
        self.assertEqual(self._scopes("login"), ["read:user", "user:email"])

    @override_settings(GITHUB_CONNECT_REPO_SCOPE_REMOVED=True)
    def test_on_connect_asks_for_sign_in_scopes_only(self):
        self.assertEqual(self._scopes("connect"), ["read:user", "user:email"])
        self.assertEqual(self._scopes("login"), ["read:user", "user:email"])

    @override_settings(GITHUB_CONNECT_REPO_SCOPE_REMOVED=True, GITHUB_APP_ENABLED=False)
    def test_the_switch_does_not_depend_on_the_app_being_on(self):
        # A rollback of the App switch must not bring the broad scope back.
        self.assertNotIn("repo", self._scopes("connect"))
