"""Tests for the GitHub PR-comment webhook → task activity feed (Phase 3).

Two event types are handled:
  - `pull_request_review_comment` (inline code-review comments) — the
    payload already carries `pull_request.head.ref`, so resolution is
    branch-name → display ID with no API call.
  - `issue_comment` (when the comment is on a PR, not a plain issue) —
    the payload only has `issue.pull_request.html_url`, so we fetch the
    PR via the GitHub API to obtain the head ref. Mocked here.

Behavior under test:
  - Only `created` actions record activity (edited/deleted ignored in v1).
  - Plain issue comments (no PR linkage) are silently dropped.
  - Idempotency: redelivering the same `comment.id` does not duplicate.
  - actor=None on the activity row; commenter identity lives in metadata.
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
    GithubWebhookRegistration,
)
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster

User = get_user_model()

WEBHOOK_SECRET = "test-secret-1234567890"


def _sign(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _review_comment_payload(
    *,
    action: str = "created",
    head_ref: str = "feature/HK-42-foo",
    comment_id: int = 1001,
    body: str = "Looks good to me.",
    login: str = "octocat",
) -> dict:
    return {
        "action": action,
        "pull_request": {
            "html_url": "https://github.com/owner/repo/pull/42",
            "number": 42,
            "head": {"ref": head_ref, "sha": "abc"},
            "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        },
        "comment": {
            "id": comment_id,
            "body": body,
            "html_url": (f"https://github.com/owner/repo/pull/42#discussion_r{comment_id}"),
            "user": {
                "login": login,
                "avatar_url": f"https://avatars.githubusercontent.com/{login}",
            },
            "path": "src/foo.py",
            "line": 17,
        },
        "repository": {"full_name": "owner/repo"},
    }


def _issue_comment_payload(
    *,
    action: str = "created",
    is_pr: bool = True,
    issue_number: int = 42,
    comment_id: int = 2001,
    body: str = "Top-level PR comment.",
    login: str = "octocat",
) -> dict:
    issue: dict = {"number": issue_number}
    if is_pr:
        issue["pull_request"] = {"html_url": f"https://github.com/owner/repo/pull/{issue_number}"}
    return {
        "action": action,
        "issue": issue,
        "comment": {
            "id": comment_id,
            "body": body,
            "html_url": (
                f"https://github.com/owner/repo/pull/{issue_number}#issuecomment-{comment_id}"
            ),
            "user": {
                "login": login,
                "avatar_url": f"https://avatars.githubusercontent.com/{login}",
            },
        },
        "repository": {"full_name": "owner/repo"},
    }


@override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
class TestPrCommentWebhook(TestCase):
    def setUp(self):
        # Redis-backed cache holds the `_fetch_pr_head_ref` lookup; clear
        # between tests so a stale cached head ref doesn't leak.
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="hook-user",
            email="hook@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.team = TeamMaster.objects.create(
            team_name="Hook Team",
            team_email="hook@team.com",
            owner=self.user,
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Hook Project",
            owner=self.user,
            project_system_user=self.user,
            code="HK",
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
        # For `issue_comment` events we need a registered webhook +
        # connected account on file so `_fetch_pr_head_ref` can find a
        # token to use.
        self.account = ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id="123",
            scopes=["repo", "read:user", "user:email"],
            access_token_encrypted="placeholder",
        )
        GithubWebhookRegistration.objects.create(
            owner="owner", repo="repo", hook_id=1, registered_by=self.user
        )

    def tearDown(self):
        cache.clear()

    # ── helpers ───────────────────────────────────────────────────

    def _post(
        self,
        payload: dict,
        *,
        event: str,
        secret: str = WEBHOOK_SECRET,
    ):
        body = json.dumps(payload).encode("utf-8")
        return self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body, secret),
            HTTP_X_GITHUB_EVENT=event,
        )

    @staticmethod
    def _pr_detail_response(head_ref: str):
        resp = MagicMock(spec=requests.Response)
        resp.ok = True
        resp.status_code = 200
        resp.json.return_value = {"head": {"ref": head_ref}}
        return resp

    # ── pull_request_review_comment (no API fetch needed) ─────────

    def test_review_comment_records_activity(self):
        resp = self._post(
            _review_comment_payload(head_ref="feature/HK-42-foo"),
            event="pull_request_review_comment",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["recorded"], 1)
        rows = TaskActivity.objects.filter(
            task=self.task, action_type=TaskActivityActionType.PR_COMMENT_ADDED
        )
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        # Metadata payload assertions — these are what the frontend
        # render branch consumes, so pin them.
        self.assertEqual(row.metadata["pr_url"], "https://github.com/owner/repo/pull/42")
        self.assertEqual(row.metadata["github_username"], "octocat")
        self.assertEqual(row.metadata["comment_kind"], "review")
        self.assertEqual(row.metadata["file_path"], "src/foo.py")
        self.assertEqual(row.metadata["line"], 17)
        self.assertEqual(row.metadata["comment_excerpt"], "Looks good to me.")
        self.assertIsNone(row.actor)

    def test_review_comment_excerpt_capped_at_280_chars(self):
        long_body = "x" * 500
        resp = self._post(
            _review_comment_payload(body=long_body),
            event="pull_request_review_comment",
        )
        self.assertEqual(resp.status_code, 200)
        row = TaskActivity.objects.get(
            task=self.task, action_type=TaskActivityActionType.PR_COMMENT_ADDED
        )
        self.assertEqual(len(row.metadata["comment_excerpt"]), 280)

    def test_review_comment_skips_action_other_than_created(self):
        # Edited/deleted are out of scope for v1.
        for action in ("edited", "deleted"):
            with self.subTest(action=action):
                resp = self._post(
                    _review_comment_payload(action=action),
                    event="pull_request_review_comment",
                )
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(
                    TaskActivity.objects.filter(
                        action_type=TaskActivityActionType.PR_COMMENT_ADDED
                    ).count(),
                    0,
                )

    def test_review_comment_no_matching_task(self):
        resp = self._post(
            _review_comment_payload(head_ref="feature/OTHER-99-foo"),
            event="pull_request_review_comment",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["recorded"], 0)
        self.assertEqual(
            TaskActivity.objects.filter(
                action_type=TaskActivityActionType.PR_COMMENT_ADDED
            ).count(),
            0,
        )

    def test_review_comment_no_aliasing_on_id_prefix(self):
        # HK-4 should NOT receive a comment from HK-42's branch.
        TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title="HK-4 task",
            status="Open",
            project_task_number=4,
        )
        resp = self._post(
            _review_comment_payload(head_ref="feature/HK-42-foo"),
            event="pull_request_review_comment",
        )
        self.assertEqual(resp.data["recorded"], 1)
        # Only the HK-42 task got the row.
        rows = TaskActivity.objects.filter(action_type=TaskActivityActionType.PR_COMMENT_ADDED)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().task_id, self.task.task_id)

    def test_review_comment_is_idempotent(self):
        payload = _review_comment_payload(comment_id=9999)
        # First delivery.
        self._post(payload, event="pull_request_review_comment")
        # Same delivery again (e.g. webhook retry).
        resp = self._post(payload, event="pull_request_review_comment")
        self.assertEqual(resp.status_code, 200)
        # Still only one row.
        self.assertEqual(
            TaskActivity.objects.filter(
                action_type=TaskActivityActionType.PR_COMMENT_ADDED,
                metadata__comment_id=9999,
            ).count(),
            1,
        )

    # ── issue_comment (PR fetch path) ─────────────────────────────

    @patch("origin.views.common.github_views._github_get")
    def test_pr_issue_comment_records_activity_via_fetch(self, mock_get):
        mock_get.return_value = self._pr_detail_response("feature/HK-42-from-fetch")
        resp = self._post(
            _issue_comment_payload(issue_number=42),
            event="issue_comment",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["recorded"], 1)
        row = TaskActivity.objects.get(
            task=self.task, action_type=TaskActivityActionType.PR_COMMENT_ADDED
        )
        self.assertEqual(row.metadata["comment_kind"], "issue")
        self.assertEqual(row.metadata["pr_url"], "https://github.com/owner/repo/pull/42")
        # Confirm we actually called the GitHub PR-detail endpoint.
        call_paths = [c.args[1] for c in mock_get.call_args_list]
        self.assertTrue(any("/repos/owner/repo/pulls/42" in p for p in call_paths))

    @patch("origin.views.common.github_views._github_get")
    def test_pr_head_ref_lookup_is_cached(self, mock_get):
        mock_get.return_value = self._pr_detail_response("feature/HK-42-cached")
        # First delivery hits GitHub once.
        self._post(_issue_comment_payload(comment_id=3001), event="issue_comment")
        first_calls = mock_get.call_count
        # Second delivery (different comment_id, same PR) reuses the cache.
        mock_get.reset_mock()
        self._post(_issue_comment_payload(comment_id=3002), event="issue_comment")
        self.assertEqual(mock_get.call_count, 0)
        # And both still resulted in activity rows.
        self.assertEqual(
            TaskActivity.objects.filter(
                action_type=TaskActivityActionType.PR_COMMENT_ADDED
            ).count(),
            2,
        )
        self.assertGreater(first_calls, 0)

    @patch("origin.views.common.github_views._github_get")
    def test_pr_head_ref_negative_lookup_is_cached(self, mock_get):
        # GitHub returns 404 (e.g. private repo we can no longer access).
        bad = MagicMock(spec=requests.Response)
        bad.ok = False
        bad.status_code = 404
        mock_get.return_value = bad
        self._post(_issue_comment_payload(), event="issue_comment")
        mock_get.reset_mock()
        # Repeated event — should not re-hit GitHub.
        self._post(_issue_comment_payload(comment_id=4001), event="issue_comment")
        self.assertEqual(mock_get.call_count, 0)
        self.assertEqual(
            TaskActivity.objects.filter(
                action_type=TaskActivityActionType.PR_COMMENT_ADDED
            ).count(),
            0,
        )

    def test_plain_issue_comment_is_ignored(self):
        # `issue.pull_request` not present → it's a regular issue
        # comment, not a PR comment. Out of scope.
        resp = self._post(
            _issue_comment_payload(is_pr=False),
            event="issue_comment",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["detail"], "ignored_non_pr")
        self.assertEqual(
            TaskActivity.objects.filter(
                action_type=TaskActivityActionType.PR_COMMENT_ADDED
            ).count(),
            0,
        )

    @patch("origin.views.common.github_views._github_get")
    def test_pr_issue_comment_action_other_than_created_is_noop(self, mock_get):
        # We don't even fetch the PR if action isn't created.
        for action in ("edited", "deleted"):
            with self.subTest(action=action):
                resp = self._post(
                    _issue_comment_payload(action=action),
                    event="issue_comment",
                )
                self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_get.call_count, 0)
        self.assertEqual(
            TaskActivity.objects.filter(
                action_type=TaskActivityActionType.PR_COMMENT_ADDED
            ).count(),
            0,
        )

    # ── Signature gate ────────────────────────────────────────────

    def test_invalid_signature_returns_401(self):
        body = json.dumps(_review_comment_payload()).encode()
        resp = self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=deadbeef",
            HTTP_X_GITHUB_EVENT="pull_request_review_comment",
        )
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(
            TaskActivity.objects.filter(
                action_type=TaskActivityActionType.PR_COMMENT_ADDED
            ).count(),
            0,
        )
