"""Tests for the branch auto-linking endpoint
(`GET /api/v2/github/branches/for-task/`).

We mock GitHub's HTTP API and `get_valid_access_token` so the tests run
offline. The endpoint scans `GithubWebhookRegistration` rows for repos
to query and filters branches by a word-boundary regex against the task's
`display_id` (e.g. "GEN-42").
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
from origin.views.common.github_views import _branch_match_re

User = get_user_model()


class TestBranchMatchRegex(TestCase):
    """Pure-function tests for the boundary-aware display-ID matcher.
    Catches the common off-by-one alias bug ("GEN-4" should not match
    "GEN-42") before we even hit the view layer."""

    def test_matches_at_start_of_branch(self):
        r = _branch_match_re("GEN-42")
        self.assertTrue(r.search("GEN-42-add-foo"))
        self.assertTrue(r.search("gen-42-add-foo"))  # case-insensitive

    def test_matches_after_separator(self):
        r = _branch_match_re("GEN-42")
        self.assertTrue(r.search("feature/GEN-42-thing"))
        self.assertTrue(r.search("kamiken/GEN-42_fix"))
        self.assertTrue(r.search("fix-GEN-42"))

    def test_matches_exact_name(self):
        r = _branch_match_re("GEN-42")
        self.assertTrue(r.search("GEN-42"))

    def test_does_not_match_prefix_of_longer_id(self):
        # The killer false-positive case: branch for GEN-420 should not
        # be linked to task GEN-42.
        r = _branch_match_re("GEN-42")
        self.assertFalse(r.search("GEN-420-foo"))
        self.assertFalse(r.search("feature/GEN-421"))

    def test_does_not_match_substring_of_other_id(self):
        r = _branch_match_re("GEN-42")
        self.assertFalse(r.search("OTHER-42"))
        self.assertFalse(r.search("XGEN-42"))


class TestBranchesForTaskView(TestCase):
    def setUp(self):
        # Branch list is now Redis-cached. Clear between tests so a
        # cached payload from a prior test doesn't shadow this test's mock.
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="branch-test",
            email="branch@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.team = TeamMaster.objects.create(
            team_name="Branch Team",
            team_email="branch@team.com",
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
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title="Branch test task",
            status="Open",
            project_task_number=42,
        )
        # User has a connected GitHub account (token won't actually be
        # used — `_github_get` is mocked at the request layer).
        self.account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="123",
            scopes=["repo", "read:user", "user:email"],
            access_token_encrypted="placeholder",
        )
        # One repo registered for branch scanning.
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="rocket", hook_id=1, registered_by=self.user
        )
        self.client.force_authenticate(user=self.user)

    @staticmethod
    def _mock_branches_response(names: list[str]):
        resp = MagicMock(spec=requests.Response)
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = [{"name": n, "commit": {"sha": f"sha-{n}"}} for n in names]
        return resp

    # ── Happy paths ───────────────────────────────────────────────

    @patch("origin.views.common.github_views._github_get")
    def test_returns_matching_branches(self, mock_get):
        mock_get.return_value = self._mock_branches_response(
            [
                "main",
                "feature/GEN-42-something",
                "kamiken/GEN-42_fix",
                "OTHER-99-unrelated",
            ]
        )
        resp = self.client.get(f"/api/v2/github/branches/for-task/?task_id={self.task.task_id}")
        self.assertEqual(resp.status_code, 200)
        branches = resp.json()["branches"]
        self.assertEqual(len(branches), 2)
        names = {b["name"] for b in branches}
        self.assertEqual(names, {"feature/GEN-42-something", "kamiken/GEN-42_fix"})
        for b in branches:
            self.assertEqual(b["owner"], "acme")
            self.assertEqual(b["repo"], "rocket")
            self.assertTrue(b["url"].startswith("https://github.com/acme/rocket/tree/"))

    @patch("origin.views.common.github_views._github_get")
    def test_does_not_alias_longer_ids(self, mock_get):
        # Critical: GEN-420 must not be returned for task GEN-42.
        mock_get.return_value = self._mock_branches_response(["GEN-420-other-task", "GEN-42"])
        resp = self.client.get(f"/api/v2/github/branches/for-task/?task_id={self.task.task_id}")
        branches = resp.json()["branches"]
        self.assertEqual([b["name"] for b in branches], ["GEN-42"])

    @patch("origin.views.common.github_views._github_get")
    def test_returns_empty_when_no_match(self, mock_get):
        mock_get.return_value = self._mock_branches_response(["main", "develop"])
        resp = self.client.get(f"/api/v2/github/branches/for-task/?task_id={self.task.task_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["branches"], [])

    # ── Guard paths ───────────────────────────────────────────────

    def test_400_when_task_id_missing(self):
        resp = self.client.get("/api/v2/github/branches/for-task/")
        self.assertEqual(resp.status_code, 400)

    def test_404_when_task_unknown(self):
        resp = self.client.get("/api/v2/github/branches/for-task/?task_id=999999")
        self.assertEqual(resp.status_code, 404)

    @patch("origin.views.common.github_views._github_get")
    def test_returns_empty_for_task_without_display_id(self, mock_get):
        # Orphan task — no project. display_id falls back to "#<id>" so
        # the view returns an empty list to avoid aliasing.
        orphan = TaskMaster.objects.create(
            team=self.team,
            project=None,
            assignee=self.user,
            reporter=self.user,
            title="Orphan",
            status="Open",
        )
        resp = self.client.get(f"/api/v2/github/branches/for-task/?task_id={orphan.task_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["branches"], [])
        mock_get.assert_not_called()  # short-circuit before hitting GitHub

    def test_returns_empty_when_github_not_connected(self):
        # Drop the ConnectedAccount and re-request — view should return
        # empty silently (the UI hides the section).
        self.account.delete()
        resp = self.client.get(f"/api/v2/github/branches/for-task/?task_id={self.task.task_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["branches"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_skips_repos_that_return_error(self, mock_get):
        # First repo errors, second returns a match. We should not bail
        # out — the matching branch should still come through.
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="other", hook_id=2, registered_by=self.user
        )
        good_resp = self._mock_branches_response(["GEN-42-fix"])
        bad_resp = MagicMock(spec=requests.Response)
        bad_resp.ok = False
        bad_resp.status_code = 404

        # `values_list("owner", "repo").distinct()` ordering isn't
        # guaranteed, so return based on the called path.
        def side_effect(_account, path, params=None):
            return bad_resp if "/acme/rocket/" in path else good_resp

        mock_get.side_effect = side_effect
        resp = self.client.get(f"/api/v2/github/branches/for-task/?task_id={self.task.task_id}")
        self.assertEqual(resp.status_code, 200)
        branches = resp.json()["branches"]
        self.assertEqual(len(branches), 1)
        self.assertEqual(branches[0]["repo"], "other")

    def test_401_when_unauthenticated(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(f"/api/v2/github/branches/for-task/?task_id={self.task.task_id}")
        self.assertIn(resp.status_code, (401, 403))
