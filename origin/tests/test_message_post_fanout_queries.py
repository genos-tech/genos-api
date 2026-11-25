"""Posting a chat message costs a CONSTANT number of queries however big the channel is
(MIP-2387).

One `Activity` row per other member is the feed's data model (per-recipient `is_read`, a
per-user `activity.created` push, the read-cursor sweep), so the rows stay. What must not grow
with the channel is everything around them: the response serializes every row, and each row
embeds the same message, so a per-row lazy load (actor, channel, message sender, ...) turns a
50-member channel into hundreds of queries.
"""

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import Activity, Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.tests.test_base import BaseAPITestCase


class MessagePostFanoutQueryTests(BaseAPITestCase):
    def _channel_with(self, members: int) -> Channel:
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title=f"GM {members}")
        ChannelMember.objects.create(channel=channel, user=self.user, role="owner")
        User = get_user_model()
        for i in range(members - 1):
            u = User.objects.create_user(
                username=f"m{members}_{i}", email=f"m{members}_{i}@example.com", password="pw123456"
            )
            TeamMembers.objects.create(team=self.team, attendee=u)
            ChannelMember.objects.create(channel=channel, user=u, role="member")
        return channel

    def _post(self, channel: Channel):
        self.authenticate(self.user)
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.post(
                f"/api/v3/channels/{channel.id}/messages/",
                {"body": [{"type": "paragraph", "content": "hi"}], "body_text": "hi"},
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.content)
        return res, len(ctx)

    def test_query_count_does_not_grow_with_channel_size(self):
        small_res, small = self._post(self._channel_with(3))
        big_res, big = self._post(self._channel_with(30))
        self.assertEqual(len(small_res.data["_v3_activities"]), 2)
        self.assertEqual(len(big_res.data["_v3_activities"]), 29)
        self.assertEqual(Activity.objects.filter(channel__title="GM 30").count(), 29)
        self.assertEqual(big, small, f"queries grew with channel size: {small} -> {big}")
