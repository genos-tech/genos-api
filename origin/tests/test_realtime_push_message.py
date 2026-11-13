"""A chat message this API wrote is announced to the chat that has it open (MIP-1459).

The task card posted for a task made over MCP (MIP-1450) never went through genos-sockets'
`message.send` handler, so nobody broadcast `message.created`: a colleague with the project chat
already open saw it at their next sync. After the write commits the API now posts the message to
genos-sockets (`/internal/broadcast/message`), which sends it to the channel's room.

These pin what is sent, when (after commit only), that it is signed, that a failure never costs
the write, and that the card poster uses it.
"""

import json
from unittest import mock

import requests
from django.test import override_settings

from origin.models.chat.unified_models import Channel, ChannelKind, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.services import realtime_push

from .test_base import BaseAPITestCase

SOCKETS = "http://sockets.internal:8889"
SECRET = "test-only-sockets-internal-secret"
MESSAGE_URL = f"{SOCKETS}/internal/broadcast/message"
ACTIVITY_URL = f"{SOCKETS}/internal/broadcast/activity"


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class MessagePushTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        for member in (self.user, self.user2):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.channel = Channel.objects.get(kind=ChannelKind.PM, legacy_chat_id=self.project.pk)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        patcher = mock.patch.object(realtime_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200)
        self.addCleanup(patcher.stop)

    def _message(self):
        return Message.objects.create(
            channel=self.channel,
            seq=1,
            sender=self.user,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
            body_text="hello",
        )

    def _sent(self, url=MESSAGE_URL):
        return [json.loads(c.kwargs["data"]) for c in self.post.call_args_list if c.args[0] == url]

    # ---- what is sent ----

    def test_a_pushed_message_carries_the_event_and_the_message_as_the_app_reads_it(self):
        message = self._message()

        with self.captureOnCommitCallbacks(execute=True):
            realtime_push.push_message(message, realtime_push.CREATED)

        (sent,) = self._sent()
        self.assertEqual(sent["event"], "created")
        self.assertEqual(sent["message"]["id"], str(message.id))
        self.assertEqual(sent["message"]["channelId"], str(self.channel.id))
        self.assertEqual(sent["message"]["channelKind"], ChannelKind.PM)
        self.assertEqual(sent["message"]["bodyText"], "hello")
        self.assertEqual(sent["message"]["body"], message.body)

    def test_an_update_is_sent_as_updated(self):
        message = self._message()

        with self.captureOnCommitCallbacks(execute=True):
            realtime_push.push_message(message, realtime_push.UPDATED)

        self.assertEqual(self._sent()[0]["event"], "updated")

    def test_nothing_is_sent_until_the_write_commits(self):
        message = self._message()

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            realtime_push.push_message(message, realtime_push.CREATED)

        self.post.assert_not_called()
        self.assertEqual(len(callbacks), 1)

    def test_the_push_is_signed_and_goes_to_the_message_path(self):
        message = self._message()

        with self.captureOnCommitCallbacks(execute=True):
            realtime_push.push_message(message, realtime_push.CREATED)

        self.assertEqual(self.post.call_args.args[0], MESSAGE_URL)
        headers = self.post.call_args.kwargs["headers"]
        self.assertTrue(headers["X-Genos-Signature"].startswith("sha256="))
        self.assertFalse(self.post.call_args.kwargs["allow_redirects"])

    # ---- when it must not send, and never raise ----

    @override_settings(SOCKETS_INTERNAL_URL="")
    def test_with_no_sockets_url_it_does_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            realtime_push.push_message(self._message(), realtime_push.CREATED)
        self.post.assert_not_called()

    @override_settings(SOCKETS_INTERNAL_SECRET="")
    def test_with_no_secret_it_warns_and_sends_nothing(self):
        with self.assertLogs("origin.services.realtime_push", level="WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                realtime_push.push_message(self._message(), realtime_push.CREATED)
        self.post.assert_not_called()
        self.assertIn("SOCKETS_INTERNAL_SECRET is unset", logs.output[0])

    def test_an_unreachable_sockets_is_logged_and_never_raised(self):
        self.post.side_effect = requests.ConnectionError("down")
        message = self._message()

        with self.assertLogs("origin.services.realtime_push", level="WARNING"):
            with self.captureOnCommitCallbacks(execute=True):
                realtime_push.push_message(message, realtime_push.CREATED)

    def test_a_refusal_from_sockets_is_logged_and_never_raised(self):
        """The state before genos-sockets has the route: a 404, and the card shows at next sync."""
        self.post.return_value = mock.Mock(status_code=404)
        message = self._message()

        with self.assertLogs("origin.services.realtime_push", level="WARNING") as logs:
            with self.captureOnCommitCallbacks(execute=True):
                realtime_push.push_message(message, realtime_push.CREATED)
        self.assertIn("status=404", logs.output[0])

    def test_a_failure_while_building_the_push_never_escapes(self):
        message = self._message()

        with (
            mock.patch(
                "origin.serializers.chat.unified_serializers.MessageSerializer",
                side_effect=RuntimeError("cannot serialize"),
            ),
            self.assertLogs("origin.services.realtime_push", level="WARNING") as logs,
        ):
            realtime_push.push_message(message, realtime_push.CREATED)

        self.post.assert_not_called()
        self.assertIn("could not be scheduled", logs.output[0])

    def test_a_message_that_is_gone_is_skipped(self):
        message = self._message()
        message.delete()

        with self.captureOnCommitCallbacks(execute=True):
            realtime_push.push_message(message, realtime_push.CREATED)

        self.post.assert_not_called()


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class CardPushTests(BaseAPITestCase):
    """The card poster announces what it wrote."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        for member in (self.user, self.user2):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        patcher = mock.patch.object(realtime_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200)
        self.addCleanup(patcher.stop)

    def _create(self, **args):
        with self.captureOnCommitCallbacks(execute=True):
            out = CREATE_TASK.run(
                {"title": "Ship it", "project_id": self.project.project_id, **args}, self.ctx
            )
        return TaskMaster.objects.get(task_id=out["task_id"])

    def _urls(self):
        return [c.args[0] for c in self.post.call_args_list]

    def test_a_created_task_announces_its_card_to_the_chat_and_its_assignee_to_them(self):
        task = self._create(assignee_id=str(self.user2.id))

        card = Message.objects.get(task_id=task.task_id)
        messages = [
            json.loads(c.kwargs["data"])
            for c in self.post.call_args_list
            if c.args[0] == MESSAGE_URL
        ]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["event"], "created")
        self.assertEqual(messages[0]["message"]["id"], str(card.id))
        self.assertEqual(messages[0]["message"]["taskId"], task.task_id)
        self.assertEqual(self._urls().count(ACTIVITY_URL), 1)

    def test_an_unassigned_task_announces_the_card_and_pings_nobody(self):
        self._create()

        self.assertEqual(self._urls().count(MESSAGE_URL), 1)
        self.assertEqual(self._urls().count(ACTIVITY_URL), 0)

    def test_a_card_that_was_not_posted_is_not_announced(self):
        task = self._create()
        self.post.reset_mock()

        from origin.services.task_card_post import post_task_created_card

        with self.captureOnCommitCallbacks(execute=True):
            self.assertIsNone(post_task_created_card(task, actor_id=self.user.id))

        self.post.assert_not_called()

    def test_an_unreachable_sockets_never_costs_the_task_or_its_card(self):
        self.post.side_effect = requests.ConnectionError("down")

        with self.assertLogs("origin.services.realtime_push", level="WARNING"):
            task = self._create(assignee_id=str(self.user2.id))

        self.assertTrue(Message.objects.filter(task_id=task.task_id).exists())
