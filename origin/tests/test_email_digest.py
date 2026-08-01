"""Tests for the email digest (PR A6 of the email series): local-hour
targeting, the per-channel stamp + MIN_GAP idempotency, stamp-on-empty,
the coalescer-overlap dedupe (outbox `sent` rows as the ledger), the
opt-out/suppression gates, and locale rendering."""

from io import StringIO
from zoneinfo import ZoneInfo

from django.core import mail
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.inbox_models import InboxItems
from origin.models.common.notification_models import (
    EmailNotificationEvent,
    EmailSuppression,
    NotificationPreference,
)
from origin.services.email_suppression import suppress
from origin.services.v3_activity import SURFACE_TASK_BODY
from origin.tests.test_base import BaseAPITestCase


@override_settings(
    EMAIL_NOTIFICATIONS_ENABLED=True,
    FRONTEND_BASE_URL="https://app.genos.test",
    API_PUBLIC_BASE_URL="https://api.genos.test",
    DEFAULT_FROM_EMAIL="Genos <noreply@genos.test>",
)
class DigestTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # Pin the user to a known zone and always tick at their CURRENT
        # local hour, so the tests don't depend on wall-clock time.
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        self.local_hour = timezone.now().astimezone(ZoneInfo("Asia/Tokyo")).hour

    def _unread_mention(self, recipient=None, title_id="WRD-7"):
        return Activity.objects.create(
            team=self.team,
            recipient=recipient or self.user,
            actor=self.user2,
            activity_type=ActivityType.MENTION,
            surface_type=SURFACE_TASK_BODY,
            meta={"displayId": title_id, "projectId": "p1", "taskId": "t1"},
        )

    def _tick(self, at_hour=None, **kwargs):
        out = StringIO()
        call_command(
            "email_digest",
            at_hour=self.local_hour if at_hour is None else at_hour,
            stdout=out,
            **kwargs,
        )
        return out.getvalue()


class SelectionTests(DigestTestBase):
    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=False)
    def test_flag_off_is_a_noop(self):
        self._unread_mention()
        out = self._tick()
        self.assertIn("off", out)
        self.assertEqual(len(mail.outbox), 0)

    def test_sends_at_the_users_local_hour(self):
        self._unread_mention()
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, [self.user.email])
        self.assertIn("mentioned you in a task", msg.body)
        self.assertIn("https://app.genos.test/workspace/tasks/project/p1/task/t1", msg.body)
        self.assertIn("List-Unsubscribe", msg.extra_headers)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_digest_last_sent_at)
        # The AGENT digest's stamp is untouched — separate channels.
        self.assertIsNone(self.user.digest_last_sent_at)

    def test_wrong_local_hour_selects_nobody(self):
        self._unread_mention()
        self._tick(at_hour=(self.local_hour + 3) % 24)
        self.assertEqual(len(mail.outbox), 0)

    def test_min_gap_prevents_double_send(self):
        self._unread_mention()
        self._tick()
        self._unread_mention(title_id="WRD-8")
        self._tick()
        self.assertEqual(len(mail.outbox), 1)

    def test_empty_digest_stamps_without_sending(self):
        self._tick()
        self.assertEqual(len(mail.outbox), 0)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_digest_last_sent_at)

    def test_user_id_bypasses_the_hour_check(self):
        self._unread_mention()
        self._tick(at_hour=(self.local_hour + 3) % 24, user_id=str(self.user.id))
        self.assertEqual(len(mail.outbox), 1)

    def test_dry_run_mutates_nothing(self):
        self._unread_mention()
        out = self._tick(dry_run=True)
        self.assertIn("would send", out)
        self.assertEqual(len(mail.outbox), 0)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.email_digest_last_sent_at)


class GateTests(DigestTestBase):
    def test_digest_opt_out_is_not_selected(self):
        self.user.email_digest_enabled = False
        self.user.save(update_fields=["email_digest_enabled"])
        self._unread_mention()
        self._tick()
        self.assertEqual(len(mail.outbox), 0)

    def test_email_master_off_skips(self):
        NotificationPreference.objects.create(user=self.user, email_enabled=False)
        self._unread_mention()
        self._tick()
        self.assertEqual(len(mail.outbox), 0)

    def test_suppressed_address_skips(self):
        suppress(self.user.email, EmailSuppression.REASON_BOUNCE)
        self._unread_mention()
        self._tick()
        self.assertEqual(len(mail.outbox), 0)


class ContentTests(DigestTestBase):
    def test_coalesced_events_are_not_repeated(self):
        already_mailed = self._unread_mention(title_id="MAILED-1")
        EmailNotificationEvent.objects.create(
            user=self.user,
            category="mention_task",
            title="x",
            url="/x",
            activity=already_mailed,
            status=EmailNotificationEvent.STATUS_SENT,
            sent_at=timezone.now(),
        )
        self._unread_mention(title_id="FRESH-1")
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        # Both are mentions with identical titles, so count instead of
        # title matching: exactly ONE entry line.
        self.assertEqual(mail.outbox[0].body.count("Open"), 1)

    def test_only_coalesced_events_means_empty_digest(self):
        act = self._unread_mention()
        EmailNotificationEvent.objects.create(
            user=self.user,
            category="mention_task",
            title="x",
            url="/x",
            activity=act,
            status=EmailNotificationEvent.STATUS_SENT,
            sent_at=timezone.now(),
        )
        self._tick()
        self.assertEqual(len(mail.outbox), 0)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_digest_last_sent_at)

    def test_inbox_items_are_included(self):
        InboxItems.objects.create(
            team=self.team, sender=self.user2, receiver=self.user, item_type=1
        )
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("asked to join your team", mail.outbox[0].body)

    def test_overflow_shows_more_count(self):
        for i in range(12):
            self._unread_mention(title_id=f"T-{i}")
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("12", mail.outbox[0].subject)
        self.assertIn("and 2 more", mail.outbox[0].body)

    def test_ja_locale_renders_ja_digest(self):
        self.user.language = "ja"
        self.user.save(update_fields=["language"])
        self._unread_mention()
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Genosダイジェスト", mail.outbox[0].subject)
        self.assertIn("ダイジェストを停止", mail.outbox[0].body)
