"""After a takeover, part 2 (MIP-2290).

1. The password-reset answer lists webhooks made in the last 7 days (host and id only), since a
   reset ends keys and connections but not webhooks.
2. A wrong password on `confirm-password` leaves a security event, and the Nth failure in an hour
   leaves one alert event and emails the owner once.
3. The owner email for a new key or webhook is sent from a background step, not inside the
   request that carries the secret.
"""

from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.utils import timezone

from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.webhook_models import WebhookEndpoint
from origin.services import credential_alert, recent_auth
from origin.services.credential_revocation import recent_webhooks
from origin.tests.test_base import BaseAPITestCase

CONFIRM = "/api/v2/user/confirm-password/"


def _hook(team, user, url, age_days=0):
    hook = WebhookEndpoint.objects.create(
        team=team, url=url, events=["task.created"], created_by=user, secret_encrypted="x"
    )
    if age_days:
        WebhookEndpoint.objects.filter(pk=hook.pk).update(
            ts_created_at=timezone.now() - timedelta(days=age_days)
        )
    return hook


class TestRecentWebhooks(BaseAPITestCase):
    def test_lists_only_this_users_webhooks_from_the_last_seven_days_by_host(self):
        new = _hook(self.team, self.user, "https://evil.example.com/hook/secret-path-9")
        _hook(self.team, self.user, "https://old.example.com/h", age_days=8)
        _hook(self.team, self.user2, "https://theirs.example.com/h")
        rows = recent_webhooks(self.user)
        self.assertEqual(rows, [{"id": str(new.id), "host": "evil.example.com"}])
        self.assertNotIn("secret-path", str(rows))


class TestConfirmFailures(BaseAPITestCase):
    recent_auth_gate = True

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.authenticate()

    def fail(self):
        return self.client.post(CONFIRM, {"password": "wrong"}, format="json")

    def events(self, action):
        return SecurityEvent.objects.filter(action=action, actor_id=self.user.pk)

    def test_each_failure_is_recorded_without_the_password(self):
        self.assertEqual(self.fail().status_code, 403)
        rows = self.events(SecurityAction.RECENT_AUTH_FAILED)
        self.assertEqual(rows.count(), 1)
        self.assertNotIn("wrong", str(rows.first().metadata))

    def test_a_success_records_no_failure(self):
        self.client.post(CONFIRM, {"password": "testpass123"}, format="json")
        self.assertEqual(self.events(SecurityAction.RECENT_AUTH_FAILED).count(), 0)

    def test_the_threshold_failure_alerts_once_and_emails_the_owner_once(self):
        n = recent_auth.ALERT_AFTER_FAILURES
        for _ in range(n - 1):
            self.fail()
        self.assertEqual(self.events(SecurityAction.ALERT_RECENT_AUTH_GUESSING).count(), 0)
        with self.captureOnCommitCallbacks(execute=True):
            self.fail()
        self.assertEqual(self.events(SecurityAction.ALERT_RECENT_AUTH_GUESSING).count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.user.email, mail.outbox[0].to)


class TestBackgroundEmail(BaseAPITestCase):
    def test_notify_created_hands_the_send_to_a_background_step(self):
        spawned = []
        with (
            patch.object(credential_alert, "_spawn", side_effect=spawned.append),
            patch.object(credential_alert, "send_templated_email") as send,
        ):
            with self.captureOnCommitCallbacks(execute=True):
                credential_alert.notify_created(
                    self.user, kind=credential_alert.KIND_WEBHOOK, label="h.example.com"
                )
            self.assertEqual(len(spawned), 1)
            send.assert_not_called()  # nothing sent inside the request
            spawned[0]()
            send.assert_called_once()
