"""What people are told when a project rule moves a task (MIP-2500): one line in the old chat
that names neither the destination nor the task, and a notice to the assignee."""

import json
from unittest import mock

from django.urls import reverse

from origin.models.chat.unified_models import Activity, Channel, ChannelKind, Message
from origin.services import automation_notice, realtime_push, webpush_dispatch
from origin.services.task_card_post import post_task_created_card
from origin.tests.test_automation_rules import AutomationTestBase


class RuleMoveNoticeTests(AutomationTestBase):
    def setUp(self):
        super().setUp()
        self.a.project_name = "Alpha"
        self.x.project_name = "Zanzibar Secret"
        self.x.is_private = True
        for project in (self.a, self.x):
            project.save()

    def chat(self, project):
        channel, _ = Channel.objects.get_or_create(
            kind=ChannelKind.PM,
            legacy_chat_id=project.project_id,
            defaults={"team": self.team, "project": project},
        )
        return channel

    def notices(self, channel):
        return list(
            Message.objects.filter(channel=channel, metadata__systemNotice="task_moved_by_rule")
        )

    def move(self, **extra):
        self.chat(self.a)
        self.chat(self.x)
        task = self.task(title="Plan the layoffs", assignee=self.user2, **extra)
        post_task_created_card(task, actor_id=self.user.id)
        with self.captureOnCommitCallbacks(execute=True):
            self.start(task)
        self.assertEqual(self.fresh(task).project_id, self.x.project_id)
        return task

    def test_the_old_chat_gets_one_line_that_names_only_the_rule(self):
        rule = self.rule(name="Handover")
        task = self.move()

        (line,) = self.notices(self.chat(self.a))
        self.assertIsNone(line.sender_id)
        self.assertIsNone(line.task_id)
        self.assertFalse(line.is_thread_reply)
        self.assertEqual(
            line.metadata,
            {
                "systemNotice": "task_moved_by_rule",
                "movedTaskId": task.task_id,
                "destinationProjectId": self.x.project_id,
                "ruleName": rule.name,
            },
        )
        self.assertEqual(line.body_text, 'Rule "Handover" moved a task to another project.')
        stored = json.dumps([line.body, line.body_text, line.metadata])
        for secret in ("Zanzibar", "layoffs", task.display_id, "XX-", "AA-"):
            self.assertNotIn(secret, stored)
        self.assertFalse(line.mentions.exists())
        self.assertFalse(Activity.objects.filter(message=line).exists())
        # The card itself left; the new chat has it, and no second one.
        self.assertFalse(Message.objects.filter(channel=self.chat(self.a), task=task).exists())
        self.assertEqual(Message.objects.filter(channel=self.chat(self.x), task=task).count(), 1)
        self.assertEqual(self.notices(self.chat(self.x)), [])

    def test_the_line_is_announced_to_the_open_chat_after_the_card_left(self):
        self.rule()
        sent = []
        with (
            mock.patch.object(
                realtime_push,
                "push_messages_deleted",
                lambda *a, **k: sent.append("removed"),
            ),
            mock.patch.object(
                realtime_push,
                "push_message",
                lambda message, event: sent.append(message.metadata.get("systemNotice")),
            ),
        ):
            self.move()
        self.assertLess(sent.index("removed"), sent.index("task_moved_by_rule"))

    def test_the_assignee_is_told_that_a_rule_moved_it(self):
        self.rule()
        pushed = []
        with mock.patch.object(
            webpush_dispatch,
            "schedule_push_for_activities",
            lambda rows: pushed.extend(webpush_dispatch._full_push_spec(r) for r in rows),
        ):
            task = self.move()
        row = Activity.objects.get(recipient=self.user2, meta__ruleMove=True)
        self.assertEqual((row.message.task_id, row.actor_id), (task.task_id, self.user.id))
        self.assertEqual(row.channel_id, self.chat(self.x).pk)
        self.assertIn("A rule moved your task", [spec["title"] for spec in pushed])

    def test_a_rule_that_also_reassigns_tells_the_new_assignee_once(self):
        self.rule(set_assignee=self.user2)
        self.chat(self.a)
        self.chat(self.x)
        task = self.task()
        post_task_created_card(task, actor_id=self.user.id)
        # The owner is the actor, so the new assignee is somebody else.
        with self.captureOnCommitCallbacks(execute=True):
            self.start(task)
        rows = Activity.objects.filter(recipient=self.user2, meta__taskAssign=True)
        self.assertEqual(rows.count(), 1)
        self.assertNotIn("ruleMove", rows.get().meta)

    def test_the_owner_is_not_told_about_their_own_rule(self):
        self.rule()
        self.chat(self.a)
        self.chat(self.x)
        task = self.task()
        post_task_created_card(task, actor_id=self.user.id)
        self.start(task)
        self.assertFalse(Activity.objects.filter(meta__ruleMove=True).exists())

    def test_a_rule_that_does_not_move_posts_no_line(self):
        self.rule(destination_project=None, set_status="Pending")
        self.chat(self.a)
        task = self.task()
        self.start(task)
        self.assertEqual(self.notices(self.chat(self.a)), [])

    def test_a_move_by_hand_posts_no_line(self):
        self.chat(self.a)
        self.chat(self.x)
        task = self.task()
        response = self.client.put(
            reverse("task_create"),
            {"task_id": task.task_id, "project": self.x.project_id},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self.notices(self.chat(self.a)), [])

    def test_a_notice_that_fails_does_not_cost_the_move(self):
        self.rule()
        with mock.patch.object(automation_notice, "_pm_channel", side_effect=RuntimeError("boom")):
            self.move()
        self.assertEqual(self.notices(self.chat(self.a)), [])

    def test_a_member_cannot_forge_the_line(self):
        channel = self.chat(self.a)
        response = self.client.post(
            reverse("v3_messages_delta", args=[channel.pk]),
            {
                "body": [{"type": "paragraph", "content": []}],
                "body_text": "x",
                "metadata": {"systemNotice": "task_moved_by_rule", "movedTaskId": 1},
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNotNone(Message.objects.get(channel=channel).sender_id)
