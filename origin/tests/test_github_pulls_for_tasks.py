"""Tests for the batched PR auto-linking endpoint
(`GET /api/v2/github/pulls/for-tasks/?task_ids=1,2,3`).

Batched variant of pulls/for-task/: the task table fires ONE request
per paint instead of one per row. The per-task semantics (branch-match
sources, html_url dedupe) are shared with the single view through
`_pulls_for_task`; what these tests pin down is the batch-specific
behavior — CSV parsing/caps, per-id response keys, and the hoisted
repo/branch walk (one branches fetch per repo per request, however
many tasks are asked for).
"""

from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import (
    ConnectedAccount,
    GithubWebhookRegistration,
)
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster

User = get_user_model()


class TestPullsForTasksView(TestCase):
    def setUp(self):
        # Redis is a real backend; clear between tests so a cached
        # branch list from a prior test doesn't shadow this test's mock.
        cache.clear()
        # The view resolves the user's GitHub token itself (once per request,
        # before any parallel fetch), and these accounts store a placeholder
        # that cannot be decrypted. `_github_get` is what each test fakes.
        self.enterContext(
            patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
        )
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="pulls-batch-test",
            email="pulls-batch@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.team = TeamMaster.objects.create(
            team_name="Pulls Batch Team",
            team_email="pulls-batch@team.com",
            owner=self.user,
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Genos Core",
            owner=self.user,
            project_system_user=self.user,
            code="GEN",
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task_a = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title="Task A",
            status="Open",
            project_task_number=42,
        )
        self.task_b = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title="Task B",
            status="Open",
            project_task_number=43,
        )
        self.account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="123",
            scopes=["repo", "read:user", "user:email"],
            access_token_encrypted="placeholder",
        )
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="rocket", hook_id=1, registered_by=self.user
        )
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        cache.clear()

    # ── Mock helpers ──────────────────────────────────────────────

    @staticmethod
    def _branches_response(names):
        resp = MagicMock(spec=requests.Response)
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = [{"name": n, "commit": {"sha": f"sha-{n}"}} for n in names]
        return resp

    @staticmethod
    def _pulls_response(items):
        resp = MagicMock(spec=requests.Response)
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = items
        return resp

    @staticmethod
    def _empty_pulls_response():
        resp = MagicMock(spec=requests.Response)
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = []
        return resp

    def _get(self, ids):
        return self.client.get(f"/api/v2/github/pulls/for-tasks/?task_ids={ids}")

    # ── Happy path ────────────────────────────────────────────────

    @patch("origin.views.common.github_views._github_get")
    def test_resolves_pulls_per_task_in_one_request(self, mock_get):
        branch_a = "feature/GEN-42-add-thing"
        branch_b = "fix/GEN-43-hotfix"
        pr_a = {
            "number": 7,
            "html_url": "https://github.com/acme/rocket/pull/7",
            "title": "Add thing",
            "state": "open",
            "draft": False,
            "merged_at": None,
        }
        pr_b = {
            "number": 8,
            "html_url": "https://github.com/acme/rocket/pull/8",
            "title": "Hotfix",
            "state": "closed",
            "draft": False,
            "merged_at": "2026-06-01T00:00:00Z",
        }

        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return self._branches_response(["main", branch_a, branch_b])
            if path.endswith("/pulls"):
                head = (params or {}).get("head")
                if head == f"acme:{branch_a}":
                    return self._pulls_response([pr_a])
                if head == f"acme:{branch_b}":
                    return self._pulls_response([pr_b])
            self.fail(f"Unexpected GitHub path: {path}")

        mock_get.side_effect = side_effect
        resp = self._get(f"{self.task_a.task_id},{self.task_b.task_id}")
        self.assertEqual(resp.status_code, 200)
        by_task = resp.json()["pulls_by_task"]
        self.assertEqual(len(by_task[str(self.task_a.task_id)]), 1)
        self.assertEqual(by_task[str(self.task_a.task_id)][0]["number"], 7)
        self.assertEqual(len(by_task[str(self.task_b.task_id)]), 1)
        self.assertEqual(by_task[str(self.task_b.task_id)][0]["number"], 8)

    @patch("origin.views.common.github_views._github_get")
    def test_branches_fetched_once_per_repo_regardless_of_task_count(self, mock_get):
        # The whole point of the batch: N tasks must not mean N branch
        # walks. One branches call per registered repo per request.
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="other", hook_id=2, registered_by=self.user
        )

        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return self._branches_response(["main"])
            self.fail(f"Unexpected GitHub path: {path}")

        mock_get.side_effect = side_effect
        resp = self._get(f"{self.task_a.task_id},{self.task_b.task_id}")
        self.assertEqual(resp.status_code, 200)
        branches_calls = [c for c in mock_get.call_args_list if c.args[1].endswith("/branches")]
        self.assertEqual(len(branches_calls), 2)  # one per repo, not per task

    @patch("origin.views.common.github_views._github_get")
    def test_unknown_and_pull_less_ids_map_to_empty_lists(self, mock_get):
        # Unlike the single view's 404, a bad id must not fail the
        # batch — every requested id gets a key so the client can
        # cache negatives uniformly.
        mock_get.return_value = self._branches_response(["main"])
        resp = self._get(f"{self.task_a.task_id},999999")
        self.assertEqual(resp.status_code, 200)
        by_task = resp.json()["pulls_by_task"]
        self.assertEqual(by_task[str(self.task_a.task_id)], [])
        self.assertEqual(by_task["999999"], [])

    # ── Guard paths ───────────────────────────────────────────────

    def test_400_on_non_integer_ids(self):
        resp = self._get("1,abc")
        self.assertEqual(resp.status_code, 400)

    def test_400_when_over_the_cap(self):
        ids = ",".join(str(i) for i in range(1, 202))
        resp = self._get(ids)
        self.assertEqual(resp.status_code, 400)

    def test_empty_ids_returns_empty_map(self):
        resp = self._get("")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pulls_by_task"], {})

    def test_returns_empty_lists_when_github_not_connected(self):
        self.account.delete()
        resp = self._get(f"{self.task_a.task_id},{self.task_b.task_id}")
        self.assertEqual(resp.status_code, 200)
        by_task = resp.json()["pulls_by_task"]
        self.assertEqual(by_task[str(self.task_a.task_id)], [])
        self.assertEqual(by_task[str(self.task_b.task_id)], [])

    def test_401_when_unauthenticated(self):
        self.client.force_authenticate(user=None)
        resp = self._get(str(self.task_a.task_id))
        self.assertIn(resp.status_code, (401, 403))

    @patch("origin.views.common.github_views._github_get")
    def test_task_without_display_id_maps_to_empty_without_github_calls(self, mock_get):
        orphan = TaskMaster.objects.create(
            team=self.team,
            project=None,
            assignee=self.user,
            reporter=self.user,
            title="Orphan",
            status="Open",
        )
        resp = self._get(str(orphan.task_id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pulls_by_task"][str(orphan.task_id)], [])
        mock_get.assert_not_called()

    # ── Which answers are "could not look" (MIP-1412) ─────────────
    #
    # A GitHub call that fails for a reason that is not about access (a 5xx, a
    # rate limit, a 401) used to read as "this branch has no PR": the task's list
    # was just shorter or empty, in a 200 that looks like any other. The response
    # now also names the tasks it could not fully look at, in `incomplete`. A
    # REFUSAL (404, or a 403 that is not a rate limit) is not one of them: it is
    # GitHub saying this account cannot read the thing, which the server already
    # remembers for ten minutes, and "no PR" is the true answer for this user.

    BRANCH_A = "feature/GEN-42-add-thing"
    BRANCH_B = "fix/GEN-43-hotfix"

    @staticmethod
    def _failure(status, headers=None):
        resp = MagicMock(spec=requests.Response)
        resp.ok = False
        resp.status_code = status
        resp.headers = headers or {}
        resp.json.return_value = {"message": "boom"}
        return resp

    @staticmethod
    def _pr(number):
        return {
            "number": number,
            "html_url": f"https://github.com/acme/rocket/pull/{number}",
            "title": f"PR {number}",
            "state": "open",
            "draft": False,
            "merged_at": None,
        }

    def _github(self, mock_get, *, lookup=None, branches=None, by_number=None, names=None):
        """A fake GitHub for acme/rocket with a branch per task. `lookup` maps a
        branch name to the response of its PR lookup (default: its PR), `branches`
        maps a repo to the response of its branch list, `by_number` a PR number to
        the response of a lookup by number, `names` the branches acme/rocket has."""
        lookup, branches, by_number = lookup or {}, branches or {}, by_number or {}
        prs = {self.BRANCH_A: self._pr(7), self.BRANCH_B: self._pr(8)}

        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                repo = path.split("/")[3]
                if repo in branches:
                    return branches[repo]
                return self._branches_response(names or ["main", self.BRANCH_A, self.BRANCH_B])
            if path.endswith("/pulls"):
                branch = (params or {})["head"].split(":", 1)[1]
                if branch in lookup:
                    return lookup[branch]
                return self._pulls_response([prs[branch]] if branch in prs else [])
            number = int(path.rsplit("/", 1)[1])
            if number in by_number:
                return by_number[number]
            self.fail(f"Unexpected GitHub path: {path}")

        mock_get.side_effect = side_effect

    def _ids(self):
        return f"{self.task_a.task_id},{self.task_b.task_id}"

    def _body(self, query=""):
        resp = self.client.get(f"/api/v2/github/pulls/for-tasks/?task_ids={self._ids()}{query}")
        self.assertEqual(resp.status_code, 200)
        return resp.json()

    @patch("origin.views.common.github_views._github_get")
    def test_a_healthy_batch_reports_nothing_incomplete(self, mock_get):
        self._github(mock_get)
        body = self._body()
        self.assertEqual(body["incomplete"], [])
        self.assertEqual(len(body["pulls_by_task"][str(self.task_a.task_id)]), 1)

    @patch("origin.views.common.github_views._github_get")
    def test_a_5xx_on_one_lookup_names_that_task_and_no_other(self, mock_get):
        self._github(mock_get, lookup={self.BRANCH_A: self._failure(500)})
        body = self._body()
        self.assertEqual(body["incomplete"], [str(self.task_a.task_id)])
        self.assertEqual(body["pulls_by_task"][str(self.task_a.task_id)], [])
        # The healthy task is answered in the same request, and is not named.
        self.assertEqual(body["pulls_by_task"][str(self.task_b.task_id)][0]["number"], 8)

    @patch("origin.views.common.github_views._github_get")
    def test_a_rate_limit_or_a_401_is_a_failed_lookup(self, mock_get):
        for label, failure in (
            ("429", lambda: self._failure(429)),
            ("403 with no quota left", lambda: self._failure(403, {"X-RateLimit-Remaining": "0"})),
            ("403 with Retry-After", lambda: self._failure(403, {"Retry-After": "30"})),
            ("401", lambda: self._failure(401)),
        ):
            with self.subTest(failure=label):
                cache.clear()
                self._github(mock_get, lookup={self.BRANCH_A: failure()})
                self.assertEqual(self._body()["incomplete"], [str(self.task_a.task_id)])

    @patch("origin.views.common.github_views._github_get")
    def test_a_refusal_is_not_incomplete_it_is_the_answer(self, mock_get):
        for label, failure in (
            ("404", lambda: self._failure(404)),
            ("403 that is not a rate limit", lambda: self._failure(403)),
        ):
            with self.subTest(failure=label):
                cache.clear()
                self._github(mock_get, lookup={self.BRANCH_A: failure()})
                for _ in range(2):  # the second is the remembered refusal
                    body = self._body()
                    self.assertEqual(body["incomplete"], [])
                    self.assertEqual(body["pulls_by_task"][str(self.task_a.task_id)], [])

    @patch("origin.views.common.github_views._github_get")
    def test_a_failed_lookup_is_not_cached_so_the_next_request_heals(self, mock_get):
        self._github(mock_get, lookup={self.BRANCH_A: self._failure(502)})
        self.assertEqual(self._body()["incomplete"], [str(self.task_a.task_id)])

        self._github(mock_get)  # GitHub is back
        body = self._body()
        self.assertEqual(body["incomplete"], [])
        self.assertEqual(body["pulls_by_task"][str(self.task_a.task_id)][0]["number"], 7)

    @patch("origin.views.common.github_views._github_get")
    def test_both_failing_are_both_named_in_id_order(self, mock_get):
        self._github(
            mock_get,
            lookup={self.BRANCH_A: self._failure(500), self.BRANCH_B: self._failure(500)},
        )
        self.assertEqual(
            self._body()["incomplete"],
            sorted([str(self.task_a.task_id), str(self.task_b.task_id)], key=int),
        )

    @patch("origin.views.common.github_views._github_get")
    def test_a_repo_whose_branch_list_failed_makes_every_task_incomplete(self, mock_get):
        # The branch we could not read may be the one that carries a task's PR, so
        # no task can be called complete. The repo that did answer still counts.
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="other", hook_id=2, registered_by=self.user
        )
        self._github(mock_get, branches={"other": self._failure(503)})
        body = self._body()
        self.assertEqual(
            body["incomplete"],
            sorted([str(self.task_a.task_id), str(self.task_b.task_id)], key=int),
        )
        self.assertEqual(body["pulls_by_task"][str(self.task_a.task_id)][0]["number"], 7)

    @patch("origin.views.common.github_views._github_get")
    def test_a_repo_that_refuses_us_is_not_incomplete(self, mock_get):
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="other", hook_id=2, registered_by=self.user
        )
        self._github(mock_get, branches={"other": self._failure(404)})
        self.assertEqual(self._body()["incomplete"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_a_persisted_pr_that_fails_to_load_is_a_failed_lookup(self, mock_get):
        # The PR of a deleted branch comes from the task's persisted auto-link.
        self.task_a.links = [{"url": "https://github.com/acme/rocket/pull/9", "isAutoLinked": True}]
        self.task_a.save(update_fields=["links"])
        self._github(
            mock_get,
            lookup={self.BRANCH_A: self._pulls_response([])},
            by_number={9: self._failure(500)},
        )
        self.assertEqual(self._body()["incomplete"], [str(self.task_a.task_id)])

    @patch("origin.views.common.github_views._github_get")
    def test_a_persisted_pr_that_is_refused_is_not_incomplete(self, mock_get):
        self.task_a.links = [{"url": "https://github.com/acme/rocket/pull/9", "isAutoLinked": True}]
        self.task_a.save(update_fields=["links"])
        self._github(
            mock_get,
            lookup={self.BRANCH_A: self._pulls_response([])},
            by_number={9: self._failure(404)},
        )
        self.assertEqual(self._body()["incomplete"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_ids_are_ordered_as_numbers_not_as_text(self, mock_get):
        # "1000000000" < "900000001" as text; the other way round as numbers.
        tasks = [
            TaskMaster.objects.create(
                task_id=task_id,
                team=self.team,
                project=self.project,
                assignee=self.user,
                reporter=self.user,
                title=f"Task {number}",
                status="Open",
                project_task_number=number,
            )
            for task_id, number in ((1_000_000_000, 62), (900_000_001, 61))
        ]
        branches = ["main", "feature/GEN-61-x", "feature/GEN-62-y"]
        self._github(
            mock_get,
            names=branches,
            lookup={name: self._failure(500) for name in branches[1:]},
        )
        ids = ",".join(str(t.task_id) for t in tasks)
        resp = self.client.get(f"/api/v2/github/pulls/for-tasks/?task_ids={ids}")
        self.assertEqual(resp.json()["incomplete"], ["900000001", "1000000000"])

    @patch("origin.views.common.github_views._github_get")
    def test_fresh_reports_it_the_same_way(self, mock_get):
        self._github(mock_get, lookup={self.BRANCH_A: self._failure(500)})
        self.assertEqual(self._body("&fresh=1")["incomplete"], [str(self.task_a.task_id)])

    @patch("origin.views.common.github_views._github_get")
    def test_every_answer_carries_the_key_so_a_client_can_tell_the_server_reports_it(
        self, mock_get
    ):
        # Present and empty means "I looked and it is complete"; absent means an
        # older server that cannot say. Every path of the route has to say it.
        mock_get.return_value = self._branches_response(["main"])
        self.assertEqual(self._get("").json()["incomplete"], [])
        self.assertEqual(self._get(self._ids()).json()["incomplete"], [])
        orphan = TaskMaster.objects.create(
            team=self.team,
            project=None,
            assignee=self.user,
            reporter=self.user,
            title="Orphan",
            status="Open",
        )
        self.assertEqual(self._get(str(orphan.task_id)).json()["incomplete"], [])
        self.account.delete()
        self.assertEqual(self._get(self._ids()).json()["incomplete"], [])
