"""A repo hooked by hand can be registered, and unknown repos are visible (MIP-755).

A webhook added in GitHub's settings answers 200 and links nothing: the
registration row is what says which teams its events may write into, and only
`ensure_repo_webhook` creates it. So:

  * a VERIFIED delivery from a repo with no registration is remembered
    (`GithubUnregisteredDelivery`), and still writes nothing: the tenant guard
    in `_repo_team_ids` is untouched;
  * `POST /github/register-repo/` registers (or adopts) the hook with the
    caller's own GitHub connection and says WHY when it cannot;
  * `GET /github/unregistered-repos/` lists those repos, but only the ones the
    caller's own GitHub token can read: the record has no tenant, so showing it
    unfiltered would leak one customer's private repo names to another.

GitHub's HTTP API is mocked; nothing here is networked.
"""

import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import (
    ConnectedAccount,
    GithubUnregisteredDelivery,
    GithubWebhookRegistration,
)
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services.github_webhooks import record_unregistered_delivery

User = get_user_model()
SECRET = "test-secret-1234567890"


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _resp(status_code=200, body=None):
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.ok = 200 <= status_code < 300
    r.json.return_value = body if body is not None else {}
    r.text = ""
    return r


def _pr_payload(full_name="acme/rocket", *, merged=True, head_ref="HK-1-fix"):
    url = f"https://github.com/{full_name}/pull/7"
    return {
        "action": "closed",
        "pull_request": {
            "html_url": url,
            "merged": merged,
            "number": 7,
            "title": "t",
            "state": "closed",
            "head": {"sha": "abc", "ref": head_ref},
            "base": {"repo": {"full_name": full_name}, "ref": "main"},
        },
        "repository": {"full_name": full_name},
    }


