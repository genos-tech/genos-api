"""Idempotency of the v3 `message.send` create path.

A reconnect flush re-emits `message.send` with the SAME correlation_id
after a lost/slow ack. The Flask `/v3` layer forwards it to
`POST /api/v3/channels/{id}/messages/`, so a duplicate POST carrying the
same correlation_id must:

  - NOT create a second Message row (every channel member would otherwise
    see the message twice), and
  - NOT re-fire mention / thread-reply activities (which broadcast to
    each recipient's `user:{id}` room — a reconnect must not re-ping).

Regression for bug #3 in the V3 migration audit.
"""

from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.tests.test_base import BaseAPITestCase


class MessageSendIdempotencyTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Idempotency Test GM",
            owner=self.user,
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        self.url = reverse("v3_messages_delta", args=[self.channel.id])

    def _payload(self, correlation_id):
        # Body @-mentions user2 so the FIRST send fans out a mention
        # Activity — the re-send must NOT produce another.
        return {
            "body": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "hi "},
                        {
                            "type": "mention",
                            "props": {"userId": str(self.user2.id), "userName": "U2"},
                        },
                    ],
                }
            ],
            "body_text": "hi @U2",
            "correlation_id": correlation_id,
        }

    def test_resend_same_correlation_id_dedups_to_one_row(self):
        self.authenticate()
        r1 = self.client.post(self.url, self._payload("corr-xyz"), format="json")
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        first_id = r1.data["id"]
        # First send fans out one mention activity (to user2).
        self.assertEqual(len(r1.data["_v3_activities"]), 1)

        # Re-emit with the SAME correlation_id (reconnect flush).
        r2 = self.client.post(self.url, self._payload("corr-xyz"), format="json")
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        # Same row returned, NOT a duplicate.
        self.assertEqual(str(r2.data["id"]), str(first_id))
        self.assertEqual(Message.objects.filter(channel=self.channel).count(), 1)
        # Crucially: no re-fired activities on the dedup path (guards the
        # double-notify regression).
        self.assertEqual(r2.data["_v3_activities"], [])

    def test_distinct_correlation_ids_create_distinct_rows(self):
        self.authenticate()
        r1 = self.client.post(self.url, self._payload("corr-a"), format="json")
        r2 = self.client.post(self.url, self._payload("corr-b"), format="json")
        self.assertNotEqual(str(r1.data["id"]), str(r2.data["id"]))
        self.assertEqual(Message.objects.filter(channel=self.channel).count(), 2)

    def test_missing_correlation_id_is_exempt_from_dedup(self):
        # REST callers without a correlation_id are exempt from dedup and
        # the partial unique constraint (NULLs aren't unique-constrained),
        # so two such sends create two distinct rows.
        self.authenticate()
        payload = self._payload("unused")
        del payload["correlation_id"]
        r1 = self.client.post(self.url, payload, format="json")
        r2 = self.client.post(self.url, payload, format="json")
        self.assertEqual(r1.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r2.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(str(r1.data["id"]), str(r2.data["id"]))
        self.assertEqual(Message.objects.filter(channel=self.channel).count(), 2)
