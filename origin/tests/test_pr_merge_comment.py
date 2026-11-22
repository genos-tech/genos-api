"""MIP-2016: when a linked PR merges, Genos leaves ONE comment on the task saying what is still open.

The decision (Ken, 2026-11-22, MIP-2014): suggestions and records, never new restrictions. So the
webhook still does exactly what it did (links, status close), and then adds a visible, automatic,
quiet comment. Pinned here: what the comment says in each of the four situations, that it is written
once per PR and task however often the event arrives, that it is not authored by a person and tells
nobody, that it stays inside the registrar's teams, and that nothing in it can be forged from the PR.
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import override_settings

from origin.models.chat.unified_models import Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import GithubWebhookRegistration
from origin.models.task.task_delivery_models import TaskDeliverySource, TaskDeliveryState
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import pr_merge_comment
from origin.services.task_delivery import record_delivery
from origin.tests.test_github_webhook import WEBHOOK_SECRET, _sign
from origin.tests.test_task_delivery import DeliveryBase, _pr_event

PR42 = "https://github.com/owner/repo/pull/42"


def _text(node) -> str:
    """The plain text of a BlockNote body: text, link labels and task chips, in order."""
    if isinstance(node, list):
        return "\n".join(_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hashTask":
        return "<chip " + node["props"]["displayId"] + ">"
    parts = [_text(node.get("content")), _text(node.get("children"))]
    if kind == "link":
        parts.append(f"<{node.get('href')}>")
    return "\n".join(p for p in parts if p)


def _hrefs(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, list):
        for n in node:
            found += _hrefs(n)
    elif isinstance(node, dict):
        if node.get("type") == "link":
            found.append(node.get("href"))
        found += _hrefs(node.get("content")) + _hrefs(node.get("children"))
    return found


@override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
class MergeCommentTests(DeliveryBase):
    def setUp(self):
        super().setUp()
        GithubWebhookRegistration.objects.create(
            owner="owner", repo="repo", hook_id=1, registered_by=self.user
        )

    def _post(self, payload):
        body = json.dumps(payload).encode()
        return self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body),
            HTTP_X_GITHUB_EVENT="pull_request",
        )

    def _merge(self, **kw):
        kw.setdefault("number", 42)
        kw.setdefault("ref", "feature/HK-42-fix")
        res = self._post(_pr_event("closed", state="closed", merged=True, **kw))
        self.assertEqual(res.status_code, 200, res.content)
        return res

    def _genos_comments(self, task=None):
        return list(
            TaskComments.objects.filter(task=task or self.task, sender__isnull=True).order_by(
                "comment_id"
            )
        )

    def _only_comment_text(self, task=None) -> str:
        comments = self._genos_comments(task)
        self.assertEqual(len(comments), 1, [_text(c.comment_body) for c in comments])
        return _text(comments[0].comment_body)

    # ── the four situations ──

    def test_task_left_open_with_no_other_open_pr_says_to_close_it_or_say_what_is_left(self):
        self._merge()

        text = self._only_comment_text()
        self.assertIn("Genos", text)
        self.assertIn("owner/repo #42", text)
        self.assertIn(f"<{PR42}>", text)
        self.assertIn("All linked PRs merged", text)
        self.assertIn("close it or say what is left", text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")  # no status change by this

    def test_task_closed_with_an_open_subtask_and_another_open_pr_lists_both(self):
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])
        child = self._task(43)
        TaskMaster.objects.filter(pk=child.pk).update(parent_task_id=self.task.task_id)
        # A second PR still open holds the auto-close, so close the task by hand first: the point
        # here is what the comment says about a Closed task that is not finished.
        self._link(self.task, number=9, state=TaskDeliveryState.OPEN, repo="other")
        TaskMaster.objects.filter(pk=self.task.pk).update(status="Closed")

        self._merge()

        text = self._only_comment_text()
        self.assertIn("The task is Closed, but it may not be finished", text)
        self.assertIn("Open sub-tasks (1)", text)
        self.assertIn("<chip HK-43>", text)
        self.assertIn("Other linked PRs still open (1)", text)
        self.assertIn("<https://github.com/genos-tech/other/pull/9>", text)
        self.assertIn("Reopen it", text)

    def test_the_close_this_merge_makes_is_reported_with_what_it_left_open(self):
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])
        child = self._task(43)
        TaskMaster.objects.filter(pk=child.pk).update(parent_task_id=self.task.task_id)

        self._merge()

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Closed")  # today's behaviour, unchanged
        text = self._only_comment_text()
        self.assertIn("The task is Closed, but it may not be finished", text)
        self.assertIn("<chip HK-43>", text)

    def test_task_closed_with_nothing_open_says_so_in_one_line(self):
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])

        self._merge()

        text = self._only_comment_text()
        self.assertIn("The task is Closed. No other linked PR or sub-task is open.", text)

    def test_task_left_open_because_another_pr_is_open_lists_that_pr(self):
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])
        self._link(self.task, number=9, state=TaskDeliveryState.OPEN, repo="other")

        self._merge()

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")  # held by the other PR, as before
        text = self._only_comment_text()
        self.assertIn("waits for the other linked PRs", text)
        self.assertIn("<https://github.com/genos-tech/other/pull/9>", text)
        self.assertNotIn("All linked PRs merged", text)

    def test_a_closed_unmerged_or_opened_pr_posts_nothing(self):
        self._post(_pr_event("opened"))
        self._post(_pr_event("closed", state="closed", merged=False))

        self.assertEqual(self._genos_comments(), [])

    def test_a_merged_pr_linked_to_no_task_posts_nothing(self):
        self._merge(ref="feature/no-task-here")

        self.assertEqual(TaskComments.objects.count(), 0)

    # ── once per PR and task ──

    def test_a_redelivery_does_not_post_again(self):
        self._merge()
        self._merge()
        self._merge()

        self.assertEqual(len(self._genos_comments()), 1)

    def test_a_deleted_genos_comment_is_not_posted_again(self):
        self._merge()
        TaskComments.objects.filter(task=self.task).update(is_deleted=True)

        self._merge()

        self.assertEqual(len(self._genos_comments()), 1)

    def test_pr_98_and_pr_982_each_get_their_own_comment(self):
        self._merge(number=98)
        self._merge(number=982)
        self._merge(number=98)

        self.assertEqual(len(self._genos_comments()), 2)

    def test_the_last_pr_still_posts_after_an_earlier_comment_linked_it_as_open(self):
        # PR 9 is open when PR 42 merges, so the first comment LINKS PR 9. When PR 9 merges later,
        # that earlier link must not count as "already posted": the second comment is the one that
        # says every linked PR is merged.
        record_delivery(
            task=self.task,
            owner="owner",
            repo="repo",
            pr_number=9,
            state=TaskDeliveryState.OPEN,
            source=TaskDeliverySource.MANUAL,
        )
        self._merge(number=42)
        self.assertEqual(len(self._genos_comments()), 1)
        self.assertIn(
            "waits for the other linked PRs", _text(self._genos_comments()[0].comment_body)
        )

        self._merge(number=9, ref="feature/HK-42-more")

        comments = self._genos_comments()
        self.assertEqual(len(comments), 2)
        self.assertIn("All linked PRs merged", _text(comments[1].comment_body))

    def test_two_prs_of_one_task_each_post_once(self):
        self._merge(number=42)
        self._merge(number=43, repo="repo")

        texts = [_text(c.comment_body) for c in self._genos_comments()]
        self.assertEqual(len(texts), 2)
        self.assertIn("owner/repo #42", texts[0])
        self.assertIn("owner/repo #43", texts[1])

    # ── who wrote it, and who is told ──

    def test_it_is_not_authored_by_a_person_and_tells_nobody(self):
        with (
            mock.patch("origin.services.task_comment_fanout.fan_out_task_comment") as fan_out,
            mock.patch("origin.services.webpush_dispatch.schedule_push_for_activities") as push,
        ):
            self._merge()

        (comment,) = self._genos_comments()
        self.assertIsNone(comment.sender_id)
        self.assertTrue(_text(comment.comment_body).startswith("Genos"))
        fan_out.assert_not_called()
        push.assert_not_called()

    def test_it_shows_in_the_project_task_thread_without_a_sender(self):
        self._merge()

        (comment,) = self._genos_comments()
        mirror = Message.objects.get(
            task_id=self.task.task_id, metadata__taskCommentId=comment.comment_id
        )
        self.assertIsNone(mirror.sender_id)
        self.assertTrue(mirror.is_thread_reply)

    # ── tenant boundary and forgery ──

    def test_a_task_of_another_team_with_the_same_display_id_gets_nothing(self):
        other_team = TeamMaster.objects.create(team_name="Other")
        TeamMembers.objects.create(team=other_team, attendee=self.user2)
        other_project = self._project(other_team, "Platform", "HK", owner=self.user2)
        other_task = self._task(42, project=other_project, team=other_team, assignee=self.user2)

        self._merge()

        self.assertEqual(self._genos_comments(other_task), [])
        self.assertEqual(len(self._genos_comments()), 1)

    def test_the_pr_title_cannot_forge_a_link_a_chip_or_a_mention(self):
        evil = "[click](https://evil.example) [#HK-1](task:1) **x** `y` @[a](user:b)\nline two"

        self._merge(title=evil)

        (comment,) = self._genos_comments()
        hrefs = _hrefs(comment.comment_body)
        self.assertNotIn("https://evil.example", hrefs)
        self.assertEqual(hrefs, [PR42])
        self.assertNotIn("<chip", _text(comment.comment_body))

    def test_a_task_chip_is_built_only_for_the_tasks_the_comment_read(self):
        # The resolver is closed over the sub-tasks it was given and knows no other id.
        resolve = pr_merge_comment._chip_resolver([self.task])

        self.assertIsNotNone(resolve(f"task:{self.task.task_id}"))
        self.assertIsNone(resolve("task:999999"))
        self.assertIsNone(resolve("user:00000000-0000-0000-0000-000000000000"))

    # ── never breaks the webhook ──

    def test_a_failure_while_commenting_still_answers_200_and_still_closes_the_task(self):
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])

        with mock.patch.object(
            pr_merge_comment, "post_merge_comment", side_effect=RuntimeError("boom")
        ):
            self._merge()

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Closed")
        self.assertEqual(self._genos_comments(), [])

    def test_many_open_subtasks_are_capped_and_counted(self):
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])
        for n in range(43, 43 + 8):
            child = self._task(n)
            TaskMaster.objects.filter(pk=child.pk).update(parent_task_id=self.task.task_id)

        self._merge()

        text = self._only_comment_text()
        self.assertIn("Open sub-tasks (8)", text)
        self.assertEqual(text.count("<chip "), pr_merge_comment.MAX_LISTED)
        self.assertIn("and 3 more", text)