@override_settings(GITHUB_WEBHOOK_SECRET=SECRET)
class UnregisteredDeliveryRecordingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="owner",
            email="o@test.com",
            password="x",
            is_email_verified=True,
            auto_close_on_pr_merge=True,
        )
        self.team = TeamMaster.objects.create(team_name="T", team_email="t@t.com", owner=self.user)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            owner=self.user,
            project_system_user=self.user,
            code="HK",
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _post(self, payload, *, event="pull_request", secret=SECRET):
        body = json.dumps(payload).encode()
        return self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body, secret),
            HTTP_X_GITHUB_EVENT=event,
        )

    def test_a_verified_delivery_from_an_unregistered_repo_is_remembered_and_counted(self):
        self.assertEqual(self._post(_pr_payload()).status_code, 200)
        self.assertEqual(self._post(_pr_payload()).status_code, 200)
        (row,) = GithubUnregisteredDelivery.objects.all()
        self.assertEqual((row.owner, row.repo, row.event_count), ("acme", "rocket", 2))
        self.assertEqual(row.last_event, "pull_request")

    def test_the_same_repo_in_another_case_is_one_row(self):
        self._post(_pr_payload("acme/rocket"))
        self._post(_pr_payload("Acme/Rocket"))
        self.assertEqual(GithubUnregisteredDelivery.objects.count(), 1)
        self.assertEqual(GithubUnregisteredDelivery.objects.get().event_count, 2)

    def test_a_ping_counts_too_it_is_the_first_thing_a_hand_made_hook_sends(self):
        self._post({"zen": "hi", "repository": {"full_name": "acme/rocket"}}, event="ping")
        self.assertEqual(GithubUnregisteredDelivery.objects.get().last_event, "ping")

    def test_a_registered_repo_is_not_recorded(self):
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="rocket", hook_id=1, registered_by=self.user
        )
        self._post(_pr_payload())
        self.assertEqual(GithubUnregisteredDelivery.objects.count(), 0)

    def test_an_unverified_delivery_is_not_recorded(self):
        self.assertEqual(self._post(_pr_payload(), secret="wrong").status_code, 401)
        self.assertEqual(GithubUnregisteredDelivery.objects.count(), 0)

    def test_the_tenant_guard_is_unchanged_an_unregistered_repo_still_writes_nothing(self):
        task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title="t",
            status="Open",
            project_task_number=1,
        )
        self.assertEqual(task.display_id, "HK-1")
        res = self._post(_pr_payload(head_ref="HK-1-fix"))
        self.assertEqual(res.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")

    def test_odd_repo_names_are_not_recorded(self):
        for bad in ("a/b c", "../x", "a/..", "a/.", "my.org/x", "a/", "/b", "x" * 200 + "/y"):
            record_unregistered_delivery(*(bad.split("/", 1) + [""])[:2], "pull_request")
        self.assertEqual(GithubUnregisteredDelivery.objects.count(), 0)

    def test_the_table_is_bounded(self):
        GithubUnregisteredDelivery.objects.bulk_create(
            [
                GithubUnregisteredDelivery(
                    repo_key=f"o/r{i}", owner="o", repo=f"r{i}", ts_last_seen_at="2026-11-01T00:00Z"
                )
                for i in range(500)
            ]
        )
        record_unregistered_delivery("acme", "rocket", "pull_request")
        self.assertEqual(GithubUnregisteredDelivery.objects.count(), 500)
        # ...but a repo already in the table keeps counting.
        record_unregistered_delivery("o", "r1", "pull_request")
        self.assertEqual(GithubUnregisteredDelivery.objects.get(repo_key="o/r1").event_count, 2)

    def test_bookkeeping_failure_never_fails_the_webhook(self):
        with patch("origin.services.github_webhooks.GithubUnregisteredDelivery.objects") as objects:
            objects.filter.side_effect = RuntimeError("db down")
            self.assertEqual(self._post(_pr_payload()).status_code, 200)


@override_settings(GITHUB_WEBHOOK_SECRET=SECRET, BACKEND_BASE_URL="https://api.example.com")
class RegisterRepoEndpointTests(TestCase):
    URL = "/api/v2/github/register-repo/"

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="admin", email="a@test.com", password="x", is_email_verified=True
        )
        self.client.force_authenticate(self.user)
        self.account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="1",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )

    def _register(self, owner="acme", repo="rocket"):
        return self.client.post(self.URL, {"owner": owner, "repo": repo}, format="json")

    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    @patch("origin.services.github_webhooks.requests.post")
    def test_registers_the_repo_and_forgets_it_was_unknown(self, post, _token):
        post.return_value = _resp(201, {"id": 4242})
        record_unregistered_delivery("acme", "rocket", "pull_request")
        res = self._register()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["reason"], "created")
        reg = GithubWebhookRegistration.objects.get(owner="acme", repo="rocket")
        self.assertEqual((reg.hook_id, reg.registered_by), (4242, self.user))
        self.assertEqual(GithubUnregisteredDelivery.objects.count(), 0)

    @patch("origin.services.github_webhooks.requests.patch")
    @patch("origin.services.github_webhooks.requests.get")
    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    def test_adopts_a_hook_added_by_hand_and_fixes_its_events(self, _t, post, get, patch_):
        post.return_value = _resp(422)
        patch_.return_value = _resp(200)
        get.return_value = _resp(
            200,
            [
                {
                    "id": 333,
                    "events": ["pull_request"],  # the manual steps' old "only Pull requests"
                    "config": {"url": "https://api.example.com/api/v2/github/webhook/"},
                }
            ],
        )
        res = self._register()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["reason"], "adopted")
        self.assertEqual(GithubWebhookRegistration.objects.get().hook_id, 333)
        events_call = patch_.call_args_list[0]
        self.assertEqual(
            sorted(events_call.kwargs["json"]["events"]),
            ["issue_comment", "pull_request", "pull_request_review_comment"],
        )

    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    def test_each_failure_says_why(self, _token, post):
        cases = [
            (403, "no_admin", 403),
            (401, "no_admin", 403),
            (404, "repo_not_found", 404),
            (500, "github_error", 502),
        ]
        for github_status, reason, http in cases:
            with self.subTest(github=github_status):
                post.return_value = _resp(github_status)
                res = self._register()
                self.assertEqual((res.status_code, res.data["detail"]), (http, reason))
        self.assertEqual(GithubWebhookRegistration.objects.count(), 0)

    @patch("origin.services.github_webhooks.requests.get")
    @patch("origin.services.github_webhooks.requests.post")
    @patch("origin.services.github_webhooks.get_valid_access_token", return_value="ghp_xx")
    def test_a_422_with_no_hook_of_ours_is_reported_not_swallowed(self, _t, post, get):
        post.return_value = _resp(422)
        get.return_value = _resp(200, [{"id": 1, "config": {"url": "https://elsewhere/hook"}}])
        res = self._register()
        self.assertEqual((res.status_code, res.data["detail"]), (409, "adopt_failed"))

    def test_without_a_github_connection(self):
        self.account.delete()
        res = self._register()
        self.assertEqual((res.status_code, res.data["detail"]), (400, "not_connected"))

    def test_bad_names_are_refused_before_any_github_call(self):
        with patch("origin.services.github_webhooks.requests.post") as post:
            for owner, repo in (
                ("", "r"),
                ("o", ""),
                ("a b", "r"),
                ("o", "../x"),
                ("o", ".."),
                ("o", "."),
                ("..", "r"),
                ("my.org", "r"),
                ("o", "r" * 101),
            ):
                with self.subTest(owner=owner, repo=repo):
                    res = self._register(owner, repo)
                    self.assertEqual((res.status_code, res.data["detail"]), (400, "invalid_repo"))
            post.assert_not_called()

    def test_anonymous_is_refused(self):
        self.client.force_authenticate(None)
        self.assertEqual(self._register().status_code, 401)


