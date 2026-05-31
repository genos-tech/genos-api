"""Tests for the Phase 4 GitHub PR agent tools.

Five read-only tools that let the agent introspect a PR via the
calling user's OAuth-stored token:

  * fetch_pr           — metadata + truncated body
  * list_pr_comments   — issue + review comments merged, newest first
  * list_pr_files      — changed files (no patches)
  * list_pr_reviews    — Approve / Changes-requested / Comment reviews
  * list_pr_commits    — commits on the PR

All tests mock `_github_get` so they don't hit the live GitHub API.
"""

from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase

from origin.models.common.user_models import ConnectedAccount
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.fetch_pr import FETCH_PR
from origin.search_engine.agent.tools.list_pr_comments import LIST_PR_COMMENTS
from origin.search_engine.agent.tools.list_pr_commits import LIST_PR_COMMITS
from origin.search_engine.agent.tools.list_pr_files import LIST_PR_FILES
from origin.search_engine.agent.tools.list_pr_reviews import LIST_PR_REVIEWS

User = get_user_model()

PR_URL = "https://github.com/acme/rocket/pull/42"


def _ok_response(payload):
    resp = MagicMock(spec=requests.Response)
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _not_found_response():
    resp = MagicMock(spec=requests.Response)
    resp.ok = False
    resp.status_code = 404
    resp.json.return_value = {"message": "Not Found"}
    return resp


