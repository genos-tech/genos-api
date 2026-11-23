"""The App registration check (2026-11-23: the production App was not subscribed to "Pull request").

GitHub sends only the events an App subscribed to and never says one is missing, so with that box
unticked the webhook answered 200 to every check_run / status and no PR was linked to its task.
Pinned here:

  * `REQUIRED_EVENTS` is exactly the set of events the webhook handles, so the two cannot drift;
  * `registration_problems` names the missing events, the permissions below the needed level and the
    extra permissions beyond the design;
  * `fetch_app_registration` is one read-only GET /app that works with the switch off, returns only
    event and permission names, and fails with a status or a fixed reason, never GitHub's answer;
  * `check_github_app_registration` exits non-zero naming what is missing, and warns when
    check_run / status arrive but no pull_request does;
  * the other suspects of that incident stay cleared: an installation on "all repositories" with no
    repository rows still covers its account's repos, and a DRAFT PR whose body says "Part of MIP-n"
    is linked through the App endpoint.

All GitHub traffic is mocked.
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from origin.models.common.github_app_models import (
    GithubAppInstallationRepository,
    GithubAppWebhookDelivery,
)
from origin.models.task.task_delivery_models import TaskDelivery
from origin.services import github_app
from origin.services import github_app_webhook as hook
from origin.tests import test_github_app_webhook as w

GOOD_PERMISSIONS = {
    "metadata": "read",
    "pull_requests": "read",
    "checks": "read",
    "statuses": "read",
    "issues": "read",
    "members": "read",
}
GOOD_EVENTS = sorted(github_app.REQUIRED_EVENTS)


def _resp(status=200, body=None):
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.json.return_value = body if body is not None else {}
    return resp


class TestRequiredEvents(SimpleTestCase):
    def test_required_events_are_exactly_the_events_the_webhook_handles(self):
        self.assertEqual(github_app.REQUIRED_EVENTS, hook.PR_EVENTS | hook.CI_EVENTS)

    def test_the_incident_set_is_reported_missing_pull_request(self):
        incident = {
            "events": [
                "check_run",
                "issue_comment",
                "pull_request_review",
                "pull_request_review_comment",
                "pull_request_review_thread",
                "status",
            ],
            "permissions": GOOD_PERMISSIONS,
        }
        problems = github_app.registration_problems(incident)
        self.assertEqual(problems["missing_events"], ["pull_request"])
        self.assertEqual(problems["missing_permissions"], {})
        self.assertEqual(problems["extra_permissions"], {})


class TestRegistrationProblems(SimpleTestCase):
    def test_a_good_registration_has_no_problems(self):
        problems = github_app.registration_problems(
            {"events": GOOD_EVENTS, "permissions": GOOD_PERMISSIONS}
        )
        self.assertEqual(
            problems,
            {"missing_events": [], "missing_permissions": {}, "extra_permissions": {}},
        )

    def test_a_missing_permission_is_named_with_the_level_needed(self):
        perms = {k: v for k, v in GOOD_PERMISSIONS.items() if k != "members"}
        problems = github_app.registration_problems({"events": GOOD_EVENTS, "permissions": perms})
        self.assertEqual(problems["missing_permissions"], {"members": "read"})

    def test_write_where_read_is_needed_is_enough_but_reported_as_extra(self):
        perms = {**GOOD_PERMISSIONS, "issues": "write", "contents": "read"}
        problems = github_app.registration_problems({"events": GOOD_EVENTS, "permissions": perms})
        self.assertEqual(problems["missing_permissions"], {})
        self.assertEqual(problems["extra_permissions"], {"contents": "read", "issues": "write"})

    def test_extra_events_are_harmless_and_not_listed(self):
        problems = github_app.registration_problems(
            {
                "events": [*GOOD_EVENTS, "pull_request_review_thread"],
                "permissions": GOOD_PERMISSIONS,
            }
        )
        self.assertEqual(problems["missing_events"], [])


class TestFetchAppRegistration(SimpleTestCase):
    """Works with the switch OFF (it checks a registration before the switch goes on)."""

    @override_settings(GITHUB_APP_ENABLED=False)
    def test_reads_events_and_permissions_with_the_switch_off(self):
        body = {
            "events": ["status", "pull_request"],
            "permissions": {"metadata": "read"},
            "name": "ignored",
            "owner": {"login": "ignored"},
        }
        with (
            mock.patch.object(github_app, "_sign_jwt", return_value="jwt-SECRET"),
            mock.patch.object(github_app.requests, "get", return_value=_resp(200, body)) as get,
        ):
            reg = github_app.fetch_app_registration()
        self.assertEqual(
            reg, {"events": ["pull_request", "status"], "permissions": {"metadata": "read"}}
        )
        args, kwargs = get.call_args
        self.assertEqual(args[0], "https://api.github.com/app")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer jwt-SECRET")

    def test_a_non_200_names_the_status_only(self):
        with (
            mock.patch.object(github_app, "_sign_jwt", return_value="jwt-SECRET"),
            mock.patch.object(
                github_app.requests, "get", return_value=_resp(401, {"message": "Bad credentials"})
            ),
        ):
            with self.assertRaises(github_app.GithubAppError) as ctx:
                github_app.fetch_app_registration()
        self.assertEqual(str(ctx.exception), "GET /app answered 401")

    def test_a_network_error_names_its_type_only(self):
        with (
            mock.patch.object(github_app, "_sign_jwt", return_value="jwt-SECRET"),
            mock.patch.object(
                github_app.requests, "get", side_effect=requests.ConnectionError("host jwt-SECRET")
            ),
        ):
            with self.assertRaises(github_app.GithubAppError) as ctx:
                github_app.fetch_app_registration()
        self.assertNotIn("SECRET", str(ctx.exception))
        self.assertIn("ConnectionError", str(ctx.exception))

    @override_settings(GITHUB_APP_PRIVATE_KEY="not a key", GITHUB_APP_CLIENT_ID="Iv1.x")
    def test_a_bad_private_key_is_a_fixed_reason_without_the_key(self):
        with self.assertRaises(github_app.GithubAppError) as ctx:
            github_app.fetch_app_registration()
        self.assertNotIn("not a key", str(ctx.exception))
        self.assertIn("could not sign", str(ctx.exception))

    def test_an_unexpected_shape_is_refused(self):
        with (
            mock.patch.object(github_app, "_sign_jwt", return_value="j"),
            mock.patch.object(github_app.requests, "get", return_value=_resp(200, ["x"])),
        ):
            with self.assertRaises(github_app.GithubAppError):
                github_app.fetch_app_registration()


class TestCommand(TestCase):
    def _run(self, registration, **opts):
        out = StringIO()
        with mock.patch.object(github_app, "fetch_app_registration", return_value=registration):
            call_command("check_github_app_registration", stdout=out, **opts)
        return out.getvalue()

    def test_passes_on_a_complete_registration(self):
        out = self._run({"events": GOOD_EVENTS, "permissions": GOOD_PERMISSIONS})
        self.assertIn("has every event and permission", out)

    def test_fails_naming_the_missing_event(self):
        events = [e for e in GOOD_EVENTS if e != "pull_request"]
        out = StringIO()
        with mock.patch.object(
            github_app,
            "fetch_app_registration",
            return_value={"events": events, "permissions": GOOD_PERMISSIONS},
        ):
            with self.assertRaises(CommandError):
                call_command("check_github_app_registration", stdout=out)
        self.assertIn("MISSING EVENTS", out.getvalue())
        self.assertIn("pull_request", out.getvalue())

    def test_fails_on_a_missing_permission(self):
        perms = {k: v for k, v in GOOD_PERMISSIONS.items() if k != "pull_requests"}
        with mock.patch.object(
            github_app,
            "fetch_app_registration",
            return_value={"events": GOOD_EVENTS, "permissions": perms},
        ):
            with self.assertRaises(CommandError):
                call_command("check_github_app_registration", stdout=StringIO())

    def test_extra_permission_warns_but_passes(self):
        out = self._run(
            {"events": GOOD_EVENTS, "permissions": {**GOOD_PERMISSIONS, "contents": "read"}}
        )
        self.assertIn("More permissions than the design allows", out)
        self.assertIn("contents=read", out)

    def test_github_unreachable_is_a_command_error_with_the_reason(self):
        with mock.patch.object(
            github_app,
            "fetch_app_registration",
            side_effect=github_app.GithubAppError("GET /app answered 401"),
        ):
            with self.assertRaises(CommandError) as ctx:
                call_command("check_github_app_registration", stdout=StringIO())
        self.assertIn("GET /app answered 401", str(ctx.exception))

    def test_warns_when_check_runs_arrive_but_no_pull_request(self):
        GithubAppWebhookDelivery.objects.create(delivery_id="aaaaaaaa-0001", event="check_run")
        GithubAppWebhookDelivery.objects.create(delivery_id="aaaaaaaa-0002", event="status")
        out = self._run({"events": GOOD_EVENTS, "permissions": GOOD_PERMISSIONS})
        self.assertIn("check_run=1, status=1", out)
        self.assertIn("no pull_request event did", out)

    def test_no_warning_once_a_pull_request_has_arrived(self):
        GithubAppWebhookDelivery.objects.create(delivery_id="aaaaaaaa-0001", event="check_run")
        GithubAppWebhookDelivery.objects.create(delivery_id="aaaaaaaa-0002", event="pull_request")
        out = self._run({"events": GOOD_EVENTS, "permissions": GOOD_PERMISSIONS})
        self.assertNotIn("no pull_request event did", out)

    def test_the_command_writes_nothing(self):
        before = GithubAppWebhookDelivery.objects.count()
        self._run({"events": GOOD_EVENTS, "permissions": GOOD_PERMISSIONS})
        self.assertEqual(GithubAppWebhookDelivery.objects.count(), before)


class TestIncidentSuspectsStayCleared(w.AppWebhookBase):
    """The production installation was on "all repositories" of the organization (7 repo rows);
    a draft PR with "Part of MIP-n" in its body is what was opened. Both work."""

    def _make_installation_all_without_repo_rows(self):
        GithubAppInstallationRepository.objects.filter(installation=self.installation).delete()
        self.installation.repository_selection = "all"
        self.installation.save(update_fields=["repository_selection"])

    def test_draft_pr_with_part_of_in_the_body_is_linked_on_an_all_repositories_installation(self):
        self._make_installation_all_without_repo_rows()
        payload = self._pr("opened", ref="feature/no-id")
        payload["pull_request"]["draft"] = True
        payload["pull_request"]["body"] = "Part of HK-42"
        resp = self._post(payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(TaskDelivery.objects.filter(task=self.task).count(), 1)

    def test_a_pr_in_a_repo_of_another_account_is_still_not_covered(self):
        self._make_installation_all_without_repo_rows()
        payload = self._pr("opened", owner="someone-else", repo="repo")
        payload["pull_request"]["body"] = "Part of HK-42"
        resp = self._post(payload)
        self.assertEqual(resp.data["detail"], "repo_not_covered")
        self.assertFalse(TaskDelivery.objects.exists())
