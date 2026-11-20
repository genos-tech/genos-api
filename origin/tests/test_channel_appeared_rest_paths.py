"""MIP-1722: a DM made over REST, a REST add-member and a self-join announce to the people they
give a chat to (MIP-1719 did the group create and the restore).

`push_channel_appeared` posts one notice per person. What these pin, per path:

* DM create: both sides are told of a NEW DM; a plain re-open of a live DM tells nobody; a DM that
  was deleted and is re-opened comes back for everyone still in it; a requester who had left gets
  it back on their own devices only; a refused create and a rolled-back one tell nobody.
* Add member: ONLY the people who gained the chat (a re-add of a live member, or the existing
  roster, is not told); a refused add tells nobody.
* Self-join: the joiner (their other devices) only; a re-join of a chat they are already in tells
  nobody; a refused join (private, unknown, outsider) tells nobody.
* The calls genos-sockets' own `channel.create` / `channel.member.add` / `channel.join` handlers
  make carry `X-Genos-Relay: sockets` and are NOT announced again (those handlers send
  `channel.created` themselves).
* The announcement waits for the commit and a sockets outage never fails the write.
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
)
from origin.models.common.team_models import TeamMembers
from origin.services import realtime_push
from origin.services.realtime_push import CHANNEL_APPEARED_PATH

from .test_base import BaseAPITestCase

User = get_user_model()

SOCKETS = "http://sockets.internal:8889"
SECRET = "test-only-sockets-internal-secret"
RELAY = {"X-Genos-Relay": "sockets"}


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.user3 = User.objects.create_user(
            username="third1722", email="third1722@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        patcher = mock.patch.object(realtime_push, "_post", return_value=True)
        self.push = patcher.start()
        self.addCleanup(patcher.stop)
        settings_patch = self.settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)

    def told(self):
        for call in self.push.call_args_list:
            self.assertEqual(call.args[1], CHANNEL_APPEARED_PATH)
        return [c.args[0] for c in self.push.call_args_list]

    def told_ids(self):
        return sorted(p["userId"] for p in self.told())

    def call(self, user, method, url, body=None, headers=None):
        self.authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            return getattr(self.client, method)(
                url, body or {}, format="json", headers=headers or {}
            )


class DmCreateTests(_Base):
    def setUp(self):
        super().setUp()
        self.url = reverse("v3_channel_list")

    def create_dm(self, user=None, other=None, headers=None):
        return self.call(
            user or self.user,
            "post",
            self.url,
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": str((other or self.user2).id),
            },
            headers,
        )

    def test_a_new_dm_is_announced_to_both_sides_once(self):
        res = self.create_dm()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(self.told_ids(), sorted([str(self.user.id), str(self.user2.id)]))
        for payload in self.told():
            self.assertEqual(
                {k: v for k, v in payload.items() if k != "userId"},
                {
                    "teamId": str(self.team.team_id),
                    "channelId": res.data["channel"]["id"],
                    "kind": int(ChannelKind.DM),
                },
            )

    def test_a_self_dm_is_announced_to_its_one_member(self):
        res = self.create_dm(other=self.user)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(self.told_ids(), [str(self.user.id)])

    def test_the_dm_the_sockets_create_handler_announces_itself_is_not_announced_again(self):
        res = self.create_dm(headers=RELAY)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.push.assert_not_called()

    def test_reopening_a_live_dm_tells_nobody(self):
        self.create_dm()
        self.push.reset_mock()
        res = self.create_dm(user=self.user2, other=self.user)
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        res = self.create_dm()
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.push.assert_not_called()

    def test_a_deleted_dm_that_is_reopened_comes_back_for_everyone_still_in_it(self):
        res = self.create_dm()
        channel = Channel.objects.get(id=res.data["channel"]["id"])
        Channel.objects.filter(pk=channel.pk).update(is_deleted=True)
        self.push.reset_mock()
        res = self.create_dm()
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.told_ids(), sorted([str(self.user.id), str(self.user2.id)]))

    def test_a_deleted_dm_is_not_announced_to_a_side_that_left_it(self):
        res = self.create_dm()
        channel = Channel.objects.get(id=res.data["channel"]["id"])
        Channel.objects.filter(pk=channel.pk).update(is_deleted=True)
        ChannelMember.objects.filter(channel=channel, user=self.user2).update(is_deleted=True)
        self.push.reset_mock()
        self.create_dm()
        self.assertEqual(self.told_ids(), [str(self.user.id)])

    def test_a_requester_who_had_left_gets_it_back_on_their_own_devices_only(self):
        res = self.create_dm()
        channel = Channel.objects.get(id=res.data["channel"]["id"])
        ChannelMember.objects.filter(channel=channel, user=self.user).update(is_deleted=True)
        self.push.reset_mock()
        res = self.create_dm()
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.told_ids(), [str(self.user.id)])

    def test_a_refused_dm_announces_nothing(self):
        res = self.call(
            self.user,
            "post",
            self.url,
            {"kind": ChannelKind.DM, "team_id": str(self.team.team_id)},
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        res = self.call(
            self.user,
            "post",
            self.url,
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        outsider = User.objects.create_user(
            username="outsider1722", email="outsider1722@example.com", password="x"
        )
        res = self.create_dm(other=outsider)
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.push.assert_not_called()

    def test_a_dm_that_is_rolled_back_announces_nothing(self):
        """The notice is scheduled inside the create's transaction: if anything after it fails,
        the DM is rolled back and nobody is told."""
        real = realtime_push.push_channel_appeared

        def announce_then_fail(**kwargs):
            real(**kwargs)
            raise RuntimeError("the write failed after the announcement was scheduled")

        self.authenticate(self.user)
        with (
            mock.patch(
                "origin.views.chat.channel_views.push_channel_appeared",
                side_effect=announce_then_fail,
            ),
            self.captureOnCommitCallbacks(execute=True),
            self.assertRaises(RuntimeError),
        ):
            self.client.post(
                self.url,
                {
                    "kind": ChannelKind.DM,
                    "team_id": str(self.team.team_id),
                    "other_user_id": str(self.user2.id),
                },
                format="json",
            )
        self.assertFalse(ChannelDirectPair.objects.exists())
        self.push.assert_not_called()

    def test_the_announcement_waits_for_the_commit(self):
        self.authenticate(self.user)
        self.client.post(
            self.url,
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": str(self.user2.id),
            },
            format="json",
        )
        self.push.assert_not_called()  # this test's transaction never commits

    def test_a_sockets_outage_does_not_fail_the_create(self):
        self.push.return_value = False
        res = self.create_dm()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertTrue(ChannelDirectPair.objects.exists())


class AddMemberTests(_Base):
    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Squad", owner=self.user
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.gm, user=self.user2, role="member")
        self.url = reverse("v3_channel_members", args=[self.gm.id])
        self.user4 = User.objects.create_user(
            username="fourth1722", email="fourth1722@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user4)

    def add(self, *users, headers=None, user=None):
        return self.call(
            user or self.user,
            "post",
            self.url,
            {"user_ids": [str(u.id) for u in users]},
            headers,
        )

    def test_only_the_added_people_are_told_not_the_roster(self):
        res = self.add(self.user3, self.user4)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(self.told_ids(), sorted([str(self.user3.id), str(self.user4.id)]))
        for payload in self.told():
            self.assertEqual(
                {k: v for k, v in payload.items() if k != "userId"},
                {
                    "teamId": str(self.team.team_id),
                    "channelId": str(self.gm.id),
                    "kind": int(ChannelKind.GM),
                },
            )

    def test_a_re_add_of_a_live_member_is_not_told(self):
        res = self.add(self.user2, self.user3)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(self.told_ids(), [str(self.user3.id)])

    def test_a_person_who_had_left_is_told_when_re_added(self):
        ChannelMember.objects.create(
            channel=self.gm, user=self.user3, role="member", is_deleted=True
        )
        res = self.add(self.user3)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(self.told_ids(), [str(self.user3.id)])

    def test_adding_only_existing_members_tells_nobody(self):
        res = self.add(self.user2)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.push.assert_not_called()

    def test_the_add_the_sockets_handler_announces_itself_is_not_announced_again(self):
        res = self.add(self.user3, headers=RELAY)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.push.assert_not_called()

    def test_a_marker_that_is_not_the_sockets_one_does_not_silence_it(self):
        res = self.add(self.user3, headers={"X-Genos-Relay": "somebody"})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(self.told_ids(), [str(self.user3.id)])

    def test_a_refused_add_announces_nothing(self):
        # Empty list, an unknown person, a non-member caller, a DM.
        res = self.call(self.user, "post", self.url, {"user_ids": []})
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        res = self.call(
            self.user, "post", self.url, {"user_ids": ["00000000-0000-0000-0000-000000000000"]}
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        res = self.add(self.user4, user=self.user3)
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="")
        ChannelMember.objects.create(channel=dm, user=self.user, role="member")
        res = self.call(
            self.user,
            "post",
            reverse("v3_channel_members", args=[dm.id]),
            {"user_ids": [str(self.user3.id)]},
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.push.assert_not_called()
        self.assertFalse(ChannelMember.objects.filter(user=self.user3).exists())

    def test_the_announcement_waits_for_the_commit(self):
        self.authenticate(self.user)
        self.client.post(self.url, {"user_ids": [str(self.user3.id)]}, format="json")
        self.push.assert_not_called()

    def test_a_sockets_outage_does_not_fail_the_add(self):
        self.push.return_value = False
        res = self.add(self.user3, self.user4)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(len(self.told()), 1, "stopped at the first failure")
        self.assertTrue(ChannelMember.objects.filter(channel=self.gm, user=self.user4).exists())


class SelfJoinTests(_Base):
    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Open", is_private=False, owner=self.user
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        self.url = reverse("v3_channel_join", args=[self.gm.id])

    def join(self, user=None, headers=None, url=None):
        return self.call(user or self.user2, "post", url or self.url, {}, headers)

    def test_the_joiner_alone_is_told(self):
        res = self.join()
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.told_ids(), [str(self.user2.id)])
        self.assertEqual(
            {k: v for k, v in self.told()[0].items() if k != "userId"},
            {
                "teamId": str(self.team.team_id),
                "channelId": str(self.gm.id),
                "kind": int(ChannelKind.GM),
            },
        )

    def test_a_person_who_had_left_is_told_when_they_re_join(self):
        ChannelMember.objects.create(
            channel=self.gm, user=self.user2, role="member", is_deleted=True
        )
        res = self.join()
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.told_ids(), [str(self.user2.id)])

    def test_a_re_join_of_a_chat_already_in_tells_nobody(self):
        self.join()
        self.push.reset_mock()
        res = self.join()
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.push.assert_not_called()

    def test_the_join_the_sockets_handler_announces_itself_is_not_announced_again(self):
        res = self.join(headers=RELAY)
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.push.assert_not_called()

    def test_a_refused_join_announces_nothing(self):
        private = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Closed", is_private=True, owner=self.user
        )
        res = self.join(url=reverse("v3_channel_join", args=[private.id]))
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        mdm = Channel.objects.create(
            team=self.team, kind=ChannelKind.MDM, title="m", owner=self.user
        )
        ChannelMember.objects.create(channel=mdm, user=self.user, role="owner")
        res = self.join(user=self.user, url=reverse("v3_channel_join", args=[mdm.id]))
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        outsider = User.objects.create_user(
            username="outsider1722b", email="outsider1722b@example.com", password="x"
        )
        res = self.join(user=outsider)
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.push.assert_not_called()
        self.assertFalse(ChannelMember.objects.filter(channel=self.gm, user=outsider).exists())

    def test_the_announcement_waits_for_the_commit(self):
        self.authenticate(self.user2)
        self.client.post(self.url, {}, format="json")
        self.push.assert_not_called()

    def test_a_sockets_outage_does_not_fail_the_join(self):
        self.push.return_value = False
        res = self.join()
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertTrue(ChannelMember.objects.filter(channel=self.gm, user=self.user2).exists())
