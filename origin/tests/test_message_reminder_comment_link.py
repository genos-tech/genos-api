"""A reminder about a task comment links to the comment, not just its thread (MIP-1437).

A task comment is mirrored as a PM thread-reply `Message` carrying `metadata.taskCommentId`. The
PM thread shows comments on its Comments tab, which has its own deep link
(`/thread/<task id>/comment/<comment id>`) that scrolls to and highlights the comment. The generic
`/thread/<root>/message/<seq>` link opens the right task but cannot say which comment, which is
most of what a reminder is for.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.services import message_reminders
from origin.tests.test_base import BaseAPITestCase


class ReminderCommentLinkTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="LinkProj",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        self.header = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )

    def _reply(self, seq, metadata, channel=None, task_id="task"):
        return Message.objects.create(
            channel=channel or self.channel,
            sender=self.user,
            seq=seq,
            body={"text": "c"},
            body_text="c",
            is_thread_reply=True,
            parent=self.header,
            thread_root=self.header,
            task_id=self.task.task_id if task_id == "task" else task_id,
            metadata=metadata,
        )

    def test_a_comment_mirror_links_to_the_comment(self):
        message = self._reply(2, {"taskCommentId": 5})
        self.assertEqual(
            message_reminders.chat_href(message),
            f"/workspace/chat/pm/{self.channel.id}/thread/{self.task.task_id}/comment/5",
        )

    def test_the_inbox_card_carries_that_link(self):
        message = self._reply(2, {"taskCommentId": 5})
        reminder = type(
            "R",
            (),
            {"id": "r1", "remind_at": message.ts_sent_at, "ts_created_at": message.ts_sent_at},
        )()
        _, optionals = message_reminders._card(message, reminder)
        self.assertTrue(optionals["href"].endswith(f"/thread/{self.task.task_id}/comment/5"))

    def test_a_pm_reply_that_is_not_a_comment_keeps_the_thread_link(self):
        message = self._reply(2, {})
        self.assertEqual(
            message_reminders.chat_href(message),
            f"/workspace/chat/pm/{self.channel.id}/thread/{self.header.id}/message/2",
        )

    def test_a_comment_id_that_is_not_a_number_is_not_trusted(self):
        for seq, bad in enumerate(("5", True, None, [5]), start=10):
            with self.subTest(bad=bad):
                message = self._reply(seq, {"taskCommentId": bad})
                self.assertIn(f"/message/{seq}", message_reminders.chat_href(message))
                self.assertNotIn("/comment/", message_reminders.chat_href(message))

    def test_a_reply_with_no_task_keeps_the_thread_link(self):
        message = self._reply(4, {"taskCommentId": 5}, task_id=None)
        self.assertNotIn("/comment/", message_reminders.chat_href(message))

    def test_other_channels_are_untouched(self):
        gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="G", owner=self.user)
        message = Message.objects.create(
            channel=gm,
            sender=self.user,
            seq=1,
            body={"text": "x"},
            body_text="x",
            metadata={"taskCommentId": 5},
            task_id=self.task.task_id,
        )
        self.assertEqual(
            message_reminders.chat_href(message), f"/workspace/chat/gm/{gm.id}/message/1"
        )
