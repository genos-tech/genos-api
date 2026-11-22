"""MIP-2055: "Closes MIP-n" / "Part of MIP-n" in a PR title or description links the task.

Parser cases first (pure), then the webhook: links, closes on merge like a branch id, never closes
for "Part of", stays inside the registrar's teams, and the merge comment names the rule.
"""

from __future__ import annotations

import json

from django.test import SimpleTestCase, override_settings

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import GithubWebhookRegistration
from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.models.task.task_models import TaskComments
from origin.services.pr_references import CLOSES, PART_OF, parse_pr_references
from origin.tests.test_github_webhook import WEBHOOK_SECRET, _sign
from origin.tests.test_pr_merge_comment import _text
from origin.tests.test_task_delivery import DeliveryBase, _pr_event


class ParsePrReferencesTests(SimpleTestCase):
    def test_closes_and_part_of(self):
        self.assertEqual(parse_pr_references("", "Closes HK-1"), {"HK-1": CLOSES})
        self.assertEqual(parse_pr_references("", "Part of HK-2"), {"HK-2": PART_OF})

    def test_every_github_closing_word_and_case(self):
        for word in ("close", "Closes", "CLOSED", "fix", "Fixes", "fixed", "resolve", "Resolves"):
            self.assertEqual(parse_pr_references("", f"{word} hk-7"), {"HK-7": CLOSES}, word)
        self.assertEqual(parse_pr_references("", "PART OF hk-7"), {"HK-7": PART_OF})
        self.assertEqual(parse_pr_references("", "part   of: HK-7"), {"HK-7": PART_OF})

    def test_title_is_read_too(self):
        self.assertEqual(parse_pr_references("Speed up search (Closes HK-3", ""), {"HK-3": CLOSES})

    def test_several_ids_share_the_keyword(self):
        got = parse_pr_references("", "Closes HK-1, HK-2 and HK-3 & HK-4.\nPart of HK-5")
        self.assertEqual(
            got, {"HK-1": CLOSES, "HK-2": CLOSES, "HK-3": CLOSES, "HK-4": CLOSES, "HK-5": PART_OF}
        )

    def test_part_of_wins_over_closes_for_the_same_id(self):
        self.assertEqual(parse_pr_references("Closes HK-1", "Part of HK-1"), {"HK-1": PART_OF})
        self.assertEqual(parse_pr_references("Part of HK-1", "Closes HK-1"), {"HK-1": PART_OF})

    def test_not_a_reference(self):
        for text in (
            "HK-1 is related",  # no keyword
            "Closes #12",  # a GitHub issue, not a task
            "Disclosed HK-1",  # keyword inside a word
            "Closes the HK-1 gap",  # a word between keyword and id
            "Closes HK-1x",  # not an id boundary
            "Closes HK-",  # no number
            "",
        ):
            self.assertEqual(parse_pr_references("", text), {}, text)
        self.assertEqual(parse_pr_references(None, None), {})

    def test_code_and_html_comments_are_ignored(self):
        body = (
            "<!-- Closes HK-1 -->\n`Closes HK-2`\n```\nCloses HK-3\n```\n"
            "~~~\nPart of HK-4\n~~~\nCloses HK-5"
        )
        self.assertEqual(parse_pr_references("", body), {"HK-5": CLOSES})

    def test_bounded(self):
        body = "Closes " + ", ".join(f"HK-{n}" for n in range(1, 60))
        self.assertEqual(len(parse_pr_references("", body)), 20)
        self.assertEqual(parse_pr_references("", "x" * 70_000 + " Closes HK-1"), {})


