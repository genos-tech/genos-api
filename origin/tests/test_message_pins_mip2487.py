"""Messages pinned in a chat for everyone (MIP-2487)."""

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessagePin,
)
from origin.search_engine import quota
from origin.services import message_tombstone

from .test_message_retention import RetentionTestBase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas


class PinBase(RetentionTestBase):
    def pins_url(self, channel=None):
        return f"/api/v3/channels/{(channel or self.channel).id}/message_pins/"

    def pin_url(self, message):
        return f"/api/v3/messages/{message.id}/message_pin/"

    def pin(self, message):
        return self.client.post(self.pin_url(message))

    def listed(self):
        res = self.client.get(self.pins_url())
        self.assertEqual(res.status_code, 200)
        return [row["message"]["bodyText"] for row in res.data["pins"]]


class MessagePinTests(PinBase):
    def test_a_pin_is_shared_and_names_who_made_it(self):
        message = self.make_message("keep this")
        res = self.pin(message)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(
            res.data,
            {
                "messageId": str(message.id),
                "channelId": str(self.channel.id),
                "channelKind": ChannelKind.GM,
            },
        )
        self.authenticate(self.user2)
        res = self.client.get(self.pins_url())
        self.assertEqual(res.data["limit"], 50)
        self.assertEqual(res.data["hiddenCount"], 0)
        (row,) = res.data["pins"]
        self.assertEqual(row["messageId"], str(message.id))
        self.assertEqual(row["message"]["bodyText"], "keep this")
        self.assertEqual(
            row["pinnedBy"], {"userId": str(self.user.id), "username": self.user.username}
        )

    def test_pinning_again_changes_nothing_and_anyone_may_unpin(self):
        message = self.make_message("once")
        self.pin(message)
        self.authenticate(self.user2)
        self.assertEqual(self.pin(message).status_code, 200)
        self.assertEqual(MessagePin.objects.get().pinned_by_id, self.user.id)
        res = self.client.delete(self.pin_url(message))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["removed"])
        self.assertFalse(self.client.delete(self.pin_url(message)).data["removed"])
        self.assertEqual(self.listed(), [])

    def test_newest_pin_first(self):
        first, second = self.make_message("a"), self.make_message("b")
        self.pin(second)
        self.pin(first)
        self.assertEqual(self.listed(), ["a", "b"])

    def test_the_sidebar_chat_pin_is_untouched(self):
        self.pin(self.make_message("x"))
        self.assertEqual(self.client.get("/api/v3/pins/").data["pins"], [])

    def test_a_non_member_gets_404_everywhere(self):
        message = self.make_message("secret")
        self.pin(message)
        ChannelMember.objects.filter(channel=self.channel, user=self.user2).delete()
        self.authenticate(self.user2)
        self.assertEqual(self.client.get(self.pins_url()).status_code, 404)
        self.assertEqual(self.pin(message).status_code, 404)
        self.assertEqual(self.client.delete(self.pin_url(message)).status_code, 404)
        self.assertEqual(MessagePin.objects.count(), 1)

    def test_deleted_and_thread_messages_are_refused(self):
        root = self.make_message("root")
        reply = self.make_message("reply", thread_root=root)
        res = self.pin(reply)
        self.assertEqual(res.status_code, 400)
        self.assertTrue(res.data["error"].startswith("THREAD_REPLY_NOT_PINNABLE"))
        Message.objects.filter(pk=root.pk).update(deleted_at=timezone.now())
        res = self.pin(root)
        self.assertEqual(res.status_code, 400)
        self.assertTrue(res.data["error"].startswith("MESSAGE_DELETED"))
        self.assertEqual(MessagePin.objects.count(), 0)

    def test_an_archived_chat_can_be_read_but_not_changed(self):
        pinned, other = self.make_message("old"), self.make_message("new")
        self.pin(pinned)
        Channel.objects.filter(pk=self.channel.pk).update(archived_at=timezone.now())
        self.assertEqual(self.pin(other).status_code, 409)
        self.assertEqual(self.client.delete(self.pin_url(pinned)).status_code, 409)
        self.assertEqual(self.listed(), ["old"])

    def test_the_limit_per_chat(self):
        messages = [self.make_message(str(i)) for i in range(MessagePin.LIMIT_PER_CHANNEL + 1)]
        for message in messages[:-1]:
            MessagePin.objects.create(channel=self.channel, message=message)
        res = self.pin(messages[-1])
        self.assertEqual(res.status_code, 409)
        self.assertTrue(res.data["error"].startswith("PIN_LIMIT_REACHED"))
        # An already pinned one is still a harmless repeat at the limit.
        self.assertEqual(self.pin(messages[0]).status_code, 200)

    def test_deleting_the_message_drops_its_pin(self):
        message = self.make_message("gone soon")
        self.pin(message)
        self.assertEqual(self.client.delete(f"/api/v3/messages/{message.id}/").status_code, 204)
        self.assertEqual(MessagePin.objects.count(), 0)

    def test_the_tombstone_and_a_hard_delete_drop_the_pin(self):
        first, second = self.make_message("one"), self.make_message("two")
        self.pin(first)
        self.pin(second)
        message_tombstone.tombstone_message(first)
        second.delete()
        self.assertEqual(MessagePin.objects.count(), 0)

    def test_a_pin_outlives_the_account_that_made_it(self):
        message = self.make_message("kept")
        self.authenticate(self.user2)
        self.pin(message)
        MessagePin.objects.update(pinned_by=None)
        self.authenticate()
        (row,) = self.client.get(self.pins_url()).data["pins"]
        self.assertIsNone(row["pinnedBy"])

    def test_the_list_costs_the_same_for_one_pin_or_many(self):
        self.pin(self.make_message("first"))
        self.listed()
        with CaptureQueriesContext(connection) as one:
            self.listed()
        for i in range(5):
            self.pin(self.make_message(str(i)))
        with CaptureQueriesContext(connection) as many:
            self.assertEqual(len(self.listed()), 6)
        self.assertEqual(len(many), len(one))


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class MessagePinRetentionTests(PinBase):
    def test_a_pin_past_the_window_is_counted_not_sent(self):
        old = self.make_message("ancient", days_ago=120)
        recent = self.make_message("recent")
        MessagePin.objects.create(channel=self.channel, message=old, pinned_by=self.user)
        self.pin(recent)
        res = self.client.get(self.pins_url())
        self.assertEqual([r["message"]["bodyText"] for r in res.data["pins"]], ["recent"])
        self.assertEqual(res.data["hiddenCount"], 1)
        quota.invalidate_effective_tier([self.user.id])
        self.assertEqual(self.pin(old).status_code, 404)


