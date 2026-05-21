"""Tests for the PR auto-linking endpoint
(`GET /api/v2/github/pulls/for-task/`).

This endpoint walks repos registered for our webhook, finds branches
whose names match the task's display ID, then looks up the PR (if any)
for each match. The result feeds the task table's PR column — manual
`task.links` entries are intentionally not surfaced (auto-linking is
the single source of truth).
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


class TestPullsForTaskView(TestCase):
    def setUp(self):
        # Redis is a real backend; clear between tests so a cached
        # branch list from a prior test doesn't shadow this test's mock.
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="pulls-test",
            email="pulls@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.team = TeamMaster.objects.create(
            team_name="Pulls Team",
            team_email="pulls@team.com",
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
            title="PR test task",
            status="Open",
            project_task_number=42,
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

    @staticmethod
    def _bad_response(status_code=404):
        resp = MagicMock(spec=requests.Response)
        resp.ok = False
        resp.status_code = status_code
        return resp

    # ── Happy paths ───────────────────────────────────────────────

    @patch("origin.views.common.github_views._github_get")
    def test_returns_pr_for_matching_branch(self, mock_get):
        branch = "feature/GEN-42-add-thing"
        pr = {
            "number": 7,
            "html_url": "https://github.com/acme/rocket/pull/7",
            "title": "Add thing",
            "state": "open",
            "draft": False,
            "merged_at": None,
        }

        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return self._branches_response(["main", branch])
            if path.endswith("/pulls"):
                # head param must reference the matching branch.
                self.assertEqual(params.get("head"), f"acme:{branch}")
                return self._pulls_response([pr])
            self.fail(f"Unexpected GitHub path: {path}")

        mock_get.side_effect = side_effect
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}")
        self.assertEqual(resp.status_code, 200)
        pulls = resp.json()["pulls"]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0]["number"], 7)
        self.assertEqual(pulls[0]["owner"], "acme")
        self.assertEqual(pulls[0]["repo"], "rocket")
        self.assertEqual(pulls[0]["branch"], branch)

    @patch("origin.views.common.github_views._github_get")
    def test_skips_branches_without_an_associated_pr(self, mock_get):
        # The branch matches but has no PR yet — endpoint returns empty.
        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return self._branches_response(["feature/GEN-42-thing"])
            if path.endswith("/pulls"):
                return self._empty_pulls_response()
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pulls"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_dedupes_same_pr_across_branches(self, mock_get):
        # Two branches both contain GEN-42 and the GitHub API happens to
        # surface the same PR for each (rare — possible when a branch
        # was renamed and the PR head was updated). The endpoint dedupes
        # by html_url.
        pr_url = "https://github.com/acme/rocket/pull/7"
        pr = {
            "number": 7,
            "html_url": pr_url,
            "title": "x",
            "state": "open",
            "draft": False,
            "merged_at": None,
        }

        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return self._branches_response(["feature/GEN-42-a", "fix/GEN-42-b"])
            if path.endswith("/pulls"):
                return self._pulls_response([pr])
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}")
        pulls = resp.json()["pulls"]
        self.assertEqual(len(pulls), 1)

    # ── Caching ───────────────────────────────────────────────────

    @patch("origin.views.common.github_views._github_get")
    def test_branch_list_is_cached_across_requests(self, mock_get):
        # First request hits the branches endpoint once; second request
        # within the cache TTL should not re-fetch branches.
        branch_resp = self._branches_response(["feature/GEN-42-x"])
        pulls_resp = self._empty_pulls_response()

        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return branch_resp
            if path.endswith("/pulls"):
                return pulls_resp
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        url = f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}"
        self.client.get(url)
        # Reset the call log on the mock and re-request — the branch
        # call should not happen again because the result is cached.
        first_call_count = mock_get.call_count
        mock_get.reset_mock()
        self.client.get(url)
        branches_calls = sum(1 for c in mock_get.call_args_list if c.args[1].endswith("/branches"))
        self.assertEqual(branches_calls, 0)
        # Sanity: cumulative calls should have decreased (we only call
        # the pulls API on the second request — and even that is cached
        # for the same branch, so it should also be skipped).
        self.assertLess(mock_get.call_count, first_call_count)

    @patch("origin.views.common.github_views._github_get")
    def test_negative_pr_lookup_is_cached(self, mock_get):
        # A branch with "no PR" result should also be cached so we don't
        # spam GitHub asking about a branch that never gets one.
        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return self._branches_response(["feature/GEN-42-x"])
            if path.endswith("/pulls"):
                return self._empty_pulls_response()
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        url = f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}"
        self.client.get(url)
        mock_get.reset_mock()
        self.client.get(url)
        # Both branches list AND pulls lookup should be cached now.
        self.assertEqual(mock_get.call_count, 0)

    # ── Guard paths ───────────────────────────────────────────────

    def test_400_when_task_id_missing(self):
        resp = self.client.get("/api/v2/github/pulls/for-task/")
        self.assertEqual(resp.status_code, 400)

    def test_404_when_task_unknown(self):
        resp = self.client.get("/api/v2/github/pulls/for-task/?task_id=999999")
        self.assertEqual(resp.status_code, 404)

    @patch("origin.views.common.github_views._github_get")
    def test_returns_empty_for_task_without_display_id(self, mock_get):
        orphan = TaskMaster.objects.create(
            team=self.team,
            project=None,
            assignee=self.user,
            reporter=self.user,
            title="Orphan",
            status="Open",
        )
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={orphan.task_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pulls"], [])
        mock_get.assert_not_called()

    def test_returns_empty_when_github_not_connected(self):
        self.account.delete()
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pulls"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_skips_repo_when_branches_endpoint_errors(self, mock_get):
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="other", hook_id=2, registered_by=self.user
        )
        pr = {
            "number": 9,
            "html_url": "https://github.com/acme/other/pull/9",
            "title": "x",
            "state": "open",
            "draft": False,
            "merged_at": None,
        }

        def side_effect(_account, path, params=None):
            if "/acme/rocket/branches" in path:
                return self._bad_response(404)
            if "/acme/other/branches" in path:
                return self._branches_response(["feature/GEN-42-y"])
            if path.endswith("/pulls"):
                return self._pulls_response([pr])
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}")
        pulls = resp.json()["pulls"]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0]["repo"], "other")

    def test_401_when_unauthenticated(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}")
        self.assertIn(resp.status_code, (401, 403))

    # ── Persistence: PR URL in task.links, branch deleted ─────────
    #
    # Once a PR is auto-discovered, the frontend stamps the link object
    # with `isAutoLinked: true`. After the source branch is deleted
    # (typical post-merge cleanup) the branch-walk path stops finding
    # the PR — but we want the badge to keep showing. The endpoint must
    # consult `task.links` as a fallback source.

    @staticmethod
    def _pr_detail_response(pr: dict):
        resp = MagicMock(spec=requests.Response)
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = pr
        return resp

    @patch("origin.views.common.github_views._github_get")
    def test_persisted_pr_url_surfaces_when_branch_is_gone(self, mock_get):
        # Branch was deleted — branch listing returns no matching name.
        url = "https://github.com/acme/rocket/pull/7"
        self.task.links = [
            {
                "id": "link-pr-1",
                "url": url,
                "title": "acme/rocket#7",
                "isGitHub": True,
                "isAutoLinked": True,
            }
        ]
        self.task.save(update_fields=["links"])
        merged_pr = {
            "number": 7,
            "html_url": url,
            "title": "Add thing",
            "state": "closed",
            "draft": False,
            "merged_at": "2026-05-20T00:00:00Z",
            "head": {"ref": "feature/GEN-42-something"},
        }

        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return self._branches_response([])  # branch gone
            if path.endswith("/pulls/7"):
                return self._pr_detail_response(merged_pr)
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}")
        self.assertEqual(resp.status_code, 200)
        pulls = resp.json()["pulls"]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0]["number"], 7)
        # State + merged_at flow through so the frontend can render the
        # right badge color (merged vs closed-unmerged vs open).
        self.assertEqual(pulls[0]["state"], "closed")
        self.assertEqual(pulls[0]["merged_at"], "2026-05-20T00:00:00Z")

    @patch("origin.views.common.github_views._github_get")
    def test_persisted_pr_url_closed_unmerged_renders_state(self, mock_get):
        # Same persistence path, but the PR was closed without merging.
        # We should still surface it with state=closed and merged_at=null.
        url = "https://github.com/acme/rocket/pull/9"
        self.task.links = [
            {
                "id": "link-pr-9",
                "url": url,
                "title": "acme/rocket#9",
                "isGitHub": True,
                "isAutoLinked": True,
            }
        ]
        self.task.save(update_fields=["links"])
        closed_pr = {
            "number": 9,
            "html_url": url,
            "title": "Abandoned",
            "state": "closed",
            "draft": False,
            "merged_at": None,
            "head": {"ref": "feature/GEN-42-other"},
        }

        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return self._branches_response([])
            if path.endswith("/pulls/9"):
                return self._pr_detail_response(closed_pr)
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}")
        pulls = resp.json()["pulls"]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0]["state"], "closed")
        self.assertIsNone(pulls[0]["merged_at"])

    @patch("origin.views.common.github_views._github_get")
    def test_manual_pr_url_in_links_is_not_surfaced(self, mock_get):
        # A PR URL pasted manually via DynamicURLManager — no
        # `isAutoLinked` flag. The endpoint must ignore it so the
        # column doesn't drift back into "show every linked URL" mode.
        self.task.links = [
            {
                "id": "link-manual-1",
                "url": "https://github.com/acme/rocket/pull/99",
                "title": "Manual",
                "isGitHub": True,
                # No isAutoLinked: true here.
            }
        ]
        self.task.save(update_fields=["links"])
        mock_get.return_value = self._branches_response([])  # nothing live
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pulls"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_persisted_and_live_branch_for_same_pr_is_deduped(self, mock_get):
        # The PR exists both via a still-live branch AND as a persisted
        # auto-link in task.links — we should only see it once.
        url = "https://github.com/acme/rocket/pull/12"
        self.task.links = [
            {
                "id": "link-pr-12",
                "url": url,
                "title": "acme/rocket#12",
                "isGitHub": True,
                "isAutoLinked": True,
            }
        ]
        self.task.save(update_fields=["links"])
        live_pr = {
            "number": 12,
            "html_url": url,
            "title": "Still in flight",
            "state": "open",
            "draft": False,
            "merged_at": None,
        }

        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return self._branches_response(["feature/GEN-42-thing"])
            if path.endswith("/pulls") and (params or {}).get("head"):
                return self._pulls_response([live_pr])
            if path.endswith("/pulls/12"):
                return self._pr_detail_response(
                    {**live_pr, "head": {"ref": "feature/GEN-42-thing"}}
                )
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        resp = self.client.get(f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}")
        pulls = resp.json()["pulls"]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0]["number"], 12)

    @patch("origin.views.common.github_views._github_get")
    def test_persisted_pr_lookup_is_cached(self, mock_get):
        url = "https://github.com/acme/rocket/pull/22"
        self.task.links = [
            {
                "id": "link-pr-22",
                "url": url,
                "title": "acme/rocket#22",
                "isGitHub": True,
                "isAutoLinked": True,
            }
        ]
        self.task.save(update_fields=["links"])
        pr = {
            "number": 22,
            "html_url": url,
            "title": "Cached",
            "state": "closed",
            "draft": False,
            "merged_at": "2026-05-19T00:00:00Z",
            "head": {"ref": "deleted-branch"},
        }

        def side_effect(_account, path, params=None):
            if path.endswith("/branches"):
                return self._branches_response([])
            if path.endswith("/pulls/22"):
                return self._pr_detail_response(pr)
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        url_endpoint = f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}"
        self.client.get(url_endpoint)
        # Reset the call log and re-request — the persisted-PR lookup
        # should be served from the cache.
        mock_get.reset_mock()
        self.client.get(url_endpoint)
        pulls_calls = sum(1 for c in mock_get.call_args_list if c.args[1].endswith("/pulls/22"))
        self.assertEqual(pulls_calls, 0)
