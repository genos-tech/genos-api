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
