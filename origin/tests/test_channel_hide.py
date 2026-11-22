"""MIP-1414: hide a DM / MDM chat from MY sidebar.

`ChannelMember.hidden_at` is a per-person display preference: the chat list leaves the chat out,
everything else (messages, delta sync, threads, membership) is untouched, and a new message in the
chat brings it back. Group chats and project chats are not hideable (400).
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import (
    Channel,
    ChannelDirectPair,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.common.team_models import TeamMembers
from origin.services import realtime_push
from origin.services.realtime_push import CHANNEL_APPEARED_PATH

from .test_base import BaseAPITestCase

User = get_user_model()

SOCKETS = "http://sockets.internal:8889"
SECRET = "test-only-sockets-internal-secret"


def blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="outsider1414", email="outsider1414@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.outsider)
        self.dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM)
        lo, hi = sorted([self.user.id, self.user2.id], key=str)
        ChannelDirectPair.objects.create(channel=self.dm, user_lo=lo, user_hi=hi)
        ChannelMember.objects.create(channel=self.dm, user=self.user, role="member")
        ChannelMember.objects.create(channel=self.dm, user=self.user2, role="member")
        self.gm = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="g")
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.gm, user=self.user2, role="member")
        patcher = mock.patch.object(realtime_push, "_post", return_value=True)
        self.push = patcher.start()
        self.addCleanup(patcher.stop)
        settings_patch = self.settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        self.hide_url = reverse("v3_channel_hide", args=[self.dm.id])

    def call(self, user, method, url, body=None):
        self.authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            return getattr(self.client, method)(url, body or {}, format="json")

    def listed_ids(self, user, **params):
        res = self.call(user, "get", reverse("v3_channel_list"), params)
        self.assertEqual(res.status_code, 200, res.data)
        return {c["id"] for c in res.data["channels"]}

    def hidden_at(self, user, channel=None):
        return ChannelMember.objects.get(channel=channel or self.dm, user=user).hidden_at

    def told_ids(self):
        for call in self.push.call_args_list:
            self.assertEqual(call.args[1], CHANNEL_APPEARED_PATH)
        return sorted(c.args[0]["userId"] for c in self.push.call_args_list)

    def send(self, user, text="hi", channel=None, **extra):
        return self.call(
            user,
            "post",
            reverse("v3_messages_delta", args=[(channel or self.dm).id]),
            {"body": blocks(text), "body_text": text, **extra},
        )


class HideTests(_Base):
    def test_hide_takes_the_chat_out_of_my_list_only(self):
        self.assertIn(str(self.dm.id), self.listed_ids(self.user))
        res = self.call(self.user, "post", self.hide_url)
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertNotIn(str(self.dm.id), self.listed_ids(self.user))
        # the other person is unaffected
        self.assertIn(str(self.dm.id), self.listed_ids(self.user2))
        self.assertIsNone(self.hidden_at(self.user2))

    def test_include_hidden_lists_it_again(self):
        self.call(self.user, "post", self.hide_url)
        self.assertIn(str(self.dm.id), self.listed_ids(self.user, include_hidden="1"))

    def test_nothing_is_deleted_and_the_chat_still_works(self):
        msg = Message.objects.create(
            channel=self.dm, sender=self.user2, seq=1, body=blocks("x"), body_text="x"
        )
        self.call(self.user, "post", self.hide_url)
        self.assertTrue(Message.objects.filter(pk=msg.pk).exists())
        self.assertTrue(
            ChannelMember.objects.get(channel=self.dm, user=self.user).is_deleted is False
        )
        res = self.call(self.user, "get", reverse("v3_messages_delta", args=[self.dm.id]))
        self.assertEqual(res.status_code, 200)

    def test_unhide_brings_it_back(self):
        self.call(self.user, "post", self.hide_url)
        res = self.call(self.user, "delete", self.hide_url)
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)
        self.assertIn(str(self.dm.id), self.listed_ids(self.user))

    def test_hide_tells_my_own_devices_once_and_a_repeat_tells_nobody(self):
        self.call(self.user, "post", self.hide_url)
        self.assertEqual(self.told_ids(), [str(self.user.id)])
        self.push.reset_mock()
        self.call(self.user, "post", self.hide_url)
        self.push.assert_not_called()

    def test_a_group_chat_cannot_be_hidden(self):
        res = self.call(self.user, "post", reverse("v3_channel_hide", args=[self.gm.id]))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIsNone(self.hidden_at(self.user, self.gm))

    def test_a_non_member_gets_404_and_nothing_changes(self):
        for method in ("post", "delete"):
            res = self.call(self.outsider, method, self.hide_url)
            self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIsNone(self.hidden_at(self.user))
        self.assertIsNone(self.hidden_at(self.user2))

    def test_unauthenticated_is_refused(self):
        self.unauthenticate()
        self.assertIn(self.client.post(self.hide_url).status_code, (401, 403))


class UnhideOnNewMessageTests(_Base):
    def test_a_message_from_the_other_person_brings_the_chat_back_and_tells_the_app(self):
        self.call(self.user, "post", self.hide_url)
        self.push.reset_mock()
        res = self.send(self.user2)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertIsNone(self.hidden_at(self.user))
        self.assertIn(str(self.dm.id), self.listed_ids(self.user))
        self.assertEqual(self.told_ids(), [str(self.user.id)])

    def test_my_own_message_from_another_device_brings_it_back(self):
        self.call(self.user, "post", self.hide_url)
        self.send(self.user)
        self.assertIsNone(self.hidden_at(self.user))

    def test_a_thread_reply_brings_it_back(self):
        root = Message.objects.create(
            channel=self.dm, sender=self.user2, seq=1, body=blocks("r"), body_text="r"
        )
        self.call(self.user, "post", self.hide_url)
        res = self.send(self.user2, "reply", parent_id=str(root.id))
        self.assertEqual(res.status_code, 201, res.data)
        self.assertIsNone(self.hidden_at(self.user))

    def test_only_the_hidden_member_is_touched_and_told(self):
        self.call(self.user, "post", self.hide_url)
        self.call(self.user2, "post", self.hide_url)
        self.push.reset_mock()
        self.send(self.user2)
        self.assertEqual(self.told_ids(), sorted([str(self.user.id), str(self.user2.id)]))
        self.call(self.user, "post", self.hide_url)
        self.push.reset_mock()
        self.send(self.user2, "again")
        self.assertEqual(self.told_ids(), [str(self.user.id)])  # user2 was not hidden any more

    def test_a_visible_chat_pushes_nothing(self):
        self.send(self.user2)
        self.push.assert_not_called()

    def test_a_resent_message_does_not_announce_again(self):
        self.call(self.user, "post", self.hide_url)
        self.send(self.user2, correlation_id="c-1")
        self.call(self.user, "post", self.hide_url)
        self.push.reset_mock()
        self.send(self.user2, correlation_id="c-1")  # dedup path: no new message
        self.assertIsNotNone(self.hidden_at(self.user))
        self.push.assert_not_called()


class ReopenDmTests(_Base):
    def test_starting_the_dm_again_brings_a_hidden_chat_back(self):
        self.call(self.user, "post", self.hide_url)
        self.push.reset_mock()
        res = self.call(
            self.user,
            "post",
            reverse("v3_channel_list"),
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": str(self.user2.id),
            },
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self.hidden_at(self.user))
        self.assertIn(str(self.dm.id), self.listed_ids(self.user))


class DeleteMineAndRemoveTests(_Base):
    def test_my_rows_go_the_partners_stay_and_the_chat_leaves_only_my_list(self):
        mine = Message.objects.create(
            channel=self.dm, sender=self.user, seq=1, body=blocks("secret"), body_text="secret"
        )
        theirs = Message.objects.create(
            channel=self.dm, sender=self.user2, seq=2, body=blocks("theirs"), body_text="theirs"
        )
        res = self.call(
            self.user,
            "post",
            reverse("v3_my_messages_force_delete", args=[self.dm.id]),
            {"confirm": "delete-permanently"},
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.call(self.user, "post", self.hide_url)
        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.body_text, "")
        self.assertEqual(theirs.body_text, "theirs")
        self.assertNotIn(str(self.dm.id), self.listed_ids(self.user))
        self.assertIn(str(self.dm.id), self.listed_ids(self.user2))