@override_settings(GITHUB_WEBHOOK_SECRET=WEBHOOK_SECRET)
class PrReferenceWebhookTests(DeliveryBase):
    def setUp(self):
        super().setUp()
        GithubWebhookRegistration.objects.create(
            owner="owner", repo="repo", hook_id=1, registered_by=self.user
        )
        self.user.auto_close_on_pr_merge = True
        self.user.save(update_fields=["auto_close_on_pr_merge"])

    def _post(self, payload):
        body = json.dumps(payload).encode()
        res = self.client.post(
            "/api/v2/github/webhook/",
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_sign(body),
            HTTP_X_GITHUB_EVENT="pull_request",
        )
        self.assertEqual(res.status_code, 200, res.content)
        return res

    def _event(self, action, *, body="", title="Some work", ref="feature/no-id", **kw):
        payload = _pr_event(action, title=title, ref=ref, **kw)
        payload["pull_request"]["body"] = body
        return payload

    def _merge(self, **kw):
        return self._post(self._event("closed", state="closed", merged=True, **kw))

    def _status(self, task):
        task.refresh_from_db()
        return task.status

    def _comment_text(self, task) -> str:
        comments = list(TaskComments.objects.filter(task=task, sender__isnull=True))
        self.assertEqual(len(comments), 1)
        return _text(comments[0].comment_body)

    def test_opening_with_closes_links_the_task_by_a_delivery_row(self):
        self._post(self._event("opened", body="Closes HK-42"))

        row = TaskDelivery.objects.get(task=self.task)
        self.assertEqual(row.pr_url, "https://github.com/owner/repo/pull/42")
        self.assertEqual(row.state, TaskDeliveryState.OPEN)

    def test_closes_in_the_description_closes_the_task_on_merge(self):
        self._merge(body="Closes HK-42")

        self.assertEqual(self._status(self.task), "Closed")
        text = self._comment_text(self.task)
        self.assertIn('its description says "Closes" this task', text)

    def test_closes_in_the_title_closes_the_task_on_merge(self):
        self._merge(title="Speed up search, closes HK-42")

        self.assertEqual(self._status(self.task), "Closed")

    def test_lowercase_and_several_ids(self):
        other = self._task(43)
        self._merge(body="fixes hk-42, HK-43")

        self.assertEqual(self._status(self.task), "Closed")
        self.assertEqual(self._status(other), "Closed")

    def test_part_of_links_but_does_not_close(self):
        self._merge(body="Part of HK-42")

        self.assertEqual(self._status(self.task), "Open")
        self.assertEqual(TaskDelivery.objects.get(task=self.task).state, TaskDeliveryState.MERGED)
        text = self._comment_text(self.task)
        self.assertIn('"Part of"', text)
        self.assertIn("does not close it", text)

    def test_part_of_overrides_a_branch_id_and_a_manual_link(self):
        self._link(self.task, number=42, repo="repo")  # same PR via a manual row is not our URL
        self._merge(ref="feature/HK-42-x", body="Part of HK-42")

        self.assertEqual(self._status(self.task), "Open")

    def test_part_of_pr_still_open_holds_the_close_of_another_pr(self):
        self._post(self._event("opened", number=50, body="Part of HK-42"))

        self._merge(number=51, body="Closes HK-42")

        self.assertEqual(self._status(self.task), "Open")  # waits for PR 50 like any open PR

    def test_close_keeps_the_assignee_opt_in(self):
        self.user.auto_close_on_pr_merge = False
        self.user.save(update_fields=["auto_close_on_pr_merge"])

        self._merge(body="Closes HK-42")

        self.assertEqual(self._status(self.task), "Open")

    def test_an_id_the_repos_team_cannot_see_is_ignored(self):
        other_team = TeamMaster.objects.create(team_name="Other")
        TeamMembers.objects.create(team=other_team, attendee=self.user2)
        other_project = self._project(other_team, "Platform", "ZZ", owner=self.user2)
        hidden = self._task(5, project=other_project, team=other_team, assignee=self.user2)
        self.user2.auto_close_on_pr_merge = True
        self.user2.save(update_fields=["auto_close_on_pr_merge"])

        self._merge(body="Closes ZZ-5, Part of ZZ-5 and Closes HK-42")

        self.assertEqual(self._status(hidden), "Open")
        self.assertFalse(TaskDelivery.objects.filter(task=hidden).exists())
        self.assertFalse(TaskComments.objects.filter(task=hidden).exists())

    def test_same_display_id_in_another_team_is_not_touched(self):
        other_team = TeamMaster.objects.create(team_name="Other")
        TeamMembers.objects.create(team=other_team, attendee=self.user2)
        other_project = self._project(other_team, "Platform", "HK", owner=self.user2)
        hidden = self._task(42, project=other_project, team=other_team, assignee=self.user2)

        self._merge(body="Closes HK-42")

        self.assertEqual(self._status(hidden), "Open")
        self.assertEqual(self._status(self.task), "Closed")

    def test_unknown_task_number_is_ignored(self):
        res = self._merge(body="Closes HK-9999 and Closes HK-99999999999")

        self.assertEqual(res.json()["updated"], 0)

    def test_editing_the_description_adds_the_link(self):
        self._post(self._event("opened", body="nothing yet"))
        self.assertFalse(TaskDelivery.objects.filter(task=self.task).exists())

        self._post(self._event("edited", body="Closes HK-42"))

        self.assertTrue(TaskDelivery.objects.filter(task=self.task).exists())

    def test_branch_id_still_closes_and_the_comment_names_the_branch(self):
        self._merge(ref="feature/HK-42-x")

        self.assertEqual(self._status(self.task), "Closed")
        self.assertIn("its branch name carries the task id", self._comment_text(self.task))

    def test_branch_and_description_both_named(self):
        self._merge(ref="feature/HK-42-x", body="Closes HK-42")

        text = self._comment_text(self.task)
        self.assertIn("its branch name carries the task id", text)
        self.assertIn('its description says "Closes" this task', text)
        self.assertEqual(TaskComments.objects.filter(task=self.task).count(), 1)
