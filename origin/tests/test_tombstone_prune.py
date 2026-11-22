"""MIP-1937: purge the content-less tombstone rows of deleted messages once they are older than
the 60-day sync window (`MAX_DELTA_AGE_DAYS`: a client older than that reloads in full, so it can
never need the tombstone) and nothing needs the row any more.

What keeps a row: it still says something; it still has files; a live reply, or a reply deleted
inside the window, hangs off it; it is the chat's highest `seq` (new messages take `max + 1`, so
removing it would hand its number out again); a person's read cursor points at it. Dry run by
default; counts only.
"""

from __future__ import annotations

import io
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageAttachment,
    ReadCursor,
)
from origin.models.common.notification_models import EmailNotificationEvent
from origin.services import tombstone_prune as svc
from origin.tests.test_base import BaseAPITestCase


class Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="G", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        self.seq = 0
        # the chat's newest message: a live row, so the tombstones below are not the top `seq`
        self.top = self.make(live=True, days=0)

    def make(self, *, live=False, days=90, text="", **extra):
        self.seq += 1
        msg = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=self.seq,
            body=[],
            body_text=text,
            **extra,
        )
        if not live:
            when = timezone.now() - timedelta(days=days)
            Message.objects.filter(pk=msg.pk).update(deleted_at=when, ts_updated_at=when)
        top = getattr(self, "top", None)
        if top is not None:  # keep `self.top` the chat's highest seq
            Message.objects.filter(pk=top.pk).update(seq=self.seq + 1000)
        return msg

    def exists(self, msg):
        return Message.objects.filter(pk=msg.pk).exists()

    def run_apply(self):
        return svc.prune(apply=True)


class WhichRowsGo(Base):
    def test_an_old_empty_tombstone_is_purged(self):
        old = self.make()
        res = self.run_apply()
        self.assertFalse(self.exists(old))
        self.assertEqual(res.changed, 1)

    def test_a_dry_run_changes_nothing_and_counts(self):
        old = self.make()
        res = svc.prune(apply=False)
        self.assertTrue(self.exists(old))
        self.assertEqual(res.matched, 1)
        self.assertEqual(res.changed, 0)

    def test_a_tombstone_inside_the_sync_window_stays(self):
        new = self.make(days=10)
        self.run_apply()
        self.assertTrue(self.exists(new))

    def test_a_live_message_stays(self):
        live = self.make(live=True)
        self.run_apply()
        self.assertTrue(self.exists(live))

    def test_a_row_that_still_has_text_stays(self):
        texty = self.make(text="still here")
        self.run_apply()
        self.assertTrue(self.exists(texty))

    def test_a_row_with_attachments_stays(self):
        withfile = self.make()
        MessageAttachment.objects.create(
            message=withfile,
            uploader=self.user,
            file=SimpleUploadedFile("a.txt", b"x"),
            size_bytes=1,
        )
        self.run_apply()
        self.assertTrue(self.exists(withfile))

    def test_the_chats_highest_seq_stays_even_when_deleted(self):
        Message.objects.filter(pk=self.top.pk).update(
            deleted_at=timezone.now() - timedelta(days=90),
            ts_updated_at=timezone.now() - timedelta(days=90),
        )
        Message.objects.filter(pk=self.top.pk).update(seq=5000)
        self.run_apply()
        self.assertTrue(self.exists(self.top))

    def test_a_root_with_a_live_reply_stays(self):
        root = self.make()
        self.make(live=True, parent=root, thread_root=root, is_thread_reply=True)
        self.run_apply()
        self.assertTrue(self.exists(root))

    def test_a_root_with_a_reply_deleted_inside_the_window_stays(self):
        root = self.make()
        self.make(days=5, parent=root, thread_root=root, is_thread_reply=True)
        self.run_apply()
        self.assertTrue(self.exists(root))

    def test_a_root_whose_replies_are_all_old_tombstones_goes_with_them(self):
        root = self.make()
        reply = self.make(parent=root, thread_root=root, is_thread_reply=True)
        self.run_apply()
        self.assertFalse(self.exists(root))
        self.assertFalse(self.exists(reply))

    def test_a_row_a_read_cursor_points_at_stays(self):
        pointed = self.make()
        ReadCursor.objects.create(user=self.user, channel=self.channel, last_read_message=pointed)
        self.run_apply()
        self.assertTrue(self.exists(pointed))
        cursor = ReadCursor.objects.get(user=self.user, channel=self.channel)
        self.assertEqual(cursor.last_read_message_id, pointed.id)


class WhatGoesWithTheRow(Base):
    def test_its_activity_rows_and_pending_emails_go_with_it(self):
        old = self.make()
        activity = Activity.objects.create(
            team=self.team, recipient=self.user, message=old, activity_type=ActivityType.MENTION
        )
        mail = EmailNotificationEvent.objects.create(
            user=self.user, category="mention", title="t", body="x", activity=activity
        )
        self.run_apply()
        self.assertFalse(Activity.objects.filter(pk=activity.pk).exists())
        self.assertFalse(EmailNotificationEvent.objects.filter(pk=mail.pk).exists())

    def test_the_batch_limit_is_respected(self):
        for _ in range(5):
            self.make()
        res = svc.prune(apply=True, limit=2)
        self.assertEqual(res.changed, 2)
        self.assertEqual(svc.prune(apply=False).matched, 3)


class Command(Base):
    def call(self, *args):
        out = io.StringIO()
        call_command("prune_old_tombstones", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_by_default_prints_counts_only(self):
        old = self.make()
        out = self.call()
        self.assertIn("DRY RUN", out)
        self.assertIn("1", out)
        self.assertTrue(self.exists(old))

    def test_apply_removes_them(self):
        old = self.make()
        out = self.call("--apply")
        self.assertIn("APPLY", out)
        self.assertFalse(self.exists(old))
