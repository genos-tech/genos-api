"""MIP-1719: a chat that APPEARS for people is announced to their open apps.

A group its owner restores (MIP-1715) and a group made through the REST API did not reach other
members' open apps until their next chat-list refresh: the genos-api -> genos-sockets push had
notices for a removal, messages, activities, inbox and to-dos, but none that meant "a chat
appeared", and the app's own `channel.created` is emitted by the sockets `channel.create` handler
for a chat made in the app.

`push_channel_appeared` posts `/internal/broadcast/channel` once per member. What these pin:

* the helper: the signed body is facts only; each member gets one; the posts stop at the first
  failure; a huge chat is capped; it waits for the commit and a rollback announces nothing; no
  sockets URL or secret is a no-op; and it never raises, whatever sockets answers (an OLD sockets
  that has no such route answers 404, which is only logged);
* a REST create announces to the chat's members, but NOT when the call is the one genos-sockets'
  own `channel.create` handler makes (that handler broadcasts `channel.created` itself, so
  announcing again would announce twice), and a DM or a rolled-back create announces nothing.

Restore's own announcement is pinned in `test_group_chat_restore` (`LiveNoticeTests`).
"""

import hashlib
import hmac
import json
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.services import realtime_push
from origin.services.realtime_push import (
    CHANNEL_APPEARED_PATH,
    MAX_CHANNEL_APPEARED_PUSHES,
    announced_by_sockets,
    push_channel_appeared,
)

from .cross_team_fixtures import CrossTeamTestCase
from .test_base import BaseAPITestCase

User = get_user_model()

