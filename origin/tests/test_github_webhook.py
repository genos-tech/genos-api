"""Tests for the GitHub repo webhook (PR merge → task status sync).

Behavior under test:
- Match path: PR's head branch ref contains a task's display ID
  (e.g. `feature/GEN-42-foo` matches task GEN-42), word-boundary regex.
- Opt-in gate: the task's assignee must have
  `auto_close_on_pr_merge=True`. Default is OFF for every user.
- `task.links` is *not* used here anymore — auto-link by branch name is
  the single source of truth for "what PR belongs to what task."
"""

import hashlib
import hmac
import json

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster

User = get_user_model()

WEBHOOK_SECRET = "test-secret-1234567890"


def _sign(payload_bytes: bytes, secret: str = WEBHOOK_SECRET) -> str:
    sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={sig}"


def _pr_payload(
    *,
    action: str,
    merged: bool,
    html_url: str = "https://github.com/owner/repo/pull/42",
    head_ref: str = "main",
) -> dict:
    return {
        "action": action,
        "pull_request": {
            "html_url": html_url,
            "merged": merged,
            "number": 42,
            "title": "Test PR",
            "state": "closed" if merged else "open",
            "head": {"sha": "abc123", "ref": head_ref},
            "base": {"repo": {"full_name": "owner/repo"}, "ref": "main"},
        },
        "repository": {"full_name": "owner/repo"},
    }