class OlderMessagesTests(PinBase):
    def older(self, before):
        return self.client.get(
            f"/api/v3/channels/{self.channel.id}/messages/older/?before={before.id}"
        )

    def test_the_page_before_a_message_oldest_first(self):
        first = self.make_message("1", days_ago=3)
        second = self.make_message("2", days_ago=2)
        third = self.make_message("3", days_ago=1)
        self.make_message("reply", thread_root=first)
        res = self.older(third)
        self.assertEqual(res.status_code, 200)
        self.assertEqual([m["id"] for m in res.data["messages"]], [str(first.id), str(second.id)])
        self.assertFalse(res.data["hasMore"])

    def test_a_long_history_comes_in_pages(self):
        base = [self.make_message(str(i)) for i in range(4)]
        from unittest import mock

        with mock.patch("origin.views.chat.message_views.MAX_MESSAGES_PER_DELTA", 2):
            res = self.older(base[3])
        self.assertEqual([m["bodyText"] for m in res.data["messages"]], ["1", "2"])
        self.assertTrue(res.data["hasMore"])

    def test_before_must_be_a_message_of_this_chat(self):
        other = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="other", owner=self.user
        )
        foreign = Message.objects.create(channel=other, sender=self.user, seq=1, body=[])
        self.assertEqual(self.older(foreign).status_code, 404)
        res = self.client.get(f"/api/v3/channels/{self.channel.id}/messages/older/?before=nope")
        self.assertEqual(res.status_code, 400)

    def test_a_non_member_gets_404(self):
        message = self.make_message("x")
        ChannelMember.objects.filter(channel=self.channel, user=self.user2).delete()
        self.authenticate(self.user2)
        self.assertEqual(self.older(message).status_code, 404)

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
    def test_the_history_window_applies(self):
        self.make_message("ancient", days_ago=120)
        recent = self.make_message("recent", days_ago=1)
        newest = self.make_message("newest")
        res = self.older(newest)
        self.assertEqual([m["id"] for m in res.data["messages"]], [str(recent.id)])