SOCKETS = "http://sockets.internal:8889"
SECRET = "test-only-sockets-internal-secret"
TEAM = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CHAN = "cccccccc-cccc-cccc-cccc-cccccccccccc"


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class HelperTests(TestCase):
    def setUp(self):
        patcher = mock.patch.object(realtime_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200)
        self.addCleanup(patcher.stop)

    def push(self, users=("u-1", "u-2"), **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            push_channel_appeared(
                user_ids=list(users),
                team_id=kwargs.get("team_id", TEAM),
                channel_id=kwargs.get("channel_id", CHAN),
                kind=kwargs.get("kind", ChannelKind.GM),
            )

    def sent(self):
        return [json.loads(c.kwargs["data"]) for c in self.post.call_args_list]

    # ---- what is sent ----

    def test_each_member_gets_one_signed_post_with_facts_only(self):
        self.push()
        self.assertEqual(
            self.sent(),
            [
                {"userId": "u-1", "teamId": TEAM, "channelId": CHAN, "kind": 2},
                {"userId": "u-2", "teamId": TEAM, "channelId": CHAN, "kind": 2},
            ],
        )
        for call in self.post.call_args_list:
            self.assertEqual(call.args[0], f"{SOCKETS}{CHANNEL_APPEARED_PATH}")
            self.assertEqual(CHANNEL_APPEARED_PATH, "/internal/broadcast/channel")
            headers = call.kwargs["headers"]
            digest = hmac.new(
                SECRET.encode(),
                headers["X-Genos-Timestamp"].encode() + b"." + call.kwargs["data"],
                hashlib.sha256,
            ).hexdigest()
            self.assertEqual(headers["X-Genos-Signature"], "sha256=" + digest)
            self.assertFalse(call.kwargs["allow_redirects"])

    def test_ids_are_sent_as_text_and_blank_ones_are_skipped(self):
        self.push(users=[None, "", "u-1"], team_id=TEAM, channel_id=CHAN)
        self.assertEqual([p["userId"] for p in self.sent()], ["u-1"])

    def test_nobody_to_tell_posts_nothing(self):
        self.push(users=[])
        self.post.assert_not_called()

    def test_nothing_is_sent_before_the_commit(self):
        push_channel_appeared(user_ids=["u-1"], team_id=TEAM, channel_id=CHAN, kind=2)
        self.post.assert_not_called()  # the test case's transaction never commits

    # ---- failure ----

    def test_the_posts_stop_at_the_first_failure(self):
        self.post.return_value = mock.Mock(status_code=503)
        self.push(users=["u-1", "u-2", "u-3"])
        self.assertEqual(self.post.call_count, 1, "one timeout, not one per member")

    def test_a_connection_error_stops_the_posts_and_raises_nothing(self):
        self.post.side_effect = requests.ConnectionError("sockets is down")
        self.push(users=["u-1", "u-2", "u-3"])
        self.assertEqual(self.post.call_count, 1)

    def test_an_older_sockets_with_no_such_route_is_only_logged(self):
        """Deploy order must not matter: a new api talking to an old sockets gets a 404."""
        self.post.return_value = mock.Mock(status_code=404)
        with self.assertLogs("origin.services.realtime_push", level="WARNING") as logs:
            self.push(users=["u-1", "u-2"])
        self.assertEqual(self.post.call_count, 1)
        self.assertTrue(any("status=404" in line for line in logs.output))

    def test_it_never_raises_when_it_cannot_even_be_scheduled(self):
        with mock.patch.object(realtime_push.transaction, "on_commit", side_effect=RuntimeError):
            push_channel_appeared(user_ids=["u-1"], team_id=TEAM, channel_id=CHAN, kind=2)

    def test_a_failing_send_does_not_raise_out_of_the_commit(self):
        with mock.patch.object(realtime_push, "_post", side_effect=RuntimeError("boom")):
            self.push()  # robust=True on the callback: logged, never raised

    # ---- how many ----

    def test_a_huge_chat_is_told_only_up_to_the_cap(self):
        people = [f"u-{n}" for n in range(MAX_CHANNEL_APPEARED_PUSHES + 25)]
        self.push(users=people)
        self.assertEqual(self.post.call_count, MAX_CHANNEL_APPEARED_PUSHES)
        self.assertEqual(MAX_CHANNEL_APPEARED_PUSHES, 500)

    # ---- rollback ----

    def test_a_rolled_back_write_announces_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    push_channel_appeared(user_ids=["u-1"], team_id=TEAM, channel_id=CHAN, kind=2)
                    raise RuntimeError("the write failed")
            except RuntimeError:
                pass
        self.post.assert_not_called()

    # ---- not configured ----

    @override_settings(SOCKETS_INTERNAL_URL="")
    def test_no_sockets_url_is_a_no_op(self):
        self.push()
        self.post.assert_not_called()

    @override_settings(SOCKETS_INTERNAL_SECRET="")
    def test_no_secret_is_a_no_op_and_says_so(self):
        with self.assertLogs("origin.services.realtime_push", level="WARNING") as logs:
            self.push()
        self.post.assert_not_called()
        self.assertTrue(any("SOCKETS_INTERNAL_SECRET is unset" in line for line in logs.output))


class RelayMarkerTests(TestCase):
    def request(self, **headers):
        return mock.Mock(headers=headers)

    def test_only_the_sockets_marker_counts(self):
        self.assertTrue(announced_by_sockets(self.request(**{"X-Genos-Relay": "sockets"})))
        self.assertFalse(announced_by_sockets(self.request()))
        self.assertFalse(announced_by_sockets(self.request(**{"X-Genos-Relay": "other"})))
        self.assertFalse(announced_by_sockets(self.request(**{"X-Genos-Relay": ""})))


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class CreateAnnouncesTests(BaseAPITestCase):
    """`POST /api/v3/channels/` for a group."""

    def setUp(self):
        super().setUp()
        self.user3 = User.objects.create_user(
            username="third1719", email="third1719@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        patcher = mock.patch.object(realtime_push, "_post", return_value=True)
        self.push = patcher.start()
        self.addCleanup(patcher.stop)
        self.url = reverse("v3_channel_list")

    def create(self, headers=None, **body):
        self.authenticate(self.user)
        data = {
            "kind": ChannelKind.GM,
            "team_id": str(self.team.team_id),
            "title": "Squad",
            "member_user_ids": [str(self.user2.id)],
            **body,
        }
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url, data, format="json", headers=headers or {})

    def told(self):
        return [c.args[0] for c in self.push.call_args_list]

    def test_a_rest_created_group_is_announced_to_the_creator_and_every_member(self):
        res = self.create(member_user_ids=[str(self.user2.id), str(self.user3.id)])
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        channel_id = res.data["channel"]["id"]

        told = self.told()
        self.assertEqual(
            {p["userId"] for p in told},
            {str(self.user.id), str(self.user2.id), str(self.user3.id)},
        )
        for payload in told:
            self.assertEqual(
                {k: v for k, v in payload.items() if k != "userId"},
                {
                    "teamId": str(self.team.team_id),
                    "channelId": channel_id,
                    "kind": int(ChannelKind.GM),
                },
            )
        for call in self.push.call_args_list:
            self.assertEqual(call.args[1], CHANNEL_APPEARED_PATH)

    def test_a_multi_person_dm_is_announced_with_its_own_kind(self):
        res = self.create(kind=ChannelKind.MDM)
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual({p["kind"] for p in self.told()}, {int(ChannelKind.MDM)})

    def test_the_chat_a_sockets_create_handler_announces_itself_is_not_announced_again(self):
        res = self.create(headers={"X-Genos-Relay": "sockets"})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.push.assert_not_called()

    def test_a_marker_that_is_not_the_sockets_one_does_not_silence_it(self):
        res = self.create(headers={"X-Genos-Relay": "somebody"})
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(len(self.told()), 2)

    def test_a_refused_create_announces_nothing(self):
        res = self.create(title="")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        res = self.create(member_user_ids=["00000000-0000-0000-0000-000000000000"])
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.push.assert_not_called()

    def test_a_direct_message_is_announced_to_both_sides(self):
        """MIP-1719 left the DM alone; MIP-1722 announces it (see test_channel_appeared_rest_paths)."""
        res = self.create(kind=ChannelKind.DM, other_user_id=str(self.user2.id))
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(
            {p["userId"] for p in self.told()}, {str(self.user.id), str(self.user2.id)}
        )

    def test_the_announcement_waits_for_the_commit(self):
        self.authenticate(self.user)
        self.client.post(
            self.url,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team.team_id),
                "title": "Squad",
                "member_user_ids": [str(self.user2.id)],
            },
            format="json",
        )
        self.push.assert_not_called()  # no commit happened in this test's transaction

    def test_a_sockets_outage_does_not_fail_the_create(self):
        self.push.return_value = False
        res = self.create()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertTrue(Channel.objects.filter(title="Squad").exists())
        self.assertEqual(len(self.told()), 1, "stopped at the first failure")

    def test_a_person_who_is_not_a_member_is_never_told(self):
        self.create()
        self.assertNotIn(str(self.user3.id), {p["userId"] for p in self.told()})


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class ExternalCreateTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(realtime_push, "_post", return_value=True)
        self.push = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_create_that_is_rolled_back_with_its_offer_announces_nothing(self):
        # Team C is not connected to team A, so the offer is refused and the chat rolled back.
        with self.captureOnCommitCallbacks(execute=True):
            res = self.create_external_chat([self.team_c.team_id])
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Channel.objects.filter(is_external=True).exists())
        self.push.assert_not_called()

    def test_an_external_chat_announces_only_the_host_side_members_it_already_has(self):
        self.connect_a_and_b()
        with self.captureOnCommitCallbacks(execute=True):
            res = self.create_external_chat([self.team_b.team_id])
        self.assertEqual(res.status_code, 201, res.data)
        # The guest team has only been OFFERED the chat: nobody there is a member yet. (Its owner
        # does get an inbox notice about the offer; that is a different route.)
        announced = {
            c.args[0]["userId"]
            for c in self.push.call_args_list
            if c.args[1] == CHANNEL_APPEARED_PATH
        }
        self.assertEqual(announced, {str(self.a_owner.id)})
        self.assertFalse(
            ChannelMember.objects.filter(
                channel_id=res.data["channel"]["id"], user=self.b_owner
            ).exists()
        )
