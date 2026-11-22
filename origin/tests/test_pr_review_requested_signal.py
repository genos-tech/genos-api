"""MIP-2054: a `pull_request` delivery with `review_requested` reaches the reviewer through
existing surfaces only: an Inbox item of kind 0, one comment on the linked task, and the
briefing's `waiting_on_me.pr_reviews`. Events GitHub does not send us today (reviews, CI) are not
handled; `test_hook_events_do_not_include_reviews_or_checks` pins that so a later change is a
conscious one."""

import hashlib
import hmac
import json

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import github_webhooks, pr_needs_you

User = get_user_model()
SECRET = "test-secret-1234567890"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _payload(*, reviewer_id=777, head_ref="feature/RV-7-foo", draft=False, team=False) -> dict:
    payload = {
        "action": "review_requested",
        "number": 9,
        "pull_request": {
            "html_url": "https://github.com/owner/repo/pull/9",
            "number": 9,
            "title": "Fix [the] thing",
            "draft": draft,
            "merged": False,
            "state": "open",
            "head": {"ref": head_ref, "sha": "abc"},
            "base": {"ref": "main", "repo": {"full_name": "owner/repo"}},
        },
        "repository": {"full_name": "owner/repo"},
    }
    if team:
        payload["requested_team"] = {"name": "core", "id": 5}
    else:
        payload["requested_reviewer"] = {"login": "rev-login", "id": reviewer_id}
    return payload


@override_settings(GITHUB_WEBHOOK_SECRET=SECRET)
class TestReviewRequestedSignal(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.author = User.objects.create_user(
            username="author", email="author@test.com", password="x", is_email_verified=True
        )
        self.reviewer = User.objects.create_user(
            username="reviewer", email="rev@test.com", password="x", is_email_verified=True
        )
        self.outsider = User.objects.create_user(
            username="outsider", email="out@test.com", password="x", is_email_verified=True
        )
        self.team = TeamMaster.objects.create(
            team_name="RV Team", team_email="rv@team.com", owner=self.author
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="RV Project",
            owner=self.author,
            project_system_user=self.author,
            code="RV",
        )
        for u in (self.author, self.reviewer):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=u)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.author,
            reporter=self.author,
            title="Reviewed task",
            status="WIP",
            project_task_number=7,
        )
        ConnectedAccount.objects.create(
            user=self.author,
            provider="github",
            provider_user_id="123",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        ConnectedAccount.objects.create(
            user=self.reviewer,
            provider="github",
            provider_user_id="777",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        ConnectedAccount.objects.create(
            user=self.outsider,
            provider="github",
            provider_user_id="888",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        GithubWebhookRegistration.objects.create(
            owner="owner", repo="repo", hook_id=1, registered_by=self.author
        )

    def tearDown(self):
        cache.clear()

    def _post(self, payload):
        body = json.dumps(payload).encode()
        return self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body),
            HTTP_X_GITHUB_EVENT="pull_request",
        )

    def _items(self, user):
        return InboxItems.objects.filter(
            receiver=user, item_optionals__kind=pr_needs_you.KIND_REVIEW_REQUESTED
        )

    def _comments(self):
        return TaskComments.objects.filter(task=self.task, sender__isnull=True)

    def test_writes_inbox_item_and_task_comment(self):
        resp = self._post(_payload())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["notified"], 1)
        item = self._items(self.reviewer).get()
        self.assertEqual(item.item_type, 0)
        self.assertEqual(item.request_status, "")
        self.assertFalse(item.is_read)
        self.assertEqual(item.item_optionals["task_id"], self.task.task_id)
        self.assertEqual(item.item_optionals["pr_url"], "https://github.com/owner/repo/pull/9")
        self.assertEqual(self._comments().count(), 1)
        text = json.dumps(self._comments().get().comment_body)
        self.assertIn("review of", text)
        self.assertNotIn("[the]", text)  # GitHub title stripped of markdown syntax

    def test_redelivery_is_idempotent(self):
        self._post(_payload())
        resp = self._post(_payload())
        self.assertEqual(resp.data["notified"], 0)
        self.assertEqual(self._items(self.reviewer).count(), 1)
        self.assertEqual(self._comments().count(), 1)

    def test_author_is_not_told(self):
        self._post(_payload())
        self.assertEqual(self._items(self.author).count(), 0)

    def test_unconnected_github_id_writes_nothing(self):
        resp = self._post(_payload(reviewer_id=999))
        self.assertEqual(resp.data["notified"], 0)
        self.assertEqual(InboxItems.objects.count(), 0)
        self.assertEqual(self._comments().count(), 0)

    def test_reviewer_without_task_access_writes_nothing(self):
        resp = self._post(_payload(reviewer_id=888))
        self.assertEqual(resp.data["notified"], 0)
        self.assertEqual(self._items(self.outsider).count(), 0)
        self.assertEqual(self._comments().count(), 0)

    def test_draft_pr_writes_nothing(self):
        resp = self._post(_payload(draft=True))
        self.assertNotEqual(resp.data.get("notified"), 1)
        self.assertEqual(self._items(self.reviewer).count(), 0)

    def test_team_reviewer_is_ignored(self):
        resp = self._post(_payload(team=True))
        self.assertEqual(resp.data["notified"], 0)
        self.assertEqual(InboxItems.objects.count(), 0)

    def test_pr_without_linked_task_writes_nothing(self):
        resp = self._post(_payload(head_ref="feature/no-task-here"))
        self.assertEqual(resp.data["notified"], 0)
        self.assertEqual(InboxItems.objects.count(), 0)

    def test_pending_rows_follow_unread_state(self):
        self._post(_payload())
        rows = pr_needs_you.pending_review_rows(self.reviewer.id, self.team.team_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_id"], self.task.task_id)
        self.assertEqual(pr_needs_you.pending_review_rows(self.author.id, self.team.team_id), [])
        self._items(self.reviewer).update(is_read=True)
        self.assertEqual(pr_needs_you.pending_review_rows(self.reviewer.id, self.team.team_id), [])

    def test_hook_events_do_not_include_reviews_or_checks(self):
        # Findings for MIP-2054: changes requested / approved (`pull_request_review`) and CI
        # (`check_suite`, `status`) are not delivered to Genos today.
        for event in ("pull_request_review", "check_suite", "status"):
            self.assertNotIn(event, github_webhooks._HOOK_EVENTS)
        self.assertIn("pull_request", github_webhooks._HOOK_EVENTS)
