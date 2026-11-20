"""What the "sockets refused / failed" log line says (MIP-1724).

`_post` used to read `taskId` and `commentId` from every payload, so a refused chat push logged
`task=None comment=None` and did not say WHICH chat was not announced. The line now carries the
ids that exist in the payload of that kind of push (and the path), and never a title, a body or a
member list.
"""

from unittest import mock

import requests
from django.test import SimpleTestCase, TestCase, override_settings

from origin.services import realtime_push

SOCKETS = "http://sockets.internal:8889"
SECRET = "test-only-sockets-internal-secret"
USER = "11111111-1111-1111-1111-111111111111"
TEAM = "22222222-2222-2222-2222-222222222222"
CHANNEL = "33333333-3333-3333-3333-333333333333"
MESSAGE = "44444444-4444-4444-4444-444444444444"
LOGGER = "origin.services.realtime_push"


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class RefusedLineTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch.object(realtime_push.requests, "post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def refused(self, payload, path, what, status=404):
        self.post.return_value = mock.Mock(status_code=status)
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.assertFalse(realtime_push._post(payload, path, what))
        self.assertEqual(len(logs.output), 1)
        return logs.output[0]

    def test_a_refused_channel_push_names_the_chat(self):
        line = self.refused(
            {"userId": USER, "teamId": TEAM, "channelId": CHANNEL, "kind": 2},
            realtime_push.CHANNEL_APPEARED_PATH,
            "channel",
        )
        self.assertIn("sockets channel push refused", line)
        self.assertIn("path=/internal/broadcast/channel", line)
        for fact in (f"user={USER}", f"team={TEAM}", f"channel={CHANNEL}", "kind=2", "status=404"):
            self.assertIn(fact, line)
        self.assertNotIn("None", line)
        self.assertNotIn("task=", line)
        self.assertNotIn("comment=", line)

    def test_a_failed_channel_push_says_the_same(self):
        self.post.side_effect = requests.ConnectionError("down")
        with self.assertLogs(LOGGER, level="WARNING") as logs:
            self.assertFalse(
                realtime_push._post(
                    {"userId": USER, "teamId": TEAM, "channelId": CHANNEL, "kind": 2},
                    realtime_push.CHANNEL_APPEARED_PATH,
                    "channel",
                )
            )
        line = logs.output[0]
        self.assertIn("sockets channel push failed", line)
        self.assertIn(f"channel={CHANNEL}", line)
        self.assertIn(f"user={USER}", line)
        self.assertIn("down", line)
        self.assertNotIn("None", line)

    def test_a_task_comment_push_still_names_the_task_and_the_comment(self):
        line = self.refused(
            {"projectId": 7, "taskId": 41, "commentId": 3, "action": "created", "isPrivate": True},
            realtime_push.BROADCAST_PATH,
            "task-comment",
            status=401,
        )
        for fact in ("project=7", "task=41", "comment=3", "action=created", "status=401"):
            self.assertIn(fact, line)
        self.assertNotIn("None", line)
        self.assertNotIn("isPrivate", line)

    def test_a_message_push_names_the_message_and_its_chat_not_its_text(self):
        line = self.refused(
            {
                "event": "created",
                "message": {"id": MESSAGE, "channelId": CHANNEL, "bodyText": "secret plan"},
            },
            realtime_push.MESSAGE_PATH,
            "message",
        )
        for fact in ("event=created", f"message={MESSAGE}", f"channel={CHANNEL}"):
            self.assertIn(fact, line)
        self.assertNotIn("secret plan", line)

    def test_an_activity_push_names_the_activity_and_where_it_points(self):
        line = self.refused(
            {
                "userId": USER,
                "activity": {
                    "id": "act-1",
                    "channelId": CHANNEL,
                    "messageId": MESSAGE,
                    "title": "Ken mentioned you: launch date",
                },
            },
            realtime_push.ACTIVITY_PATH,
            "activity",
        )
        for fact in (f"user={USER}", "activity=act-1", f"channel={CHANNEL}", f"message={MESSAGE}"):
            self.assertIn(fact, line)
        self.assertNotIn("launch date", line)

    def test_a_membership_push_names_the_rooms_and_says_how_many_more(self):
        line = self.refused(
            {
                "userId": USER,
                "teamId": TEAM,
                "scope": "team",
                "projectIds": [1, 2, 3, 4, 5],
                "channels": [{"kind": 2, "id": f"c{i}"} for i in range(4)],
            },
            realtime_push.MEMBERSHIP_PATH,
            "membership",
        )
        for fact in ("scope=team", "projects=1,2,3+2", "channels=c0,c1,c2+1", f"user={USER}"):
            self.assertIn(fact, line)

    def test_a_payload_with_no_ids_does_not_print_none(self):
        line = self.refused({}, realtime_push.INBOX_PATH, "inbox")
        self.assertIn("no-ids", line)
        self.assertNotIn("None", line)

    def test_a_long_value_is_cut(self):
        line = self.refused(
            {"channelId": "x" * 500}, realtime_push.CHANNEL_APPEARED_PATH, "channel"
        )
        self.assertIn("channel=" + "x" * 64 + " ", line)
        self.assertNotIn("x" * 65, line)

    def test_the_secret_and_signature_are_not_logged(self):
        line = self.refused({"userId": USER}, realtime_push.CHANNEL_APPEARED_PATH, "channel")
        self.assertNotIn(SECRET, line)
        self.assertNotIn("sha256=", line)


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class ThroughTheRealCallerTests(TestCase):
    """The line a real caller produces: the new channel push, refused by an older sockets (404)."""

    def test_the_channel_push_logs_which_chat_was_refused(self):
        with (
            mock.patch.object(realtime_push.requests, "post") as post,
            self.assertLogs(LOGGER, level="WARNING") as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            post.return_value = mock.Mock(status_code=404)
            realtime_push.push_channel_appeared(
                user_ids=[USER], team_id=TEAM, channel_id=CHANNEL, kind=2
            )
        refused = [line for line in logs.output if "push refused" in line]
        self.assertEqual(len(refused), 1)
        self.assertIn(f"channel={CHANNEL}", refused[0])
        self.assertIn(f"user={USER}", refused[0])
        self.assertIn("status=404", refused[0])
        self.assertNotIn("None", refused[0])