class _PrToolsBase(TestCase):
    """Shared setUp: a user + GitHub-connected account + tool context."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="pr-agent-test",
            email="pr-agent@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="999",
            scopes=["repo", "read:user", "user:email"],
            access_token_encrypted="placeholder",
        )
        # Phase 4 tools only need ctx.user_id — team_id is unused (the
        # data is fetched live from GitHub, not via the team-scoped
        # internal ORM). Still pass a non-empty string to mirror real ctx.
        self.ctx = ToolContext(team_id="team-x", user_id=str(self.user.id))


class TestFetchPr(_PrToolsBase):
    @patch("origin.search_engine.agent.tools.fetch_pr._github_get")
    def test_happy_path(self, mock_get):
        mock_get.return_value = _ok_response(
            {
                "number": 42,
                "title": "Add thing",
                "html_url": PR_URL,
                "state": "open",
                "draft": False,
                "merged": False,
                "merged_at": None,
                "user": {"login": "alice"},
                "head": {"ref": "feature/GEN-42-thing"},
                "base": {"ref": "main"},
                "additions": 10,
                "deletions": 2,
                "changed_files": 3,
                "commits": 4,
                "comments": 5,
                "review_comments": 1,
                "created_at": "2026-05-01T10:00:00Z",
                "updated_at": "2026-05-02T10:00:00Z",
                "body": "Adds the thing.",
            }
        )
        out = FETCH_PR.run({"pr_url": PR_URL}, self.ctx)
        self.assertEqual(out["owner"], "acme")
        self.assertEqual(out["repo"], "rocket")
        self.assertEqual(out["number"], 42)
        self.assertEqual(out["state"], "open")
        self.assertEqual(out["author"], "alice")
        self.assertEqual(out["head_ref"], "feature/GEN-42-thing")
        self.assertEqual(out["base_ref"], "main")
        self.assertEqual(out["comments_count"], 6)
        # PR body is wrapped for prompt-injection mitigation.
        self.assertIn("<workspace_content>", out["body"])
        self.assertIn("Adds the thing", out["body"])
        self.assertFalse(out["body_truncated"])
        self.assertIn("__summary__", out)

    @patch("origin.search_engine.agent.tools.fetch_pr._github_get")
    def test_merged_derives_state(self, mock_get):
        # `state` on GitHub stays "closed" for merged PRs; our tool
        # promotes it to "merged" so the model doesn't conflate
        # merged-vs-abandoned. Draft takes precedence over open too.
        mock_get.return_value = _ok_response(
            {
                "number": 42,
                "state": "closed",
                "merged": True,
                "merged_at": "2026-05-03T00:00:00Z",
                "html_url": PR_URL,
                "user": {"login": "alice"},
                "head": {"ref": "feature/GEN-42-x"},
                "base": {"ref": "main"},
            }
        )
        out = FETCH_PR.run({"pr_url": PR_URL}, self.ctx)
        self.assertEqual(out["state"], "merged")
        self.assertEqual(out["merged_at"], "2026-05-03T00:00:00Z")

    def test_rejects_bad_url(self):
        with self.assertRaises(ToolError):
            FETCH_PR.run({"pr_url": "not-a-pr-url"}, self.ctx)
        with self.assertRaises(ToolError):
            FETCH_PR.run({}, self.ctx)

    def test_rejects_when_github_not_connected(self):
        self.account.delete()
        with self.assertRaises(ToolError) as cm:
            FETCH_PR.run({"pr_url": PR_URL}, self.ctx)
        self.assertIn("GitHub", str(cm.exception))

    @patch("origin.search_engine.agent.tools.fetch_pr._github_get")
    def test_404_raises_tool_error(self, mock_get):
        mock_get.return_value = _not_found_response()
        with self.assertRaises(ToolError):
            FETCH_PR.run({"pr_url": PR_URL}, self.ctx)


class TestListPrComments(_PrToolsBase):
    @patch("origin.search_engine.agent.tools.list_pr_comments._github_get")
    def test_merges_and_sorts_newest_first(self, mock_get):
        issue_comments = [
            {
                "id": 1,
                "user": {"login": "alice"},
                "body": "top-level oldest",
                "created_at": "2026-05-01T10:00:00Z",
                "html_url": "https://github.com/acme/rocket/pull/42#issuecomment-1",
            },
        ]
        review_comments = [
            {
                "id": 2,
                "user": {"login": "bob"},
                "body": "inline newest",
                "path": "src/foo.py",
                "line": 12,
                "created_at": "2026-05-03T10:00:00Z",
                "html_url": "https://github.com/acme/rocket/pull/42#discussion_r-2",
            },
        ]

        def side_effect(_account, path, params=None):
            if path.endswith("/issues/42/comments"):
                return _ok_response(issue_comments)
            if path.endswith("/pulls/42/comments"):
                return _ok_response(review_comments)
            self.fail(f"Unexpected path: {path}")

        mock_get.side_effect = side_effect
        out = LIST_PR_COMMENTS.run({"pr_url": PR_URL}, self.ctx)
        self.assertEqual(out["returned_count"], 2)
        # Newest first → review comment leads.
        self.assertEqual(out["comments"][0]["id"], 2)
        self.assertEqual(out["comments"][0]["kind"], "review")
        self.assertEqual(out["comments"][0]["file_path"], "src/foo.py")
        self.assertEqual(out["comments"][0]["line"], 12)
        self.assertEqual(out["comments"][1]["kind"], "issue")
        # Bodies are wrapped to mark them as untrusted text.
        self.assertIn("<workspace_content>", out["comments"][0]["body"])

    @patch("origin.search_engine.agent.tools.list_pr_comments._github_get")
    def test_respects_limit(self, mock_get):
        many = [
            {
                "id": i,
                "user": {"login": "alice"},
                "body": f"c{i}",
                "created_at": f"2026-05-{i:02d}T00:00:00Z",
            }
            for i in range(1, 6)
        ]

        def side_effect(_account, path, params=None):
            if path.endswith("/issues/42/comments"):
                return _ok_response(many)
            return _ok_response([])

        mock_get.side_effect = side_effect
        out = LIST_PR_COMMENTS.run({"pr_url": PR_URL, "limit": 2}, self.ctx)
        self.assertEqual(out["returned_count"], 2)
        self.assertEqual(out["total_known"], 5)


class TestListPrFiles(_PrToolsBase):
    @patch("origin.search_engine.agent.tools.list_pr_files._github_get")
    def test_returns_slim_files(self, mock_get):
        mock_get.return_value = _ok_response(
            [
                {
                    "filename": "src/foo.py",
                    "status": "modified",
                    "additions": 10,
                    "deletions": 2,
                    "changes": 12,
                    "blob_url": "https://github.com/acme/rocket/blob/sha/src/foo.py",
                    # Even if the upstream payload carries a patch, our
                    # tool must drop it (token-budget hostile).
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                },
                {
                    "filename": "src/bar.py",
                    "previous_filename": "src/baz.py",
                    "status": "renamed",
                    "additions": 0,
                    "deletions": 0,
                    "changes": 0,
                },
            ]
        )
        out = LIST_PR_FILES.run({"pr_url": PR_URL}, self.ctx)
        self.assertEqual(out["returned_count"], 2)
        self.assertEqual(out["files"][0]["filename"], "src/foo.py")
        # Patch must NOT leak through.
        self.assertNotIn("patch", out["files"][0])
        self.assertEqual(out["files"][1]["previous_filename"], "src/baz.py")
        self.assertEqual(out["totals"], {"additions": 10, "deletions": 2})


class TestListPrReviews(_PrToolsBase):
    @patch("origin.search_engine.agent.tools.list_pr_reviews._github_get")
    def test_sorts_newest_first(self, mock_get):
        # GitHub returns reviews oldest-first by default.
        mock_get.return_value = _ok_response(
            [
                {
                    "id": 1,
                    "user": {"login": "alice"},
                    "state": "COMMENTED",
                    "body": "lgtm-ish",
                    "submitted_at": "2026-05-01T10:00:00Z",
                },
                {
                    "id": 2,
                    "user": {"login": "bob"},
                    "state": "APPROVED",
                    "body": "ship it",
                    "submitted_at": "2026-05-02T10:00:00Z",
                },
            ]
        )
        out = LIST_PR_REVIEWS.run({"pr_url": PR_URL}, self.ctx)
        self.assertEqual(out["returned_count"], 2)
        self.assertEqual(out["reviews"][0]["id"], 2)
        self.assertEqual(out["reviews"][0]["state"], "APPROVED")
        self.assertIn("<workspace_content>", out["reviews"][0]["body"])


class TestListPrCommits(_PrToolsBase):
    @patch("origin.search_engine.agent.tools.list_pr_commits._github_get")
    def test_returns_slim_commits(self, mock_get):
        mock_get.return_value = _ok_response(
            [
                {
                    "sha": "abcdef1234567890",
                    "html_url": "https://github.com/acme/rocket/commit/abcdef1",
                    "commit": {
                        "message": "First line\n\nLonger body that should\nbe dropped.",
                        "author": {"name": "Alice", "date": "2026-05-01T10:00:00Z"},
                        "committer": {
                            "name": "Alice",
                            "date": "2026-05-01T10:05:00Z",
                        },
                    },
                    "author": {"login": "alice"},
                },
            ]
        )
        out = LIST_PR_COMMITS.run({"pr_url": PR_URL}, self.ctx)
        self.assertEqual(out["returned_count"], 1)
        c = out["commits"][0]
        self.assertEqual(c["sha"], "abcdef1")
        self.assertEqual(c["full_sha"], "abcdef1234567890")
        # Prefer the resolved GitHub login over the git author name.
        self.assertEqual(c["author"], "alice")
        # Commit message is first line only, and wrapped.
        self.assertIn("<workspace_content>", c["message_first_line"])
        self.assertIn("First line", c["message_first_line"])
        self.assertNotIn("Longer body", c["message_first_line"])
        # Committer date wins over author date.
        self.assertEqual(c["committed_at"], "2026-05-01T10:05:00Z")
