"""Deleting an account erases the person's read cursors (MIP-1550).

A `ReadCursor` is the person's own record of what they have read and when. The account is
soft-deleted, so the FK cascade never fires and the rows used to stay. Nothing shows them to
another person (the API reads a cursor only to count the OWNER's unread, and the socket service
broadcasts `read.advanced` to the reader's own room only), so this is erasure of personal data,
not a change to what anyone sees. Another person's cursors are never touched.
"""

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    ReadCursor,
)
from origin.services.account_deletion import delete_account
from origin.tests.test_base import BaseAPITestCase


class DeleteAccountErasesReadCursorsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.leaver = self.user2
        self.stayer = self.user
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Room", owner=self.stayer
        )
        for u in (self.leaver, self.stayer):
            ChannelMember.objects.create(channel=self.channel, user=u)
        self.root = Message.objects.create(channel=self.channel, sender=self.stayer, seq=1, body=[])
        self.last = Message.objects.create(channel=self.channel, sender=self.stayer, seq=2, body=[])
        for u in (self.leaver, self.stayer):
            ReadCursor.objects.create(user=u, channel=self.channel, last_read_message=self.last)
            ReadCursor.objects.create(
                user=u, channel=self.channel, thread_root=self.root, last_read_message=self.last
            )

    def test_the_main_and_thread_cursors_go(self):
        self.assertEqual(ReadCursor.objects.filter(user=self.leaver).count(), 2)
        delete_account(self.leaver)
        self.assertFalse(ReadCursor.objects.filter(user=self.leaver).exists())

    def test_another_persons_cursors_stay(self):
        delete_account(self.leaver)
        self.assertEqual(ReadCursor.objects.filter(user=self.stayer).count(), 2)
        self.assertEqual(
            ReadCursor.objects.get(user=self.stayer, thread_root=None).last_read_message_id,
            self.last.id,
        )

    def test_the_messages_the_cursor_pointed_at_stay(self):
        delete_account(self.leaver)
        self.assertTrue(Message.objects.filter(pk=self.last.pk).exists())
