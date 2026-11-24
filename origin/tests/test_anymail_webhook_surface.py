"""MIP-2298: only the Resend tracking webhook is mounted, and it verifies the Svix signature.

The red-team found `include("anymail.urls")` mounted ~40 webhooks for ESPs Genos does not use.
They authenticate with HTTP basic auth against `ANYMAIL["WEBHOOK_SECRET"]`, which Genos never
sets, so an anonymous POST to e.g. the Postmark tracking route forged a bounce/complaint and
suppressed any address.
"""

import base64
import json
import time
from datetime import datetime
from types import SimpleNamespace

from anymail.signals import tracking
from django.test import override_settings
from django.urls import get_resolver
from svix.webhooks import Webhook

from origin.models.common.notification_models import EmailSuppression, NotificationPreference
from origin.services import blind_index
from origin.services.email_suppression import is_suppressed
from origin.tests.test_base import BaseAPITestCase

VICTIM = "test@example.com"  # BaseAPITestCase.user
# A Svix signing secret is "whsec_" + base64. Built at runtime so no secret-shaped literal is committed.


def _svix_secret(seed):
    return "whsec_" + base64.b64encode(seed.encode().ljust(24, b"."))[:32].decode()


SECRET = _svix_secret("resend-webhook-test")
OTHER_SECRET = _svix_secret("some-other-signer")
RESEND_URL = "/api/v2/email/anymail/resend/tracking/"
POSTMARK_URL = "/api/v2/email/anymail/postmark/tracking/"


def _suppression_count():
    return EmailSuppression.objects.count()


def _resend_body(event_type, to):
    return json.dumps(
        {
            "type": event_type,
            "created_at": "2026-11-24T03:00:00.000Z",
            "data": {"email_id": "e-1", "to": [to], "created_at": "2026-11-24T02:59:00.000Z"},
        }
    )


def _signed_headers(body, secret=SECRET):
    msg_id = "msg_test_1"
    ts = str(int(time.time()))
    signature = Webhook(secret).sign(msg_id, datetime.fromtimestamp(int(ts)), body)
    return {
        "HTTP_SVIX_ID": msg_id,
        "HTTP_SVIX_TIMESTAMP": ts,
        "HTTP_SVIX_SIGNATURE": signature,
    }


class AnymailRoutesAreResendOnlyTests(BaseAPITestCase):
    def test_only_the_resend_tracking_route_is_mounted(self):
        patterns = []

        def walk(resolver, prefix=""):
            for entry in resolver.url_patterns:
                text = prefix + str(entry.pattern)
                if hasattr(entry, "url_patterns"):
                    walk(entry, text)
                else:
                    patterns.append(text)

        walk(get_resolver())
        anymail_routes = [p for p in patterns if "anymail" in p]
        self.assertEqual(anymail_routes, ["api/v2/email/anymail/resend/tracking/"])

    def test_forged_postmark_complaint_is_404_and_changes_nothing(self):
        payload = {
            "RecordType": "SpamComplaint",
            "Email": VICTIM,
            "MessageID": "00000000-0000-0000-0000-000000000001",
            "BouncedAt": "2026-11-24T03:00:00Z",
            "Type": "SpamComplaint",
            "TypeCode": 512,
            "ID": 1,
        }
        response = self.client.post(POSTMARK_URL, payload, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(_suppression_count(), 0)
        self.assertFalse(is_suppressed(VICTIM))
        self.assertFalse(
            NotificationPreference.objects.filter(user=self.user, email_enabled=False).exists()
        )

    def test_every_other_esp_path_is_404(self):
        for esp in (
            "mailgun",
            "sendgrid",
            "brevo",
            "sendinblue",
            "mailjet",
            "mailtrap",
            "amazon_ses",
        ):
            for kind in ("tracking", "inbound"):
                response = self.client.post(
                    f"/api/v2/email/anymail/{esp}/{kind}/", {"event": "bounced"}, format="json"
                )
                self.assertEqual(response.status_code, 404, f"{esp}/{kind}")
        self.assertEqual(_suppression_count(), 0)


class ResendWebhookSignatureTests(BaseAPITestCase):
    def _post(self, body, headers):
        return self.client.post(RESEND_URL, data=body, content_type="application/json", **headers)

    @override_settings(ANYMAIL={"RESEND_SIGNING_SECRET": SECRET, "RESEND_API_KEY": ""})
    def test_valid_signature_suppresses(self):
        body = _resend_body("email.bounced", VICTIM)
        response = self._post(body, _signed_headers(body))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(is_suppressed(VICTIM))

    @override_settings(ANYMAIL={"RESEND_SIGNING_SECRET": SECRET, "RESEND_API_KEY": ""})
    def test_unsigned_and_wrongly_signed_posts_suppress_nothing(self):
        body = _resend_body("email.complained", VICTIM)
        unsigned = self._post(body, {})
        self.assertGreaterEqual(unsigned.status_code, 400)
        forged = self._post(body, _signed_headers(body, secret=OTHER_SECRET))
        self.assertGreaterEqual(forged.status_code, 400)
        self.assertEqual(_suppression_count(), 0)

    @override_settings(ANYMAIL={"RESEND_SIGNING_SECRET": "", "RESEND_API_KEY": ""})
    def test_unset_secret_fails_closed(self):
        # anymail cannot build its Svix check from an empty secret and the view errors (a loud 500
        # for a misconfigured deploy), never an unverified 200.
        self.client.raise_request_exception = False
        body = _resend_body("email.bounced", VICTIM)
        response = self._post(body, {})
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(_suppression_count(), 0)


class SuppressionReceiverIgnoresOtherEspsTests(BaseAPITestCase):
    def test_event_from_another_esp_suppresses_nothing(self):
        tracking.send(
            sender=None,
            event=SimpleNamespace(event_type="complained", recipient=VICTIM),
            esp_name="Postmark",
        )
        self.assertEqual(_suppression_count(), 0)
        self.assertFalse(
            EmailSuppression.objects.filter(blind_index.SUPPRESSED_ADDRESS.matches(VICTIM)).exists()
        )

    def test_resend_event_still_suppresses(self):
        tracking.send(
            sender=None,
            event=SimpleNamespace(event_type="bounced", recipient=VICTIM),
            esp_name="Resend",
        )
        self.assertTrue(is_suppressed(VICTIM))
