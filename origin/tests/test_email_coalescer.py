"""Tests for the email coalescer (PR A4 of the email series): away
gating, return-cancellation, read suppression, cooldown, batching into
one email, retry bookkeeping, locale fallback, dry-run, and the stale
SENDING sweep.

A locmem cache isolates presence from the running app's Redis (the
`test_webpush.py` pattern); `mail.outbox` captures sends. Rows are aged
by queryset-updating `ts_created_at` (it's auto_now_add)."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.notification_models import (
    EmailNotificationEvent,
    NotificationPreference,
)
from origin.services import presence
from origin.services.v3_activity import SURFACE_TASK_BODY
from origin.tests.test_base import BaseAPITestCase

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(
    CACHES=LOCMEM,
    EMAIL_NOTIFICATIONS_ENABLED=True,
    EMAIL_NOTIFY_AWAY_MINUTES=10,
    EMAIL_NOTIFY_COOLDOWN_MINUTES=30,
    FRONTEND_BASE_URL="https://app.genos.test",
    API_PUBLIC_BASE_URL="https://api.genos.test",
    DEFAULT_FROM_EMAIL="Genos <noreply@genos.test>",
)
class CoalescerTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _event(
        self,
        user=None,
        *,
        category="mention_chat",
        title="Alice mentioned you",
        body="hello",
        url="/workspace/chat/dm/c1",
        age_minutes=20,
        activity=None,
    ):
        row = EmailNotificationEvent.objects.create(
            user=user or self.user,
            category=category,
            title=title,
            body=body,
            url=url,
            actor_name="Alice",
            activity=activity,
        )
        if age_minutes:
            EmailNotificationEvent.objects.filter(id=row.id).update(
                ts_created_at=timezone.now() - timedelta(minutes=age_minutes)
            )
            row.refresh_from_db()
        return row

    def _tick(self, **kwargs):
        out = StringIO()
        call_command("email_notify_tick", stdout=out, **kwargs)
        return out.getvalue()

    def _row(self, row):
        row.refresh_from_db()
        return row


class FlagAndGatingTests(CoalescerTestBase):
    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=False)
    def test_flag_off_sends_nothing(self):
        self._event()
        out = self._tick()
        self.assertIn("off", out)
        self.assertEqual(len(mail.outbox), 0)

    def test_young_rows_wait(self):
        row = self._event(age_minutes=2)
        self._tick()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self._row(row).status, EmailNotificationEvent.STATUS_PENDING)

    def test_visible_tab_cancels(self):
        row = self._event()
        presence.mark_visible(self.user.id, "dev-1")
        self._tick()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self._row(row).status, EmailNotificationEvent.STATUS_SKIPPED)

    def test_read_source_is_dropped_unread_still_sends(self):
        read_act = Activity.objects.create(
            team=self.team,
            recipient=self.user,
            actor=self.user2,
            activity_type=ActivityType.MENTION,
            surface_type=SURFACE_TASK_BODY,
            meta={},
            is_read=True,
        )
        read_row = self._event(title="already-seen mention", activity=read_act)
        unread_row = self._event(title="fresh mention")
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("already-seen mention", mail.outbox[0].body)
        self.assertIn("fresh mention", mail.outbox[0].body)
        self.assertEqual(self._row(read_row).status, EmailNotificationEvent.STATUS_SKIPPED)
        self.assertEqual(self._row(unread_row).status, EmailNotificationEvent.STATUS_SENT)

    def test_pref_flip_between_enqueue_and_send_skips(self):
        row = self._event()
        NotificationPreference.objects.create(user=self.user, email_enabled=False)
        self._tick()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self._row(row).status, EmailNotificationEvent.STATUS_SKIPPED)


class BatchingTests(CoalescerTestBase):
    def test_one_email_batches_all_pending(self):
        self._event(title="Alice mentioned you")
        self._event(
            title="Bob replied in a thread", category="thread_replies", url="/workspace/chat/gm/c2"
        )
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, [self.user.email])
        self.assertIn("Alice mentioned you", msg.body)
        self.assertIn("Bob replied in a thread", msg.body)
        # Relative outbox URLs became absolute app links.
        self.assertIn("https://app.genos.test/workspace/chat/dm/c1", msg.body)
        # Subject leads with the first event and counts the rest.
        self.assertIn("(+1 more)", msg.subject)
        # RFC 8058 pair present.
        self.assertIn("List-Unsubscribe", msg.extra_headers)
        self.assertIn("List-Unsubscribe-Post", msg.extra_headers)

    def test_two_users_two_emails(self):
        self._event()
        self._event(user=self.user2, title="for user2")
        self._tick()
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual({m.to[0] for m in mail.outbox}, {self.user.email, self.user2.email})

    def test_cooldown_suppresses_second_email(self):
        self._event()
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        row2 = self._event(title="later event")
        self._tick()
        # Still just one email; the new row waits for the cooldown.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self._row(row2).status, EmailNotificationEvent.STATUS_PENDING)

    def test_ja_user_gets_ja_template(self):
        self.user.language = "ja"
        self.user.save(update_fields=["language"])
        self._event()
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("不在の間の通知", mail.outbox[0].body)

    def test_unknown_locale_falls_back_to_english(self):
        self.user.language = "fr"  # no fr templates shipped yet
        self.user.save(update_fields=["language"])
        self._event()
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("While you were away", mail.outbox[0].body)


class RetryAndSweepTests(CoalescerTestBase):
    def test_send_failure_repends_with_attempt_and_isolates_users(self):
        row = self._event()
        self._event(user=self.user2, title="for user2")

        real_send = mail.EmailMultiAlternatives.send

        def flaky_send(msg_self, *a, **kw):
            if self.user.email in msg_self.to:
                raise RuntimeError("resend down")
            return real_send(msg_self, *a, **kw)

        with mock.patch.object(mail.EmailMultiAlternatives, "send", flaky_send):
            self._tick()
        # user2's email still went out.
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user2.email])
        row = self._row(row)
        self.assertEqual(row.status, EmailNotificationEvent.STATUS_PENDING)
        self.assertEqual(row.attempts, 1)
        self.assertIsNone(row.sent_at)
        # Next tick retries and succeeds.
        self._tick()
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self._row(row).status, EmailNotificationEvent.STATUS_SENT)

    def test_stale_sending_rows_are_swept_and_resent(self):
        row = self._event()
        EmailNotificationEvent.objects.filter(id=row.id).update(
            status=EmailNotificationEvent.STATUS_SENDING,
            sent_at=timezone.now() - timedelta(minutes=20),
        )
        self._tick()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self._row(row).status, EmailNotificationEvent.STATUS_SENT)

    def test_fresh_sending_rows_are_left_alone(self):
        row = self._event()
        EmailNotificationEvent.objects.filter(id=row.id).update(
            status=EmailNotificationEvent.STATUS_SENDING, sent_at=timezone.now()
        )
        self._tick()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self._row(row).status, EmailNotificationEvent.STATUS_SENDING)


class DryRunTests(CoalescerTestBase):
    def test_dry_run_mutates_nothing(self):
        row = self._event()
        out = self._tick(dry_run=True)
        self.assertIn("would email", out)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self._row(row).status, EmailNotificationEvent.STATUS_PENDING)
