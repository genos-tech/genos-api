"""Tests for the GitHub repo webhook (PR merge → task status sync)."""

import hashlib
import hmac
import json

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster

User = get_user_model()

WEBHOOK_SECRET = "test-secret-1234567890"


def _sign(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _pr_payload(*, action: str, merged: bool, html_url: str) -> dict:
    return {
        "action": action,
        "pull_request": {
            "html_url": html_url,
            "merged": merged,
            "number": 42,
            "title": "Test PR",
            "state": "closed" if merged else "open",
            "head": {"sha": "abc123"},
            "base": {"repo": {"full_name": "owner/repo"}},
        },
        "repository": {"full_name": "owner/repo"},
    }


@override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
class TestGithubWebhook(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="webhookuser",
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
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _create_task(self, *, status_value: str = "Open", links=None) -> TaskMaster:
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title="Test task",
            status=status_value,
            links=links or [],
        )

    def _post_webhook(
        self,
        payload: dict,
        *,
        event: str = "pull_request",
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

    # ── Signature verification ────────────────────────────────────

    def test_missing_signature_returns_401(self):
        payload = _pr_payload(action="closed", merged=True, html_url="x")
        response = self.client.post(
            "/api/v2/github/webhook/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(response.status_code, 401)

    def test_wrong_signature_returns_401(self):
        body = json.dumps(_pr_payload(action="closed", merged=True, html_url="x")).encode()
        response = self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=deadbeef",
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(response.status_code, 401)

    def test_signed_with_different_secret_returns_401(self):
        body = json.dumps(_pr_payload(action="closed", merged=True, html_url="x")).encode()
        response = self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body, "different-secret"),
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(response.status_code, 401)

    # ── Event routing ─────────────────────────────────────────────

    def test_ping_event_returns_200(self):
        response = self._post_webhook({"zen": "Speak like a human."}, event="ping")
        self.assertEqual(response.status_code, 200)

    def test_unrelated_event_returns_200_noop(self):
        response = self._post_webhook(
            _pr_payload(action="closed", merged=True, html_url="x"), event="push"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["detail"], "ignored")

    def test_pr_opened_does_nothing(self):
        url = "https://github.com/owner/repo/pull/42"
        task = self._create_task(links=[{"id": "l1", "url": url, "title": "T", "isGitHub": True}])
        response = self._post_webhook(_pr_payload(action="opened", merged=False, html_url=url))
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")

    def test_pr_closed_without_merge_does_nothing(self):
        url = "https://github.com/owner/repo/pull/42"
        task = self._create_task(links=[{"id": "l1", "url": url, "title": "T", "isGitHub": True}])
        response = self._post_webhook(_pr_payload(action="closed", merged=False, html_url=url))
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")

    # ── PR merge → status transition ──────────────────────────────

    def test_merged_pr_transitions_open_task_to_closed(self):
        url = "https://github.com/owner/repo/pull/42"
        task = self._create_task(links=[{"id": "l1", "url": url, "title": "T", "isGitHub": True}])
        response = self._post_webhook(_pr_payload(action="closed", merged=True, html_url=url))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["updated"], 1)
        task.refresh_from_db()
        self.assertEqual(task.status, "Closed")

    def test_merged_pr_fans_out_to_multiple_tasks(self):
        url = "https://github.com/owner/repo/pull/77"
        link = {"id": "l1", "url": url, "title": "T", "isGitHub": True}
        t1 = self._create_task(links=[link])
        t2 = self._create_task(links=[link])
        response = self._post_webhook(_pr_payload(action="closed", merged=True, html_url=url))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["updated"], 2)
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.status, "Closed")
        self.assertEqual(t2.status, "Closed")

    def test_merged_pr_does_not_clobber_already_closed_task(self):
        url = "https://github.com/owner/repo/pull/42"
        task = self._create_task(
            status_value="Closed",
            links=[{"id": "l1", "url": url, "title": "T", "isGitHub": True}],
        )
        first_updated = task.ts_updated_at
        response = self._post_webhook(_pr_payload(action="closed", merged=True, html_url=url))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["updated"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "Closed")
        # ts_updated_at must NOT have moved — confirms we skipped the save.
        self.assertEqual(task.ts_updated_at, first_updated)

    def test_merged_pr_skips_unlinked_tasks(self):
        url = "https://github.com/owner/repo/pull/42"
        unrelated = self._create_task()
        response = self._post_webhook(_pr_payload(action="closed", merged=True, html_url=url))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["updated"], 0)
        unrelated.refresh_from_db()
        self.assertEqual(unrelated.status, "Open")

    def test_merged_pr_skips_deleted_tasks(self):
        url = "https://github.com/owner/repo/pull/42"
        task = self._create_task(links=[{"id": "l1", "url": url, "title": "T", "isGitHub": True}])
        task.is_deleted = True
        task.save(update_fields=["is_deleted"])
        response = self._post_webhook(_pr_payload(action="closed", merged=True, html_url=url))
        self.assertEqual(response.data["updated"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")

    def test_substring_match_rejected_by_strict_url_check(self):
        # The icontains query is wide on purpose (fast initial filter) but
        # then we walk the JSON list and require exact URL equality. A
        # task whose link is /pull/4 should NOT match /pull/42's webhook.
        url_short = "https://github.com/owner/repo/pull/4"
        url_long = "https://github.com/owner/repo/pull/42"
        task_short = self._create_task(
            links=[{"id": "l1", "url": url_short, "title": "T", "isGitHub": True}]
        )
        response = self._post_webhook(_pr_payload(action="closed", merged=True, html_url=url_long))
        self.assertEqual(response.data["updated"], 0)
        task_short.refresh_from_db()
        self.assertEqual(task_short.status, "Open")


@override_settings(GITHUB_WEBHOOK_SECRET="")
class TestGithubWebhookSecretUnset(TestCase):
    """With no secret configured, the endpoint must refuse all requests
    — otherwise it would silently trust any sender."""

    def test_empty_secret_rejects_all(self):
        client = APIClient()
        body = json.dumps(_pr_payload(action="closed", merged=True, html_url="x")).encode()
        response = client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body, "anything"),
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(response.status_code, 401)
