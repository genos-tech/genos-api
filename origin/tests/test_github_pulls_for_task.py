"""Tests for the PR auto-linking endpoint
(`GET /api/v2/github/pulls/for-task/`).

This endpoint walks repos registered for our webhook, reads each repo's recent pull
requests (MIP-2089: the GitHub App has no Contents permission, so there is no branch walk),
and keeps those whose head branch name matches the task's display ID. The result feeds the
task table's PR column — manual `task.links` entries are intentionally not surfaced
(auto-linking is the single source of truth).
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
from origin.tests.github_pull_fakes import bad_response, pull, pulls_response

User = get_user_model()


class TestPullsForTaskView(TestCase):
    def setUp(self):
        # Redis is a real backend; clear between tests so a cached
        # pulls list from a prior test doesn't shadow this test's mock.
        cache.clear()
        # The view resolves the user's GitHub token itself (once per request,
        # before any parallel fetch), and these accounts store a placeholder
        # that cannot be decrypted. `_github_get` is what each test fakes.
        self.enterContext(
            patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
        )
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
        self.url = f"/api/v2/github/pulls/for-task/?task_id={self.task.task_id}"

    def tearDown(self):
        cache.clear()

    @staticmethod
    def _pr_detail_response(pr: dict):
        resp = MagicMock(spec=requests.Response)
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = pr
        return resp

    # ── Happy paths ───────────────────────────────────────────────

    @patch("origin.views.common.github_views._github_get")
    def test_returns_pr_for_matching_branch(self, mock_get):
        branch = "feature/GEN-42-add-thing"

        def side_effect(_account, path, params=None):
            if path == "/repos/acme/rocket/pulls":
                # One call per repo: all states, newest first. No `head` filter, no branch list.
                self.assertEqual(params.get("state"), "all")
                self.assertNotIn("head", params)
                return pulls_response([pull(7, branch, title="Add thing"), pull(8, "main-fix")])
            self.fail(f"Unexpected GitHub path: {path}")

        mock_get.side_effect = side_effect
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        pulls = resp.json()["pulls"]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0]["number"], 7)
        self.assertEqual(pulls[0]["owner"], "acme")
        self.assertEqual(pulls[0]["repo"], "rocket")
        self.assertEqual(pulls[0]["branch"], branch)
        self.assertEqual(pulls[0]["title"], "Add thing")
        # The internal head sha is for the branches route and is not part of a pull.
        self.assertNotIn("head_sha", pulls[0])
        self.assertEqual(mock_get.call_count, 1)

    @patch("origin.views.common.github_views._github_get")
    def test_a_branch_with_no_pr_is_not_found(self, mock_get):
        # The GitHub App cannot list branches, so a branch with no PR is invisible. The pulls
        # list simply holds nothing for the task.
        mock_get.return_value = pulls_response([pull(8, "feature/OTHER-1-thing")])
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pulls"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_does_not_alias_longer_ids(self, mock_get):
        # GEN-4 must not match a GEN-42 branch, nor GEN-421.
        mock_get.return_value = pulls_response(
            [pull(1, "feature/GEN-421-x"), pull(2, "feature/GEN-4-x")]
        )
        resp = self.client.get(self.url)
        self.assertEqual(resp.json()["pulls"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_only_the_newest_pr_of_one_branch_name_shows(self, mock_get):
        # A closed PR and a newer one from the same branch name: the list is newest first, and
        # only the first shows (the per-branch lookup of the old walk gave the same).
        mock_get.return_value = pulls_response(
            [
                pull(9, "feature/GEN-42-a", state="open"),
                pull(7, "feature/GEN-42-a", state="closed"),
            ]
        )
        pulls = self.client.get(self.url).json()["pulls"]
        self.assertEqual([p["number"] for p in pulls], [9])

    @patch("origin.views.common.github_views._github_get")
    def test_two_branches_two_prs_are_both_listed(self, mock_get):
        mock_get.return_value = pulls_response(
            [pull(9, "feature/GEN-42-a"), pull(7, "fix/GEN-42-b", state="closed")]
        )
        pulls = self.client.get(self.url).json()["pulls"]
        self.assertEqual([p["number"] for p in pulls], [9, 7])

    @patch("origin.views.common.github_views._github_get")
    def test_a_fork_pr_is_not_linked_to_the_task(self, mock_get):
        # Anyone can open a PR from a fork whose branch is named after somebody's task. The old
        # branch walk only saw the repo's own branches; a forged name must not reach the column.
        mock_get.return_value = pulls_response(
            [
                pull(5, "GEN-42-evil", head_repo="mallory/rocket"),
                pull(6, "GEN-42-gone-fork", head_repo=None)
                | {"head": {"ref": "GEN-42-x", "repo": None}},
            ]
        )
        self.assertEqual(self.client.get(self.url).json()["pulls"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_a_busy_repo_also_asks_for_its_open_prs(self, mock_get):
        # A full page of other PRs: the task's open PR, untouched for a while, is past it.
        filler = [pull(1000 + i, f"bot/{i}", state="closed") for i in range(100)]

        def side_effect(_account, path, params=None):
            if path == "/repos/acme/rocket/pulls" and params["state"] == "all":
                return pulls_response(filler)
            if path == "/repos/acme/rocket/pulls" and params["state"] == "open":
                return pulls_response([pull(7, "feature/GEN-42-old-but-open")])
            self.fail(f"Unexpected GitHub path: {path} {params}")

        mock_get.side_effect = side_effect
        pulls = self.client.get(self.url).json()["pulls"]
        self.assertEqual([p["number"] for p in pulls], [7])
        self.assertEqual(mock_get.call_count, 2)
        # Cached together: nothing more is asked.
        mock_get.reset_mock()
        self.client.get(self.url)
        self.assertEqual(mock_get.call_count, 0)

    @patch("origin.views.common.github_views._github_get")
    def test_a_short_page_asks_only_once(self, mock_get):
        mock_get.return_value = pulls_response([pull(1, "x")])
        self.client.get(self.url)
        self.assertEqual(mock_get.call_count, 1)

    @patch("origin.views.common.github_views._github_get")
    def test_a_failed_open_list_is_not_cached_and_is_reported(self, mock_get):
        filler = [pull(1000 + i, f"bot/{i}", state="closed") for i in range(100)]

        def side_effect(_account, path, params=None):
            return pulls_response(filler) if params["state"] == "all" else bad_response(502)

        mock_get.side_effect = side_effect
        self.client.get(self.url)
        mock_get.reset_mock()
        self.client.get(self.url)
        self.assertEqual(mock_get.call_count, 2)  # asked again: the short answer was not kept

    # ── Caching ───────────────────────────────────────────────────

    @patch("origin.views.common.github_views._github_get")
    def test_pulls_list_is_cached_across_requests(self, mock_get):
        mock_get.return_value = pulls_response([pull(7, "feature/GEN-42-x")])
        first = self.client.get(self.url).json()
        self.assertEqual(mock_get.call_count, 1)
        mock_get.reset_mock()
        second = self.client.get(self.url).json()
        self.assertEqual(mock_get.call_count, 0)
        self.assertEqual(first, second)

    @patch("origin.views.common.github_views._github_get")
    def test_an_empty_answer_is_cached_too(self, mock_get):
        mock_get.return_value = pulls_response([])
        self.client.get(self.url)
        mock_get.reset_mock()
        self.client.get(self.url)
        self.assertEqual(mock_get.call_count, 0)

    @patch("origin.views.common.github_views._github_get")
    def test_fresh_skips_the_read_and_rewrites_it(self, mock_get):
        mock_get.return_value = pulls_response([pull(7, "feature/GEN-42-x")])
        self.client.get(self.url)
        mock_get.reset_mock()
        mock_get.return_value = pulls_response([pull(7, "feature/GEN-42-x", state="closed")])
        pulls = self.client.get(self.url + "&fresh=1").json()["pulls"]
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(pulls[0]["state"], "closed")
        # The fresh answer was written: a plain request now serves it.
        mock_get.reset_mock()
        self.assertEqual(self.client.get(self.url).json()["pulls"][0]["state"], "closed")
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
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pulls"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_skips_repo_when_pulls_endpoint_errors(self, mock_get):
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="other", hook_id=2, registered_by=self.user
        )

        def side_effect(_account, path, params=None):
            if path == "/repos/acme/rocket/pulls":
                return bad_response(404)
            if path == "/repos/acme/other/pulls":
                return pulls_response([pull(9, "feature/GEN-42-y", repo="other")])
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        pulls = self.client.get(self.url).json()["pulls"]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0]["repo"], "other")

    def test_401_when_unauthenticated(self):
        self.client.force_authenticate(user=None)
        resp = self.client.get(self.url)
        self.assertIn(resp.status_code, (401, 403))

    # ── Persistence: PR URL in task.links, PR no longer in the newest pulls ──
    #
    # Once a PR is auto-discovered, the frontend stamps the link object
    # with `isAutoLinked: true`. After the PR falls out of the repo's newest pulls
    # (a long-merged PR) the pulls walk stops finding it — but we want the badge
    # to keep showing. The endpoint must consult `task.links` as a fallback source.

    def _auto_link(self, number):
        url = f"https://github.com/acme/rocket/pull/{number}"
        self.task.links = [
            {
                "id": f"link-pr-{number}",
                "url": url,
                "title": f"acme/rocket#{number}",
                "isGitHub": True,
                "isAutoLinked": True,
            }
        ]
        self.task.save(update_fields=["links"])
        return url

    @patch("origin.views.common.github_views._github_get")
    def test_persisted_pr_url_surfaces_when_it_left_the_newest_pulls(self, mock_get):
        url = self._auto_link(7)
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
            if path == "/repos/acme/rocket/pulls":
                return pulls_response([])  # 7 is older than the page
            if path.endswith("/pulls/7"):
                return self._pr_detail_response(merged_pr)
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        resp = self.client.get(self.url)
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
        url = self._auto_link(9)
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
            if path == "/repos/acme/rocket/pulls":
                return pulls_response([])
            if path.endswith("/pulls/9"):
                return self._pr_detail_response(closed_pr)
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        pulls = self.client.get(self.url).json()["pulls"]
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
            }
        ]
        self.task.save(update_fields=["links"])
        mock_get.return_value = pulls_response([])  # nothing live
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pulls"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_persisted_pr_with_unrelated_branch_is_dropped(self, mock_get):
        # Backstop for the old body-mirror bug: a link entry can carry
        # `isAutoLinked: true` even though its branch has nothing to do
        # with this task. Source 2 must re-validate the PR's head branch
        # against the task's display_id and skip the unrelated PR.
        url = self._auto_link(77)
        unrelated_pr = {
            "number": 77,
            "html_url": url,
            "title": "Unrelated work for another task",
            "state": "open",
            "draft": False,
            "merged_at": None,
            "head": {"ref": "feature/OTHER-9-thing"},  # no GEN-42 in branch
        }

        def side_effect(_account, path, params=None):
            if path == "/repos/acme/rocket/pulls":
                return pulls_response([])
            if path.endswith("/pulls/77"):
                return self._pr_detail_response(unrelated_pr)
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["pulls"], [])

    @patch("origin.views.common.github_views._github_get")
    def test_persisted_and_live_pr_for_same_pr_is_deduped(self, mock_get):
        # The PR exists both in the newest pulls AND as a persisted
        # auto-link in task.links — we should only see it once.
        self._auto_link(12)

        def side_effect(_account, path, params=None):
            if path == "/repos/acme/rocket/pulls":
                return pulls_response([pull(12, "feature/GEN-42-thing")])
            self.fail(f"Unexpected path: {path}")  # the link is already shown: no by-number call

        mock_get.side_effect = side_effect
        pulls = self.client.get(self.url).json()["pulls"]
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0]["number"], 12)

    @patch("origin.views.common.github_views._github_get")
    def test_persisted_pr_lookup_is_cached(self, mock_get):
        url = self._auto_link(22)
        pr = {
            "number": 22,
            "html_url": url,
            "title": "Cached",
            "state": "closed",
            "draft": False,
            "merged_at": "2026-05-19T00:00:00Z",
            # Head ref retains the original (now-deleted) branch name on
            # the GitHub PR record. Must still contain the task's
            # display_id so Source 2's branch-match backstop accepts it.
            "head": {"ref": "feature/GEN-42-deleted"},
        }

        def side_effect(_account, path, params=None):
            if path == "/repos/acme/rocket/pulls":
                return pulls_response([])
            if path.endswith("/pulls/22"):
                return self._pr_detail_response(pr)
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        self.client.get(self.url)
        # Reset the call log and re-request — the persisted-PR lookup
        # should be served from the cache.
        mock_get.reset_mock()
        self.client.get(self.url)
        self.assertEqual(mock_get.call_count, 0)
