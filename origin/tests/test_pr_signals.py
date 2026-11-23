"""MIP-2062: what happened to a PR reaches its AUTHOR through existing surfaces only (an Inbox item of
kind 0, one comment on the linked task, the briefing's `waiting_on_me.pr_updates`), from the GitHub App
webhook's `pull_request_review`, `check_run` and `status` events.

The rules these pin, per signal (changes requested, approved, CI failing, approved and green):
  * one item and one comment for the author, and only the author, only if they can open the task;
  * a redelivery under a new delivery id writes nothing more (dedupe);
  * CI failing is debounced: many check runs, many commits, one notice per window;
  * "ready" needs an approval AND settled green CI on the head commit, read live from GitHub; an
    approval that is already green is ONE notice, not two;
  * a team the installation is not linked to gets nothing, and an event with no open linked PR makes
    no GitHub call at all;
  * a closed PR retires its unread signals.
GitHub is faked: no request leaves the test process.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.common.github_app_models import GithubAppInstallationTeam
from origin.models.common.inbox_models import InboxItems
from origin.models.common.user_models import ConnectedAccount
from origin.models.task.task_delivery_models import TaskDeliverySource, TaskDeliveryState
from origin.models.task.task_models import TaskComments
from origin.services import pr_needs_you as needs_you
from origin.services import pr_signal_reader as reader_mod
from origin.services.briefing import waiting_on_me
from origin.services.task_delivery import record_delivery
from origin.tests.test_github_app_webhook import AppWebhookBase, _with_installation
from origin.tests.test_task_delivery import _pr_event

User = get_user_model()
SHA = "abc1234def5678"
PR_URL = "https://github.com/owner/repo/pull/42"
AUTHOR_GH, REVIEWER_GH = 123, 777


def _review(state="approved", *, review_id=9001, reviewer=REVIEWER_GH, author=AUTHOR_GH, **kw):
    payload = _pr_event("submitted")
    pr = payload["pull_request"]
    pr["head"]["sha"] = kw.get("sha", SHA)
    pr["user"] = {"id": author, "login": "the-author"}
    pr["draft"] = kw.get("draft", False)
    pr["state"] = kw.get("pr_state", "open")
    payload.update(
        action=kw.get("action", "submitted"),
        review={"id": review_id, "state": state, "user": {"id": reviewer, "login": "rev<b>"}},
    )
    payload["repository"]["id"] = 11
    return _with_installation(payload)


def _check_run(conclusion="failure", *, name="backend-test", sha=SHA, status="completed"):
    return _with_installation(
        {
            "action": "completed",
            "check_run": {
                "id": 1,
                "name": name,
                "status": status,
                "conclusion": conclusion,
                "head_sha": sha,
                "pull_requests": [],
            },
            "repository": {"full_name": "owner/repo", "id": 11},
        }
    )


def _status(state="failure", *, context="ci/legacy", sha=SHA):
    return _with_installation(
        {
            "state": state,
            "sha": sha,
            "context": context,
            "repository": {"full_name": "owner/repo", "id": 11},
        }
    )


class FakeGithub:
    """What GitHub says, and how often it was asked."""

    def __init__(self):
        self.pulls = [
            {
                "number": 42,
                "title": "Fix it",
                "state": "open",
                "draft": False,
                "author_id": AUTHOR_GH,
                "sha": SHA,
                "ref": "feature/HK-42-fix",
            }
        ]
        self.reviews_ = [{"state": "APPROVED", "user": {"id": REVIEWER_GH}}]
        self.checks = {
            "total_count": 1,
            "check_runs": [{"status": "completed", "conclusion": "success"}],
        }
        self.combined = {"total_count": 0, "state": "pending"}
        self.calls: list[str] = []

    def pulls_for_commit(self, owner, repo, sha):
        self.calls.append("pulls")
        return [p for p in self.pulls if p["sha"] == sha]

    def reviews(self, owner, repo, number):
        self.calls.append("reviews")
        return self.reviews_

    def check_runs(self, owner, repo, sha):
        self.calls.append("checks")
        return self.checks

    def combined_status(self, owner, repo, sha):
        self.calls.append("combined")
        return self.combined


class SignalBase(AppWebhookBase):
    def setUp(self):
        super().setUp()
        self.gh = FakeGithub()
        patcher = mock.patch(
            "origin.views.common.github_app_webhook_views.InstallationReader",
            return_value=self.gh,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        ConnectedAccount.objects.create(
            user=self.user,
            provider="github",
            provider_user_id=str(AUTHOR_GH),
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        record_delivery(
            task=self.task,
            owner="owner",
            repo="repo",
            branch="feature/HK-42-fix",
            pr_number=42,
            state=TaskDeliveryState.OPEN,
            source=TaskDeliverySource.WEBHOOK,
        )

    def items(self, kind=None, user=None):
        rows = InboxItems.objects.filter(receiver=user or self.user)
        rows = rows.filter(item_optionals__kind__in=[kind] if kind else needs_you.SIGNAL_KINDS)
        return rows

    def comments(self):
        return TaskComments.objects.filter(task=self.task, sender__isnull=True)

    def comment_text(self):
        return " ".join(str(c.comment_body) for c in self.comments())

    def post(self, payload, event):
        resp = self._post(payload, event=event)
        self.assertEqual(resp.status_code, 200, resp.data)
        return resp


class TestChangesRequested(SignalBase):
    def test_writes_an_item_and_one_comment_for_the_author(self):
        resp = self.post(_review("changes_requested"), "pull_request_review")
        self.assertEqual(resp.data["notified"], 1)
        item = self.items(needs_you.KIND_CHANGES_REQUESTED).get()
        self.assertEqual(item.item_type, 0)
        self.assertFalse(item.is_read)
        self.assertEqual(item.item_optionals["task_id"], self.task.task_id)
        self.assertEqual(item.item_optionals["pr_url"], PR_URL)
        self.assertEqual(self.comments().count(), 1)
        self.assertIn("requested changes", self.comment_text())
        self.assertNotIn("<b>", self.comment_text())  # GitHub text stripped of markup

    def test_a_redelivery_of_the_same_review_writes_nothing_more(self):
        self.post(_review("changes_requested"), "pull_request_review")
        resp = self.post(_review("changes_requested"), "pull_request_review")
        self.assertEqual(resp.data["notified"], 0)
        self.assertEqual(self.items().count(), 1)
        self.assertEqual(self.comments().count(), 1)

    def test_a_second_review_is_a_second_notice(self):
        self.post(_review("changes_requested"), "pull_request_review")
        self.post(_review("changes_requested", review_id=9002), "pull_request_review")
        self.assertEqual(self.items().count(), 2)

    def test_nothing_for_a_comment_review_an_edit_or_a_closed_pr(self):
        self.post(_review("commented"), "pull_request_review")
        self.post(_review("changes_requested", action="edited"), "pull_request_review")
        self.post(_review("changes_requested", pr_state="closed"), "pull_request_review")
        self.assertEqual(self.items().count(), 0)
        self.assertEqual(self.comments().count(), 0)

    def test_nobody_is_told_about_their_own_review(self):
        self.post(_review("changes_requested", reviewer=AUTHOR_GH), "pull_request_review")
        self.assertEqual(self.items().count(), 0)

    def test_an_author_without_a_connected_github_account_is_not_told(self):
        self.post(_review("changes_requested", author=999), "pull_request_review")
        self.assertEqual(InboxItems.objects.count(), 0)
        self.assertEqual(self.comments().count(), 0)

    def test_an_author_who_cannot_open_the_task_is_not_told(self):
        outsider = User.objects.create_user(
            username="outsider", email="out@test.com", password="x", is_email_verified=True
        )
        ConnectedAccount.objects.create(
            user=outsider,
            provider="github",
            provider_user_id="888",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        self.post(_review("changes_requested", author=888), "pull_request_review")
        self.assertEqual(InboxItems.objects.count(), 0)
        self.assertEqual(self.comments().count(), 0)

    def test_a_pr_with_no_linked_task_writes_nothing(self):
        payload = _review("changes_requested")
        payload["pull_request"]["head"]["ref"] = "feature/no-task-here"
        payload["pull_request"]["number"] = 77
        payload["pull_request"]["html_url"] = "https://github.com/owner/repo/pull/77"
        self.post(payload, "pull_request_review")
        self.assertEqual(InboxItems.objects.count(), 0)

    def test_an_unlinked_team_gets_nothing(self):
        other, other_task = self._other_team()
        self.post(_review("changes_requested"), "pull_request_review")
        self.assertFalse(TaskComments.objects.filter(task=other_task).exists())
        self.assertFalse(InboxItems.objects.filter(team_id=other.team_id).exists())

    def test_an_installation_with_no_team_link_writes_nothing(self):
        self.link.delete()
        resp = self._post(_review("changes_requested"), event="pull_request_review")
        self.assertEqual(resp.data["detail"], "no_team")
        self.assertEqual(InboxItems.objects.count(), 0)

    def test_a_linked_second_team_with_a_same_named_task_is_told_only_in_its_own_team(self):
        other, other_task = self._other_team()
        GithubAppInstallationTeam.objects.create(installation=self.installation, team=other)
        self.post(_review("changes_requested"), "pull_request_review")
        # The author (user, member of `team` only) is told on `team`'s task, never on the other's.
        self.assertEqual(self.items().count(), 1)
        self.assertFalse(TaskComments.objects.filter(task=other_task).exists())


class TestApproved(SignalBase):
    def setUp(self):
        super().setUp()
        self.gh.checks = {"total_count": 1, "check_runs": [{"status": "in_progress"}]}

    def test_writes_an_approved_notice_when_ci_is_not_green(self):
        resp = self.post(_review("approved"), "pull_request_review")
        self.assertEqual(resp.data["notified"], 1)
        self.assertEqual(self.items(needs_you.KIND_APPROVED).count(), 1)
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)
        self.assertEqual(self.comments().count(), 1)
        self.assertIn("approved", self.comment_text())

    def test_redelivery_is_idempotent(self):
        self.post(_review("approved"), "pull_request_review")
        resp = self.post(_review("approved"), "pull_request_review")
        self.assertEqual(resp.data["notified"], 0)
        self.assertEqual(self.items().count(), 1)

    def test_an_approval_that_is_already_green_is_one_ready_notice_not_two(self):
        self.gh.checks = {
            "total_count": 1,
            "check_runs": [{"status": "completed", "conclusion": "success"}],
        }
        self.post(_review("approved"), "pull_request_review")
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 1)
        self.assertEqual(self.items(needs_you.KIND_APPROVED).count(), 0)
        self.assertEqual(self.comments().count(), 1)

    def test_an_approval_with_changes_still_requested_by_another_is_not_ready(self):
        self.gh.checks = {
            "total_count": 1,
            "check_runs": [{"status": "completed", "conclusion": "success"}],
        }
        self.gh.reviews_ = [
            {"state": "APPROVED", "user": {"id": REVIEWER_GH}},
            {"state": "CHANGES_REQUESTED", "user": {"id": 555}},
        ]
        self.post(_review("approved"), "pull_request_review")
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)
        self.assertEqual(self.items(needs_you.KIND_APPROVED).count(), 1)

    def test_a_github_failure_still_writes_the_approval_but_never_ready(self):
        self.gh.reviews = lambda *a: None
        self.post(_review("approved"), "pull_request_review")
        self.assertEqual(self.items(needs_you.KIND_APPROVED).count(), 1)
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)

    def test_a_draft_pr_is_approved_but_never_ready(self):
        self.gh.checks = {
            "total_count": 1,
            "check_runs": [{"status": "completed", "conclusion": "success"}],
        }
        self.post(_review("approved", draft=True), "pull_request_review")
        self.assertEqual(self.items(needs_you.KIND_APPROVED).count(), 1)
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)
        self.assertEqual(self.gh.calls, [])

    def test_an_unlinked_team_gets_nothing(self):
        self.link.delete()
        self._post(_review("approved"), event="pull_request_review")
        self.assertEqual(InboxItems.objects.count(), 0)


class TestCiFailing(SignalBase):
    def test_a_failing_check_run_tells_the_author_once(self):
        resp = self.post(_check_run("failure"), "check_run")
        self.assertEqual(resp.data["notified"], 1)
        item = self.items(needs_you.KIND_CI_FAILING).get()
        self.assertEqual(item.item_optionals["sha"], SHA)
        self.assertEqual(self.comments().count(), 1)
        self.assertIn("backend-test", self.comment_text())

    def test_many_check_runs_and_redeliveries_make_one_notice(self):
        for name in ("a", "b", "c", "d", "e"):
            self.post(_check_run("failure", name=name), "check_run")
        self.post(_check_run("timed_out", name="f"), "check_run")
        self.assertEqual(self.items().count(), 1)
        self.assertEqual(self.comments().count(), 1)

    def test_a_status_event_is_handled_like_a_check_run(self):
        self.post(_status("failure"), "status")
        self.assertEqual(self.items(needs_you.KIND_CI_FAILING).count(), 1)

    def test_another_commit_inside_the_window_is_debounced_and_after_it_is_a_new_notice(self):
        self.post(_check_run("failure"), "check_run")
        other = "fff9999aaa0000"
        self.gh.pulls[0]["sha"] = other
        self.post(_check_run("failure", sha=other), "check_run")
        self.assertEqual(self.items().count(), 1)
        TaskComments.objects.filter(task=self.task).update(
            ts_sent_at=timezone.now() - needs_you.CI_DEBOUNCE - timedelta(minutes=1)
        )
        self.post(_check_run("failure", sha=other, name="again"), "check_run")
        self.assertEqual(self.items().count(), 2)

    def test_nothing_for_a_run_still_going_a_cancelled_run_or_a_pending_status(self):
        self.post(_check_run("failure", status="in_progress"), "check_run")
        self.post(_check_run("cancelled"), "check_run")
        self.post(_status("pending"), "status")
        self.assertEqual(self.items().count(), 0)
        self.assertEqual(self.gh.calls, [])

    def test_no_open_linked_pr_makes_no_github_call(self):
        TaskDeliveryRows = self.task.deliveries  # noqa: N806
        TaskDeliveryRows.update(state=TaskDeliveryState.MERGED)
        self.post(_check_run("failure"), "check_run")
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.items().count(), 0)

    def test_a_stale_commit_is_not_told(self):
        self.post(_check_run("failure", sha="0000000aaaaaaa"), "check_run")
        self.assertEqual(self.items().count(), 0)

    def test_a_draft_pr_is_not_told(self):
        self.gh.pulls[0]["draft"] = True
        self.post(_check_run("failure"), "check_run")
        self.assertEqual(self.items().count(), 0)

    def test_an_unlinked_team_gets_nothing(self):
        other, other_task = self._other_team()
        self.post(_check_run("failure"), "check_run")
        self.assertFalse(TaskComments.objects.filter(task=other_task).exists())
        self.link.delete()
        resp = self._post(_check_run("failure", sha="eeeeeee1111111"), event="check_run")
        self.assertEqual(resp.data["detail"], "no_team")
        self.assertEqual(self.items().count(), 1)  # only the first, from before the unlink

    def test_a_github_failure_writes_nothing(self):
        self.gh.pulls_for_commit = lambda *a: None
        self.post(_check_run("failure"), "check_run")
        self.assertEqual(self.items().count(), 0)


class TestReadyToMerge(SignalBase):
    def approve_with_pending_ci(self):
        self.gh.checks = {"total_count": 1, "check_runs": [{"status": "in_progress"}]}
        self.post(_review("approved"), "pull_request_review")
        self.assertEqual(self.items(needs_you.KIND_APPROVED).count(), 1)

    def green(self):
        self.gh.checks = {
            "total_count": 2,
            "check_runs": [
                {"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "skipped"},
            ],
        }

    def test_green_after_approval_writes_ready_once(self):
        self.approve_with_pending_ci()
        self.green()
        resp = self.post(_check_run("success"), "check_run")
        self.assertEqual(resp.data["notified"], 1)
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 1)
        self.assertIn("ready to merge", self.comment_text())
        # More green check runs (and a redelivery) make no more.
        self.post(_check_run("success", name="b"), "check_run")
        self.post(_status("success"), "status")
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 1)
        self.assertEqual(self.comments().filter().count(), 2)  # approved + ready

    def test_a_pending_check_is_not_ready(self):
        self.approve_with_pending_ci()
        self.post(_check_run("success"), "check_run")  # the other check is still running
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)

    def test_a_failed_other_check_is_not_ready(self):
        self.approve_with_pending_ci()
        self.gh.checks = {
            "total_count": 2,
            "check_runs": [
                {"status": "completed", "conclusion": "success"},
                {"status": "completed", "conclusion": "failure"},
            ],
        }
        self.post(_check_run("success"), "check_run")
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)

    def test_a_failing_combined_status_is_not_ready(self):
        self.approve_with_pending_ci()
        self.green()
        self.gh.combined = {"total_count": 1, "state": "failure"}
        self.post(_status("success", context="other"), "status")
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)

    def test_no_approval_means_no_ready_and_no_github_call(self):
        self.green()
        self.post(_check_run("success"), "check_run")
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)
        self.assertEqual(self.gh.calls, [])

    def test_changes_requested_after_approval_blocks_ready(self):
        self.approve_with_pending_ci()
        self.green()
        self.gh.reviews_ = [{"state": "CHANGES_REQUESTED", "user": {"id": REVIEWER_GH}}]
        self.post(_check_run("success"), "check_run")
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)

    def test_a_dismissed_approval_is_not_an_approval(self):
        self.approve_with_pending_ci()
        self.green()
        self.gh.reviews_ = [
            {"state": "APPROVED", "user": {"id": REVIEWER_GH}},
            {"state": "DISMISSED", "user": {"id": REVIEWER_GH}},
        ]
        self.post(_check_run("success"), "check_run")
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)

    def test_ready_retires_the_failing_and_changes_requested_items(self):
        self.post(_review("changes_requested", review_id=1), "pull_request_review")
        self.post(_check_run("failure"), "check_run")
        self.assertEqual(self.items().filter(is_read=False).count(), 2)
        self.approve_with_pending_ci()
        self.green()
        self.post(_check_run("success"), "check_run")
        unread = set(
            self.items().filter(is_read=False).values_list("item_optionals__kind", flat=True)
        )
        self.assertEqual(unread, {needs_you.KIND_APPROVED, needs_you.KIND_READY})

    def test_a_green_commit_retires_a_failing_item_even_without_an_approval(self):
        self.post(_check_run("failure"), "check_run")
        self.green()
        self.gh.reviews_ = []
        self.post(_check_run("success"), "check_run")
        self.assertEqual(self.items(needs_you.KIND_CI_FAILING).filter(is_read=False).count(), 0)
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)

    def test_an_unlinked_team_gets_nothing(self):
        self.approve_with_pending_ci()
        self.green()
        self.link.delete()
        resp = self._post(_check_run("success"), event="check_run")
        self.assertEqual(resp.data["detail"], "no_team")
        self.assertEqual(self.items(needs_you.KIND_READY).count(), 0)


class TestClosedPrAndBriefing(SignalBase):
    def briefing_rows(self):
        from origin.search_engine.agent.tools import ToolContext  # noqa: PLC0415

        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        now = timezone.now()
        return waiting_on_me(ctx, now, mock.MagicMock()).get("pr_updates")

    def test_the_briefing_lists_unread_updates_and_drops_read_ones(self):
        self.assertIsNone(self.briefing_rows())  # the key is absent when there is nothing
        self.post(_review("changes_requested"), "pull_request_review")
        rows = self.briefing_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["signal"], "changes_requested")
        self.assertEqual(rows[0]["task_id"], self.task.task_id)
        self.assertEqual(rows[0]["pr_url"], PR_URL)
        self.items().update(is_read=True)
        self.assertIsNone(self.briefing_rows())

    def test_the_briefing_does_not_show_another_members_updates(self):
        self.post(_review("changes_requested"), "pull_request_review")
        ctx_rows = needs_you.pending_signal_rows(self.user2.id, self.team.team_id)
        self.assertEqual(ctx_rows, [])

    def test_closing_the_pr_retires_its_unread_signals(self):
        self.post(_review("changes_requested"), "pull_request_review")
        self.post(_check_run("failure"), "check_run")
        self.assertEqual(self.items().filter(is_read=False).count(), 2)
        self.post(self._pr("closed", state="closed"), "pull_request")
        self.assertEqual(self.items().filter(is_read=False).count(), 0)


class TestReaderRules(SignalBase):
    def test_review_decision_reads_each_reviewers_last_verdict(self):
        decide = reader_mod.review_decision
        self.assertIsNone(decide([]))
        approved = {"state": "APPROVED", "user": {"id": 1}}
        self.assertEqual(decide([approved]), "approved")
        self.assertEqual(decide([approved, {"state": "COMMENTED", "user": {"id": 1}}]), "approved")
        changes = {"state": "CHANGES_REQUESTED", "user": {"id": 2}}
        self.assertEqual(decide([approved, changes]), "changes_requested")
        self.assertEqual(decide([changes, {"state": "APPROVED", "user": {"id": 2}}]), "approved")
        self.assertIsNone(decide([approved, {"state": "DISMISSED", "user": {"id": 1}}]))

    def test_ci_is_green_needs_everything_settled_and_known(self):
        green = reader_mod.ci_is_green
        done = {"status": "completed", "conclusion": "success"}
        self.assertTrue(green({"total_count": 0}, {"total_count": 1, "check_runs": [done]}))
        self.assertFalse(
            green({"total_count": 0}, {"total_count": 0, "check_runs": []})
        )  # nothing ran
        self.assertFalse(
            green({"total_count": 0}, {"total_count": 2, "check_runs": [done]})
        )  # cut short
        self.assertFalse(green(None, {"total_count": 1, "check_runs": [done]}))
        self.assertFalse(
            green({"total_count": 1, "state": "pending"}, {"total_count": 1, "check_runs": [done]})
        )
        self.assertTrue(
            green({"total_count": 1, "state": "success"}, {"total_count": 0, "check_runs": []})
        )

    def test_the_reader_asks_github_with_a_narrow_read_only_token(self):
        response = mock.MagicMock(ok=True, status_code=200)
        response.json.return_value = []
        with (
            mock.patch.object(
                reader_mod.github_app, "get_installation_token", return_value="tok"
            ) as token,
            mock.patch.object(reader_mod.requests, "get", return_value=response) as get,
        ):
            reader_mod.InstallationReader(7770001).reviews("owner", "repo", 42)
        token.assert_called_once()
        self.assertEqual(token.call_args.kwargs["repositories"], ["repo"])
        self.assertEqual(
            token.call_args.kwargs["permissions"],
            {"pull_requests": "read", "checks": "read", "statuses": "read"},
        )
        self.assertTrue(get.call_args.args[0].endswith("/repos/owner/repo/pulls/42/reviews"))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_a_bad_repo_name_makes_no_request(self):
        with mock.patch.object(reader_mod.requests, "get") as get:
            self.assertIsNone(reader_mod.InstallationReader(1).reviews("ow/ner", "repo", 1))
        get.assert_not_called()