@override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
class TestGithubWebhook(TestCase):
    def setUp(self):
        self.client = APIClient()
        # Default user opts INTO auto-close so the happy-path tests
        # exercise the close branch. The opt-out test flips it off.
        self.user = User.objects.create_user(
            username="webhookuser",
            email="hook@test.com",
            password="testpass123",
            is_email_verified=True,
            auto_close_on_pr_merge=True,
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

    def _create_task(
        self,
        *,
        status_value: str = "Open",
        project_task_number: int | None = None,
        assignee=None,
    ) -> TaskMaster:
        # project_task_number is normally assigned by the post-save
        # signal, but we accept an explicit override so tests can pin
        # the display ID to a known value (HK-42, HK-99, …).
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=assignee or self.user,
            reporter=self.user,
            title="Test task",
            status=status_value,
            project_task_number=project_task_number,
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
        payload = _pr_payload(action="closed", merged=True)
        response = self.client.post(
            "/api/v2/github/webhook/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(response.status_code, 401)

    def test_wrong_signature_returns_401(self):
        body = json.dumps(_pr_payload(action="closed", merged=True)).encode()
        response = self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=deadbeef",
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(response.status_code, 401)

    def test_signed_with_different_secret_returns_401(self):
        body = json.dumps(_pr_payload(action="closed", merged=True)).encode()
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
        response = self._post_webhook(_pr_payload(action="closed", merged=True), event="push")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["detail"], "ignored")

    def test_pr_opened_does_not_change_status(self):
        # Opening a PR records a link activity (see the link tests below)
        # but must NOT transition the task — only a merge closes it.
        task = self._create_task(project_task_number=42)
        response = self._post_webhook(
            _pr_payload(action="opened", merged=False, head_ref="feature/HK-42-foo")
        )
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")

    def test_pr_closed_without_merge_does_nothing(self):
        task = self._create_task(project_task_number=42)
        response = self._post_webhook(
            _pr_payload(action="closed", merged=False, head_ref="feature/HK-42-foo")
        )
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")

    # ── PR merge → status transition (opt-in path) ────────────────

    def test_merged_pr_closes_task_whose_display_id_is_in_branch(self):
        task = self._create_task(project_task_number=42)
        response = self._post_webhook(
            _pr_payload(action="closed", merged=True, head_ref="feature/HK-42-add-thing")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["updated"], 1)
        task.refresh_from_db()
        self.assertEqual(task.status, "Closed")

    def test_merged_pr_matches_at_string_start(self):
        task = self._create_task(project_task_number=42)
        response = self._post_webhook(_pr_payload(action="closed", merged=True, head_ref="HK-42"))
        self.assertEqual(response.data["updated"], 1)
        task.refresh_from_db()
        self.assertEqual(task.status, "Closed")

    def test_merged_pr_close_tags_activity_with_pr_merge_metadata(self):
        """The auto-close audit row must be distinguishable from a manual
        status edit: the unauthenticated webhook has no actor, so the feed
        relies on `closedByPrMerge` + `prUrl` metadata to attribute the
        close to the merged PR instead of an anonymous "Someone"."""
        task = self._create_task(project_task_number=42)
        url = "https://github.com/owner/repo/pull/42"
        response = self._post_webhook(
            _pr_payload(action="closed", merged=True, head_ref="HK-42", html_url=url)
        )
        self.assertEqual(response.data["updated"], 1)

        row = (
            TaskActivity.objects.filter(task=task, action_type="status_changed")
            .order_by("-ts_created_at")
            .first()
        )
        self.assertIsNotNone(row)
        # Actor is null (webhook is unauthenticated) — the metadata flag,
        # not the actor, is what marks this as a PR-merge close.
        self.assertIsNone(row.actor)
        self.assertEqual(row.new_value, "Closed")
        self.assertTrue(row.metadata.get("closedByPrMerge"))
        self.assertEqual(row.metadata.get("prUrl"), url)

    def test_manual_status_change_is_not_tagged_as_pr_merge(self):
        """A plain status edit (no webhook) must NOT carry the PR-merge
        flag, so the feed only specialises genuine auto-closes."""
        task = self._create_task(project_task_number=77)
        task.status = "Closed"
        task.save(update_fields=["status", "ts_updated_at"])

        row = (
            TaskActivity.objects.filter(task=task, action_type="status_changed")
            .order_by("-ts_created_at")
            .first()
        )
        self.assertIsNotNone(row)
        self.assertNotIn("closedByPrMerge", row.metadata)

    def test_merged_pr_matches_case_insensitive(self):
        task = self._create_task(project_task_number=42)
        response = self._post_webhook(
            _pr_payload(action="closed", merged=True, head_ref="feature/hk-42-foo")
        )
        self.assertEqual(response.data["updated"], 1)
        task.refresh_from_db()
        self.assertEqual(task.status, "Closed")

    # ── Word-boundary regex (no aliasing) ─────────────────────────

    def test_does_not_close_task_whose_id_is_a_prefix(self):
        # HK-4 should NOT be closed by a branch named HK-42-foo. The
        # word-boundary regex prevents this aliasing.
        task = self._create_task(project_task_number=4)
        response = self._post_webhook(
            _pr_payload(action="closed", merged=True, head_ref="feature/HK-42-foo")
        )
        self.assertEqual(response.data["updated"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")

    def test_does_not_close_when_branch_has_no_matching_id(self):
        task = self._create_task(project_task_number=42)
        response = self._post_webhook(_pr_payload(action="closed", merged=True, head_ref="main"))
        self.assertEqual(response.data["updated"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")

    # ── Per-user opt-in gate ──────────────────────────────────────

    def test_does_not_close_when_assignee_has_not_opted_in(self):
        opted_out = User.objects.create_user(
            username="opted-out",
            email="out@test.com",
            password="x",
            is_email_verified=True,
            auto_close_on_pr_merge=False,
        )
        # Add opted-out user to project so they can own a task.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=opted_out)
        task = self._create_task(project_task_number=42, assignee=opted_out)
        response = self._post_webhook(
            _pr_payload(action="closed", merged=True, head_ref="feature/HK-42-foo")
        )
        # The branch matches and the merge fired, but the assignee
        # didn't opt in — so we don't touch the task.
        self.assertEqual(response.data["updated"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")

    def test_closes_only_opted_in_assignees_when_multiple_tasks_match(self):
        # Two tasks could match a branch like "HK-42-and-HK-99-merge".
        # Only the one whose assignee opted in should close.
        opted_out = User.objects.create_user(
            username="opted-out-fan",
            email="out-fan@test.com",
            password="x",
            is_email_verified=True,
            auto_close_on_pr_merge=False,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=opted_out)
        t_in = self._create_task(project_task_number=42)  # assignee=self.user (opted in)
        t_out = self._create_task(project_task_number=99, assignee=opted_out)
        response = self._post_webhook(
            _pr_payload(action="closed", merged=True, head_ref="feature/HK-42-and-HK-99")
        )
        self.assertEqual(response.data["updated"], 1)
        t_in.refresh_from_db()
        t_out.refresh_from_db()
        self.assertEqual(t_in.status, "Closed")
        self.assertEqual(t_out.status, "Open")

    # ── Already-done + deleted guards ─────────────────────────────

    def test_merged_pr_does_not_clobber_already_closed_task(self):
        task = self._create_task(project_task_number=42, status_value="Closed")
        first_updated = task.ts_updated_at
        response = self._post_webhook(_pr_payload(action="closed", merged=True, head_ref="HK-42"))
        self.assertEqual(response.data["updated"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "Closed")
        self.assertEqual(task.ts_updated_at, first_updated)

    def test_merged_pr_skips_deleted_tasks(self):
        task = self._create_task(project_task_number=42)
        task.is_deleted = True
        task.save(update_fields=["is_deleted"])
        response = self._post_webhook(_pr_payload(action="closed", merged=True, head_ref="HK-42"))
        self.assertEqual(response.data["updated"], 0)
        task.refresh_from_db()
        self.assertEqual(task.status, "Open")

    def test_merged_pr_skips_orphan_tasks_with_no_display_id(self):
        # A task without a project has display_id like "#<id>" which
        # would alias every task in the system — the close path bails
        # out for these explicitly.
        orphan = TaskMaster.objects.create(
            team=self.team,
            project=None,
            assignee=self.user,
            reporter=self.user,
            title="Orphan",
            status="Open",
        )
        response = self._post_webhook(
            _pr_payload(action="closed", merged=True, head_ref=f"#{orphan.task_id}")
        )
        self.assertEqual(response.data["updated"], 0)
        orphan.refresh_from_db()
        self.assertEqual(orphan.status, "Open")

    # ── PR opened → "PR linked" activity (branch carries display id) ──

    def _linked_rows(self, task):
        return TaskActivity.objects.filter(task=task, action_type="pr_linked")

    def test_pr_opened_records_link_activity_when_branch_matches(self):
        task = self._create_task(project_task_number=42)
        url = "https://github.com/owner/repo/pull/7"
        response = self._post_webhook(
            _pr_payload(
                action="opened", merged=False, head_ref="feature/HK-42-thing", html_url=url
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["linked"], 1)

        rows = self._linked_rows(task)
        self.assertEqual(rows.count(), 1)
        row = rows.first()
        self.assertIsNone(row.actor)
        self.assertEqual(row.metadata.get("pr_url"), url)
        self.assertEqual(row.metadata.get("branch"), "feature/HK-42-thing")
        self.assertEqual(row.metadata.get("pr_number"), 42)

    def test_pr_reopened_also_records_link_activity(self):
        task = self._create_task(project_task_number=42)
        response = self._post_webhook(
            _pr_payload(action="reopened", merged=False, head_ref="HK-42")
        )
        self.assertEqual(response.data["linked"], 1)
        self.assertEqual(self._linked_rows(task).count(), 1)

    def test_pr_opened_does_not_link_when_branch_has_no_matching_id(self):
        task = self._create_task(project_task_number=42)
        response = self._post_webhook(
            _pr_payload(action="opened", merged=False, head_ref="feature/no-task-here")
        )
        self.assertEqual(response.data["linked"], 0)
        self.assertEqual(self._linked_rows(task).count(), 0)

    def test_pr_opened_does_not_link_prefix_id_collision(self):
        # Branch "HK-420" must not link task HK-42 (word-boundary regex).
        task = self._create_task(project_task_number=42)
        response = self._post_webhook(
            _pr_payload(action="opened", merged=False, head_ref="feature/HK-420-foo")
        )
        self.assertEqual(response.data["linked"], 0)
        self.assertEqual(self._linked_rows(task).count(), 0)

    def test_pr_open_link_is_idempotent_across_redeliveries(self):
        task = self._create_task(project_task_number=42)
        payload = _pr_payload(
            action="opened",
            merged=False,
            head_ref="HK-42",
            html_url="https://github.com/owner/repo/pull/7",
        )
        first = self._post_webhook(payload)
        second = self._post_webhook(payload)
        self.assertEqual(first.data["linked"], 1)
        self.assertEqual(second.data["linked"], 0)
        self.assertEqual(self._linked_rows(task).count(), 1)


@override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
class TestPullRequestWebhookInvalidatesCaches(TestCase):
    """When a `pull_request` event arrives, our Redis-cached lookups for
    that PR / its head branch must be dropped so the UI surfaces the new
    state on the next fetch (rather than serving up to 60s of stale TTL).

    Comment events (`issue_comment`, `pull_request_review_comment`) do
    NOT trigger invalidation because they don't mutate any cached field.
    """

    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def tearDown(self):
        cache.clear()

    def _post(self, payload: dict, *, event: str = "pull_request"):
        body = json.dumps(payload).encode("utf-8")
        return self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body),
            HTTP_X_GITHUB_EVENT=event,
        )

    # The cache key shapes here must stay aligned with the helpers in
    # `github_views.py`. We assert against the exact keys so a silent
    # rename of either side surfaces here instead of in prod.
    @staticmethod
    def _seed_pr_caches(owner: str, repo: str, branch: str, number: int):
        cache.set(f"gh:branches:{owner.lower()}:{repo.lower()}", [{"name": branch}], 60)
        cache.set(
            f"gh:head_pull:{owner.lower()}:{repo.lower()}:{branch}",
            {"number": number, "state": "open"},
            60,
        )
        cache.set(
            f"gh:pr_by_number:{owner.lower()}:{repo.lower()}:{number}",
            {"number": number, "state": "open"},
            60,
        )
        cache.set(
            f"gh:pr_head_ref:{owner.lower()}:{repo.lower()}:{number}",
            branch,
            60,
        )

    @staticmethod
    def _cached_keys_present(owner: str, repo: str, branch: str, number: int) -> list[str]:
        keys = [
            f"gh:branches:{owner.lower()}:{repo.lower()}",
            f"gh:head_pull:{owner.lower()}:{repo.lower()}:{branch}",
            f"gh:pr_by_number:{owner.lower()}:{repo.lower()}:{number}",
            f"gh:pr_head_ref:{owner.lower()}:{repo.lower()}:{number}",
        ]
        return [k for k in keys if cache.get(k) is not None]

    def test_pull_request_opened_invalidates_all_pr_caches(self):
        self._seed_pr_caches("owner", "repo", "feature/abc", 42)
        self.assertEqual(len(self._cached_keys_present("owner", "repo", "feature/abc", 42)), 4)
        self._post(_pr_payload(action="opened", merged=False, head_ref="feature/abc"))
        # All four keys should be gone after invalidation.
        self.assertEqual(self._cached_keys_present("owner", "repo", "feature/abc", 42), [])

    def test_pull_request_merged_invalidates_all_pr_caches(self):
        # The merge path also needs invalidation — without it the UI
        # would render "open" for up to 60s after the merge fires.
        self._seed_pr_caches("owner", "repo", "feature/abc", 42)
        self._post(_pr_payload(action="closed", merged=True, head_ref="feature/abc"))
        self.assertEqual(self._cached_keys_present("owner", "repo", "feature/abc", 42), [])

    def test_pull_request_event_for_different_pr_does_not_touch_other_caches(self):
        # Seed caches for two different PRs in the same repo. An event
        # for PR #42 must only drop #42's keys; PR #99's stay.
        self._seed_pr_caches("owner", "repo", "feature/abc", 42)
        self._seed_pr_caches("owner", "repo", "feature/xyz", 99)
        # An event for #42 still drops the per-repo branch list (which
        # is shared — there's no way to invalidate just one branch's
        # entry inside that list).
        self._post(
            _pr_payload(
                action="opened",
                merged=False,
                head_ref="feature/abc",
                html_url="https://github.com/owner/repo/pull/42",
            )
        )
        # PR #99's per-PR keys survive.
        self.assertIsNotNone(cache.get("gh:head_pull:owner:repo:feature/xyz"))
        self.assertIsNotNone(cache.get("gh:pr_by_number:owner:repo:99"))
        self.assertIsNotNone(cache.get("gh:pr_head_ref:owner:repo:99"))
        # PR #42's keys are gone.
        self.assertIsNone(cache.get("gh:head_pull:owner:repo:feature/abc"))
        self.assertIsNone(cache.get("gh:pr_by_number:owner:repo:42"))

    def test_unparseable_pr_url_skips_invalidation(self):
        # If the PR URL doesn't parse (malformed payload), we silently
        # skip invalidation rather than blow up.
        self._seed_pr_caches("owner", "repo", "feature/abc", 42)
        self._post(
            _pr_payload(
                action="opened",
                merged=False,
                head_ref="feature/abc",
                html_url="not-a-real-url",
            )
        )
        self.assertEqual(len(self._cached_keys_present("owner", "repo", "feature/abc", 42)), 4)


@override_settings(GITHUB_WEBHOOK_SECRET="")
class TestGithubWebhookSecretUnset(TestCase):
    """With no secret configured, the endpoint must refuse all requests
    — otherwise it would silently trust any sender."""

    def test_empty_secret_rejects_all(self):
        client = APIClient()
        body = json.dumps(_pr_payload(action="closed", merged=True)).encode()
        response = client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body, "anything"),
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(response.status_code, 401)
