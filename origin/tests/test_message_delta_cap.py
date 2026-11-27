"""The row cap on a first load of a channel's messages (MIP-2388).

`GET /api/v3/channels/<id>/messages/` without `since` is NOT the whole channel: it is the
newest `MAX_MESSAGES_PER_DELTA` top-level messages, flagged `force_full_reload`.
"""

from datetime import timedelta

from django.db.models import DateTimeField, ExpressionWrapper, F, Value
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.views.utils.incremental import MAX_MESSAGES_PER_DELTA

from .test_base import BaseAPITestCase


class FirstLoadRowCapTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Long channel", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        self.authenticate()

    def first_load(self, message_count):
        Message.objects.bulk_create(
            Message(
                channel=self.channel,
                sender=self.user,
                seq=seq,
                body=[],
                body_text=str(seq),
            )
            for seq in range(1, message_count + 1)
        )
        # One second apart in `seq` order, so "newest" never rests on clock resolution.
        Message.objects.filter(channel=self.channel).update(
            ts_sent_at=ExpressionWrapper(
                Value(timezone.now() - timedelta(hours=1)) + F("seq") * Value(timedelta(seconds=1)),
                output_field=DateTimeField(),
            )
        )
        res = self.client.get(f"/api/v3/channels/{self.channel.id}/messages/")
        self.assertEqual(res.status_code, 200)
        return res, [m["bodyText"] for m in res.data["data"]["messages"]]

    def test_first_load_of_a_long_channel_returns_the_newest_rows_only(self):
        res, texts = self.first_load(MAX_MESSAGES_PER_DELTA + 1)
        # The oldest message is left out; the rest come back oldest first.
        self.assertEqual(texts, [str(n) for n in range(2, MAX_MESSAGES_PER_DELTA + 2)])
        self.assertTrue(res.data["force_full_reload"])

    def test_first_load_at_the_cap_returns_everything_unflagged(self):
        res, texts = self.first_load(MAX_MESSAGES_PER_DELTA)
        self.assertEqual(len(texts), MAX_MESSAGES_PER_DELTA)
        self.assertNotIn("force_full_reload", res.data)