class UnregisteredReposEndpointTests(TestCase):
    URL = "/api/v2/github/unregistered-repos/"

    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="viewer", email="v@test.com", password="x", is_email_verified=True
        )
        self.client.force_authenticate(self.user)
        self.account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="1",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        record_unregistered_delivery("mine", "app", "pull_request")
        record_unregistered_delivery("mine", "app", "pull_request")
        record_unregistered_delivery("someone-else", "secret-repo", "pull_request")

    def _github(self, readable):
        def fake(account, path, params=None):
            full = path.removeprefix("/repos/")
            if full in readable:
                return _resp(200, {"permissions": {"admin": readable[full]}})
            return _resp(404)

        return patch("origin.views.common.github_views._github_get", side_effect=fake)

    def test_lists_only_repos_the_callers_own_github_token_can_read(self):
        with self._github({"mine/app": True}):
            res = self.client.get(self.URL)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["connected"])
        self.assertEqual([(r["owner"], r["repo"]) for r in res.data["repos"]], [("mine", "app")])
        # Another customer's repo name is nowhere in the response.
        self.assertNotIn("secret-repo", json.dumps(res.data))

    def test_reports_count_last_event_and_whether_the_caller_can_administer_it(self):
        with self._github({"mine/app": False}):
            (row,) = self.client.get(self.URL).data["repos"]
        self.assertEqual((row["event_count"], row["last_event"]), (2, "pull_request"))
        self.assertFalse(row["can_admin"])
        with self._github({"mine/app": True}):
            (row,) = self.client.get(self.URL).data["repos"]
        self.assertTrue(row["can_admin"])

    def test_a_repo_that_got_registered_meanwhile_is_dropped_and_forgotten(self):
        GithubWebhookRegistration.objects.create(
            owner="MINE", repo="App", hook_id=1, registered_by=self.user
        )
        with self._github({"mine/app": True}):
            res = self.client.get(self.URL)
        self.assertEqual(res.data["repos"], [])
        self.assertFalse(GithubUnregisteredDelivery.objects.filter(repo_key="mine/app").exists())

    def test_one_failing_lookup_does_not_hide_the_others(self):
        calls = []

        def fake(account, path, params=None):
            calls.append(path)
            if "secret-repo" in path:
                raise RuntimeError("boom")
            return _resp(200, {"permissions": {"admin": True}})

        with patch("origin.views.common.github_views._github_get", side_effect=fake):
            res = self.client.get(self.URL)
        self.assertEqual([r["repo"] for r in res.data["repos"]], ["app"])
        self.assertEqual(len(calls), 2)

    def test_without_a_github_connection_nothing_is_shown(self):
        self.account.delete()
        with patch("origin.views.common.github_views._github_get") as get:
            res = self.client.get(self.URL)
        self.assertEqual(res.data, {"repos": [], "connected": False})
        get.assert_not_called()

    def test_anonymous_is_refused(self):
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(self.URL).status_code, 401)
