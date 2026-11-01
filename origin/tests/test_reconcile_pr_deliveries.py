"""`manage.py reconcile_pr_deliveries` (MIP-861).

A `pull_request` event that arrives while the API redeploys is answered 500
by the platform and GitHub never resends it. The delivery row then says
`open` for a PR that has merged, the task never auto-closes, and the stale
row holds every later merge of that task open too. The command asks GitHub
about the PRs Genos still believes are open and applies what the webhook
would have.

What's pinned:

1. One pass closes the task for a merged PR, attributed to the PR, and a
   second pass does nothing.
2. It only ever acts on a transition: `open` rows are read; `merged`
   (a task somebody reopened) and `unknown` (backfilled) rows are not.
3. The webhook's own rules still hold: tenant scope, the assignee opt-in,
   and the wait for every linked PR, including two lost PRs.
4. A 401 / 403 / 429 / outage stops that repo for the run, a dead registrar
   token is a WARNING, and only a real fault reds the run.
"""

from __future__ import annotations

from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_delivery_models import (
    TaskDelivery,
    TaskDeliverySource,
    TaskDeliveryState,
)
from origin.models.task.task_models import TaskMaster
from origin.services.oauth.tokens import ReauthRequired
from origin.services.task_delivery import record_delivery

User = get_user_model()

CMD = "origin.management.commands.reconcile_pr_deliveries"
HEADERS = {"X-Test": "1"}


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _pr(number, *, merged=False, state=None, ref=None):
    """The REST pull object, as GET /repos/{o}/{r}/pulls/{n} returns it."""
    state = state or ("closed" if merged else "open")
    return {
        "number": number,
        "state": state,
        "merged": merged,
        "merged_at": "2026-11-01T10:15:22Z" if merged else None,
        "title": "A PR",
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "head": {"ref": ref or f"feature/HK-42-part-{number}", "sha": "abc"},
    }


