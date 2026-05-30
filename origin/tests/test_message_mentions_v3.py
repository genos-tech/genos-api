"""Mention handling on the v3 `message.send` create path.

`MessageMention.mentioned_user` and `Activity.recipient` are FK'd to the
user table. A body that @-mentions a non-existent user (stale client
cache / since-deleted account) or a non-member must NOT raise an FK
`IntegrityError` that 500s the whole send — the offending id is dropped.

Regression for bug #4 in the V3 migration audit.
"""

import uuid

from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageMention,
)
from origin.tests.test_base import BaseAPITestCase


class MessageMentionFilterTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Mentions GM",
            owner=self.user,
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        self.url = reverse("v3_messages_delta", args=[self.channel.id])

    def _body_mentioning(self, *user_ids):
        content = [{"type": "text", "text": "hi "}]
        for uid in user_ids:
            content.append({"type": "mention", "props": {"userId": str(uid), "userName": "X"}})
        return [{"type": "paragraph", "content": content}]

    def _send(self, body):
        return self.client.post(self.url, {"body": body, "body_text": "x"}, format="json")

    def test_mention_of_nonexistent_user_does_not_500(self):
        # A mention id that isn't a real user (and isn't a channel member).
        # Must be dropped; the send still succeeds.
        self.authenticate()
        resp = self._send(self._body_mentioning(uuid.uuid4()))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        msg = Message.objects.get(channel=self.channel)
        self.assertEqual(MessageMention.objects.filter(message=msg).count(), 0)

    def test_mention_of_non_member_is_dropped(self):
        # user2 exists but is NOT a member of this channel → mention dropped.
        self.authenticate()
        resp = self._send(self._body_mentioning(self.user2.id))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        msg = Message.objects.get(channel=self.channel)
        self.assertEqual(MessageMention.objects.filter(message=msg).count(), 0)

    def test_mention_of_member_persists(self):
        self.authenticate()
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        resp = self._send(self._body_mentioning(self.user2.id))
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        msg = Message.objects.get(channel=self.channel)
        self.assertEqual(
            MessageMention.objects.filter(message=msg, mentioned_user=self.user2).count(),
            1,
        )
