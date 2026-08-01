"""Tests for bounce/complaint suppression (PR A5 of the email series):
the tracking-signal receiver, the suppression service, the coalescer's
send-path check, and the transactional exemption."""

from datetime import timedelta
from types import SimpleNamespace

from anymail.signals import tracking
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from origin.models.common.notification_models import (
    EmailNotificationEvent,
    EmailSuppression,
    NotificationPreference,
)
from origin.services.email import send_templated_email
from origin.services.email_suppression import is_suppressed, suppress
from origin.tests.test_base import BaseAPITestCase

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _fire(event_type, recipient):
    tracking.send(
        sender=None,
        event=SimpleNamespace(event_type=event_type, recipient=recipient),
        esp_name="Resend",
    )


class SuppressionServiceTests(BaseAPITestCase):
    def test_suppress_is_idempotent_and_case_insensitive(self):
        suppress("User@Example.com", EmailSuppression.REASON_BOUNCE)
        suppress("user@example.com", EmailSuppression.REASON_BOUNCE)
        self.assertEqual(EmailSuppression.objects.count(), 1)
        self.assertTrue(is_suppressed("USER@example.COM"))

    def test_complaint_upgrades_bounce_never_downgrades(self):
        suppress("a@example.com", EmailSuppression.REASON_BOUNCE)
        suppress("a@example.com", EmailSuppression.REASON_COMPLAINT)
        self.assertEqual(
            EmailSuppression.objects.get(address="a@example.com").reason,
            EmailSuppression.REASON_COMPLAINT,
        )
        suppress("a@example.com", EmailSuppression.REASON_BOUNCE)
        self.assertEqual(
            EmailSuppression.objects.get(address="a@example.com").reason,
            EmailSuppression.REASON_COMPLAINT,
        )

    def test_blank_address_is_treated_as_suppressed_but_never_stored(self):
        self.assertTrue(is_suppressed(""))
        suppress("", EmailSuppression.REASON_BOUNCE)
        self.assertEqual(EmailSuppression.objects.count(), 0)


class TrackingReceiverTests(BaseAPITestCase):
    def test_bounce_suppresses_but_keeps_prefs(self):
        _fire("bounced", self.user.email)
        self.assertTrue(is_suppressed(self.user.email))
        # A bounce is a transport fact, not a user choice — prefs stay.
        self.assertFalse(NotificationPreference.objects.filter(user=self.user).exists())

    def test_complaint_suppresses_and_disables_email_pref(self):
        _fire("complained", self.user.email.upper())
        self.assertTrue(is_suppressed(self.user.email))
        prefs = NotificationPreference.objects.get(user=self.user)
        self.assertFalse(prefs.email_enabled)
        # Only the email channel — the complaint was about mail.
        self.assertTrue(prefs.push_enabled)
        self.assertTrue(prefs.master_enabled)

    def test_irrelevant_events_are_ignored(self):
        _fire("delivered", self.user.email)
        _fire("opened", self.user.email)
        self.assertEqual(EmailSuppression.objects.count(), 0)


@override_settings(
    CACHES=LOCMEM,
    EMAIL_NOTIFICATIONS_ENABLED=True,
    EMAIL_NOTIFY_AWAY_MINUTES=10,
    EMAIL_NOTIFY_COOLDOWN_MINUTES=30,
    FRONTEND_BASE_URL="https://app.genos.test",
    DEFAULT_FROM_EMAIL="Genos <noreply@genos.test>",
)
class CoalescerSuppressionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _old_event(self, user):
        row = EmailNotificationEvent.objects.create(
            user=user, category="mention_chat", title="hi", body="", url="/workspace/inbox"
        )
        EmailNotificationEvent.objects.filter(id=row.id).update(
            ts_created_at=timezone.now() - timedelta(minutes=20)
        )
        return row

    def test_suppressed_address_is_skipped_others_still_send(self):
        row = self._old_event(self.user)
        self._old_event(self.user2)
        suppress(self.user.email, EmailSuppression.REASON_BOUNCE)
        call_command("email_notify_tick")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user2.email])
        row.refresh_from_db()
        self.assertEqual(row.status, EmailNotificationEvent.STATUS_SKIPPED)

    def test_transactional_send_ignores_suppression(self):
        # A password reset must reach even a suppressed address — the
        # suppression list applies to the NOTIFICATION channel only.
        suppress("recipient@example.com", EmailSuppression.REASON_BOUNCE)
        send_templated_email(
            to="recipient@example.com",
            subject="Reset your password",
            template_base="password_reset",
            context={
                "user_name": "Alice",
                "reset_url": "https://app/reset?t=xyz",
                "expiry_minutes": 30,
            },
        )
        self.assertEqual(len(mail.outbox), 1)