class _GitHub:
    """`requests.get` stand-in: PR number -> response (or exception)."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append(url)
        number = int(url.rsplit("/", 1)[1])
        answer = self.answers[number]
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, _Resp):
            return answer
        return _Resp(200, answer)


class ReconcileCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="reconciler",
            email="reconcile@test.com",
            password="testpass123",
            is_email_verified=True,
            auto_close_on_pr_merge=True,
        )
        self.team = TeamMaster.objects.create(
            team_name="Rec Team", team_email="rec@team.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Rec Project",
            owner=self.user,
            project_system_user=self.user,
            code="HK",
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.registration = GithubWebhookRegistration.objects.create(
            owner="owner", repo="repo", hook_id=1, registered_by=self.user
        )
        self.task = self._task(42)

    def _task(self, number, *, team=None, project=None, assignee=None):
        return TaskMaster.objects.create(
            team=team or self.team,
            project=project or self.project,
            assignee=assignee or self.user,
            reporter=self.user,
            title="Task",
            status="Open",
            project_task_number=number,
        )

    def _link(self, task, number, *, state=TaskDeliveryState.OPEN, ref=None, repo="repo"):
        row, _ = record_delivery(
            task=task,
            owner="owner",
            repo=repo,
            branch=ref or f"feature/HK-42-part-{number}",
            pr_number=number,
            state=state,
            source=TaskDeliverySource.WEBHOOK,
        )
        return row

    def _run(self, github, *args, headers=HEADERS):
        with (
            mock.patch(f"{CMD}.requests.get", github),
            mock.patch(f"{CMD}.hook_admin_headers", return_value=headers),
        ):
            call_command("reconcile_pr_deliveries", *args)

    def _status(self, task=None):
        task = task or self.task
        task.refresh_from_db()
        return task.status


class TestReconcileClosesWhatTheWebhookMissed(ReconcileCase):
    def test_a_merged_pr_closes_the_task_once_and_a_second_pass_does_nothing(self):
        row = self._link(self.task, 42)
        github = _GitHub({42: _pr(42, merged=True)})

        self._run(github)

        self.assertEqual(self._status(), "Closed")
        row.refresh_from_db()
        self.assertEqual(row.state, TaskDeliveryState.MERGED)
        self.assertIsNotNone(row.merged_at)
        # Attributed to the PR, as the webhook's close is.
        activity = TaskActivity.objects.get(task=self.task, action_type="status_changed")
        self.assertEqual(activity.metadata.get("prUrl"), "https://github.com/owner/repo/pull/42")
        self.assertTrue(activity.metadata.get("closedByPrMerge"))
        self.assertEqual(len(github.calls), 1)

        # Second pass: nothing is open any more, so GitHub is not even asked.
        self._run(github)
        self.assertEqual(len(github.calls), 1)
        self.assertEqual(
            TaskActivity.objects.filter(task=self.task, action_type="status_changed").count(), 1
        )

    def test_two_lost_prs_close_the_task_after_the_last_of_them(self):
        a = self._link(self.task, 42)
        b = self._link(self.task, 43)
        github = _GitHub({42: _pr(42, merged=True), 43: _pr(43, merged=True)})

        self._run(github)

        self.assertEqual(self._status(), "Closed")
        for row in (a, b):
            row.refresh_from_db()
            self.assertEqual(row.state, TaskDeliveryState.MERGED)

    def test_a_lost_merge_does_not_hold_when_the_other_pr_is_still_open(self):
        # The waiting rule is the webhook's: not the first merge, the last.
        self._link(self.task, 42)
        still_open = self._link(self.task, 43)
        github = _GitHub({42: _pr(42, merged=True), 43: _pr(43)})

        self._run(github)

        self.assertEqual(self._status(), "Open")
        still_open.refresh_from_db()
        self.assertEqual(still_open.state, TaskDeliveryState.OPEN)

    def test_a_pr_closed_without_merging_is_recorded_and_closes_nothing(self):
        row = self._link(self.task, 42)
        self._run(_GitHub({42: _pr(42, state="closed")}))
        self.assertEqual(self._status(), "Open")
        row.refresh_from_db()
        self.assertEqual(row.state, TaskDeliveryState.CLOSED)

    def test_a_pr_still_open_on_github_is_left_alone(self):
        row = self._link(self.task, 42)
        self._run(_GitHub({42: _pr(42)}))
        self.assertEqual(self._status(), "Open")
        row.refresh_from_db()
        self.assertEqual(row.state, TaskDeliveryState.OPEN)

    def test_dry_run_reports_and_changes_nothing(self):
        row = self._link(self.task, 42)
        self._run(_GitHub({42: _pr(42, merged=True)}), "--dry-run")
        self.assertEqual(self._status(), "Open")
        row.refresh_from_db()
        self.assertEqual(row.state, TaskDeliveryState.OPEN)


class TestReconcileOnlyActsOnATransition(ReconcileCase):
    def test_a_reopened_task_is_not_closed_again(self):
        # The PR merged and the webhook recorded it; somebody reopened the
        # task. A "close whatever is merged" sweep would undo that every run.
        self._link(self.task, 42, state=TaskDeliveryState.MERGED)
        github = _GitHub({42: _pr(42, merged=True)})

        self._run(github)

        self.assertEqual(self._status(), "Open")
        self.assertEqual(github.calls, [])

    def test_unknown_rows_are_ignored(self):
        # Backfilled rows say nothing about the PR; closing on them would
        # mass-close old tasks.
        row = self._link(self.task, 42, state=TaskDeliveryState.UNKNOWN)
        github = _GitHub({42: _pr(42, merged=True)})

        self._run(github)

        self.assertEqual(self._status(), "Open")
        self.assertEqual(github.calls, [])
        row.refresh_from_db()
        self.assertEqual(row.state, TaskDeliveryState.UNKNOWN)

    def test_a_branch_with_no_pr_is_ignored(self):
        record_delivery(
            task=self.task,
            owner="owner",
            repo="repo",
            branch="feature/HK-42-x",
            source=TaskDeliverySource.MANUAL,
        )
        github = _GitHub({})
        self._run(github)
        self.assertEqual(github.calls, [])


class TestReconcileKeepsTheWebhooksRules(ReconcileCase):
    def test_another_teams_task_is_not_touched(self):
        # Same project code, same repo: the registrar's teams are the only
        # scope, as for a live event (`_repo_team_ids`).
        stranger = User.objects.create_user(
            username="stranger",
            email="stranger@test.com",
            password="testpass123",
            is_email_verified=True,
            auto_close_on_pr_merge=True,
        )
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other@team.com", owner=stranger
        )
        other_project = ProjectMaster.objects.create(
            team=other_team,
            project_name="Other Project",
            owner=stranger,
            project_system_user=stranger,
            code="HK",
        )
        other_task = self._task(42, team=other_team, project=other_project, assignee=stranger)
        other_row = self._link(other_task, 42)
        own_row = self._link(self.task, 42)
        github = _GitHub({42: _pr(42, merged=True)})

        self._run(github)

        self.assertEqual(self._status(), "Closed")
        self.assertEqual(self._status(other_task), "Open")
        other_row.refresh_from_db()
        self.assertEqual(other_row.state, TaskDeliveryState.OPEN)
        own_row.refresh_from_db()
        self.assertEqual(own_row.state, TaskDeliveryState.MERGED)

    def test_an_assignee_who_has_not_opted_in_keeps_the_task_open(self):
        self.user.auto_close_on_pr_merge = False
        self.user.save(update_fields=["auto_close_on_pr_merge"])
        row = self._link(self.task, 42)

        self._run(_GitHub({42: _pr(42, merged=True)}))

        self.assertEqual(self._status(), "Open")
        row.refresh_from_db()
        self.assertEqual(row.state, TaskDeliveryState.MERGED)

    def test_a_repo_with_no_registrar_in_scope_is_skipped(self):
        # `registered_by` is SET_NULL when the user goes: fail closed.
        self._link(self.task, 42)
        GithubWebhookRegistration.objects.update(registered_by=None)
        github = _GitHub({42: _pr(42, merged=True)})

        self._run(github)

        self.assertEqual(self._status(), "Open")
        self.assertEqual(github.calls, [])


class TestReconcileStopsWhereGitHubSaysStop(ReconcileCase):
    def _two_open_prs(self):
        self._link(self.task, 42)
        self._link(self.task, 43)

    def test_403_stops_that_repo_and_does_not_fail_the_run(self):
        self._two_open_prs()
        github = _GitHub({42: _Resp(403, {}), 43: _pr(43, merged=True)})

        with self.assertLogs("origin", level="WARNING") as logs:
            self._run(github)  # no CommandError: not an ERROR

        self.assertEqual(len(github.calls), 1, "the second PR must not be asked about")
        self.assertTrue(any("403" in line for line in logs.output))
        self.assertEqual(self._status(), "Open")

    def test_429_stops_that_repo(self):
        self._two_open_prs()
        github = _GitHub({42: _Resp(429, {}), 43: _pr(43)})
        with self.assertLogs("origin", level="WARNING"):
            self._run(github)
        self.assertEqual(len(github.calls), 1)

    def test_a_stopped_repo_does_not_stop_the_next(self):
        self._two_open_prs()
        GithubWebhookRegistration.objects.create(
            owner="owner", repo="other", hook_id=2, registered_by=self.user
        )
        other_task = self._task(7)
        row = self._link(other_task, 7, repo="other", ref="feature/HK-7-x")
        calls = []

        def get(url, **kwargs):
            calls.append(url)
            if "/repo/" in url:
                return _Resp(403, {})
            return _Resp(
                200,
                {**_pr(7, merged=True, ref="feature/HK-7-x"), "html_url": "x"},
            )

        with (
            mock.patch(f"{CMD}.requests.get", get),
            mock.patch(f"{CMD}.hook_admin_headers", return_value=HEADERS),
            self.assertLogs("origin", level="WARNING"),
        ):
            call_command("reconcile_pr_deliveries")

        self.assertEqual(self._status(other_task), "Closed")
        row.refresh_from_db()
        self.assertEqual(row.state, TaskDeliveryState.MERGED)
        self.assertEqual(sum("/repo/" in c for c in calls), 1)

    def test_a_pr_that_is_gone_is_skipped_and_the_rest_go_on(self):
        self._two_open_prs()
        github = _GitHub({42: _Resp(404, {}), 43: _pr(43, merged=True)})
        with self.assertLogs("origin", level="WARNING"):
            self._run(github)
        self.assertEqual(len(github.calls), 2)

    def test_github_unreachable_stops_the_repo_without_failing_the_run(self):
        self._two_open_prs()
        github = _GitHub({42: requests.ConnectionError("boom"), 43: _pr(43)})
        with self.assertLogs("origin", level="WARNING"):
            self._run(github)
        self.assertEqual(len(github.calls), 1)

    def test_a_dead_registrar_token_is_a_warning_not_an_error(self):
        # Through the real `hook_admin_headers`: one revoked token must not
        # red the cron on every run (CronCommand fails the run on ERROR).
        self._link(self.task, 42)
        ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="1",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        github = _GitHub({42: _pr(42, merged=True)})

        with (
            mock.patch(f"{CMD}.requests.get", github),
            mock.patch(
                "origin.services.github_webhooks.get_valid_access_token",
                side_effect=lambda account: (_ for _ in ()).throw(ReauthRequired(account)),
            ),
            self.assertLogs("origin", level="WARNING") as logs,
        ):
            call_command("reconcile_pr_deliveries")  # does not raise

        self.assertEqual(github.calls, [])
        self.assertFalse([line for line in logs.output if line.startswith("ERROR")])
        self.assertTrue(any("not checked" in line for line in logs.output))
        self.assertEqual(self._status(), "Open")

    def test_a_registrar_with_no_github_connection_is_a_warning_too(self):
        self._link(self.task, 42)
        github = _GitHub({42: _pr(42, merged=True)})
        with (
            mock.patch(f"{CMD}.requests.get", github),
            self.assertLogs("origin", level="WARNING"),
        ):
            call_command("reconcile_pr_deliveries")
        self.assertEqual(github.calls, [])


class TestReconcileFailsLoudOnARealFault(ReconcileCase):
    def test_a_bug_applying_one_pr_reds_the_run_but_not_the_other_pr(self):
        self._link(self.task, 42)
        other = self._task(43)
        other_row = self._link(other, 43, ref="feature/HK-43-x")
        github = _GitHub(
            {42: _pr(42, merged=True), 43: _pr(43, merged=True, ref="feature/HK-43-x")}
        )
        from origin.views.common.github_views import apply_pull_request as real

        def flaky(owner, repo, pr, action, team_ids):
            if pr["number"] == 42:
                raise RuntimeError("boom")
            return real(owner, repo, pr, action, team_ids)

        with (
            mock.patch(f"{CMD}.apply_pull_request", flaky),
            self.assertRaises(CommandError),
        ):
            self._run(github)

        self.assertEqual(self._status(other), "Closed")
        other_row.refresh_from_db()
        self.assertEqual(other_row.state, TaskDeliveryState.MERGED)
        self.assertEqual(self._status(), "Open")
        self.assertEqual(TaskDelivery.objects.filter(state=TaskDeliveryState.OPEN).count(), 1)
