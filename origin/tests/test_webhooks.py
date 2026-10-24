"""Outbound webhooks: the SSRF guard, the signature, and the outbox.

The SSRF tests carry the most weight. Customer-supplied URLs are the
FIRST user-controlled outbound destination in this codebase — every
other outbound call targets a host we chose — so there was no guard to
reuse and no existing test to inherit. The API sits in a private network
with reachable neighbours (Postgres, Redis, OpenSearch, the sockets and
collab services, and on a cloud host the metadata endpoint that hands out
credentials), which is exactly what an unguarded webhook turns into a
proxy for.
"""

import logging
from datetime import timedelta
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone

from origin.models.common.team_models import TeamMembers
from origin.models.common.webhook_models import (
    EVENT_TASK_CREATED,
    MAX_CONSECUTIVE_FAILURES,
    WebhookDelivery,
    WebhookEndpoint,
    generate_secret,
)
from origin.services.member_roles import EDITOR
from origin.services.webhook_delivery import (
    WebhookUrlError,
    backoff_for,
    sign,
    validate_webhook_url,
)
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

WEBHOOKS = "/api/v2/webhooks/"


class TestSsrfGuard(BaseAPITestCase):
    """Each rejected address is one a request from inside the network
    could otherwise reach."""

    def _public(self):
        # Pin resolution so the suite never depends on real DNS.
        return patch(
            "origin.services.outbound_url.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        )

    def _resolves_to(self, ip):
        return patch(
            "origin.services.outbound_url.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", (ip, 443))],
        )

    def test_a_public_https_url_is_accepted(self):
        with self._public():
            self.assertTrue(validate_webhook_url("https://example.com/hook"))

    def test_http_is_refused(self):
        with self._public():
            with self.assertRaises(WebhookUrlError):
                validate_webhook_url("http://example.com/hook")

    def test_loopback_is_refused(self):
        with self._resolves_to("127.0.0.1"):
            with self.assertRaises(WebhookUrlError):
                validate_webhook_url("https://localhost/hook")

    def test_private_ranges_are_refused(self):
        for ip in ("10.0.0.5", "192.168.1.10", "172.16.0.3"):
            with self.subTest(ip=ip), self._resolves_to(ip):
                with self.assertRaises(WebhookUrlError):
                    validate_webhook_url("https://internal.example/hook")

    def test_cloud_metadata_is_refused(self):
        """169.254.169.254 hands out instance credentials."""
        with self._resolves_to("169.254.169.254"):
            with self.assertRaises(WebhookUrlError):
                validate_webhook_url("https://metadata.example/hook")

    def test_ranges_the_old_check_let_through_are_refused(self):
        """Not `is_private`, so the original list passed them (MIP-717):
        shared / CGNAT space, and deprecated IPv6 site-local."""
        for ip in ("100.64.0.1", "100.127.255.254", "fec0::1"):
            with self.subTest(ip=ip), self._resolves_to(ip):
                with self.assertRaises(WebhookUrlError):
                    validate_webhook_url("https://internal.example/hook")

    def test_a_host_with_any_private_record_is_refused(self):
        """ALL resolved addresses must be public — a mixed record would
        otherwise pass and then be sent to the private one."""
        with patch(
            "origin.services.outbound_url.socket.getaddrinfo",
            return_value=[
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("10.0.0.5", 443)),
            ],
        ):
            with self.assertRaises(WebhookUrlError):
                validate_webhook_url("https://mixed.example/hook")

    def test_unresolvable_is_refused(self):
        import socket as real_socket

        with patch(
            "origin.services.outbound_url.socket.getaddrinfo",
            side_effect=real_socket.gaierror(),
        ):
            with self.assertRaises(WebhookUrlError):
                validate_webhook_url("https://nope.invalid/hook")

    def test_credentials_in_the_url_are_refused(self):
        with self._public():
            with self.assertRaises(WebhookUrlError):
                validate_webhook_url("https://user:pass@example.com/hook")


class TestSignature(BaseAPITestCase):
    def test_the_timestamp_is_inside_the_signed_string(self):
        """The inbound GitHub verifier signs the body alone, so a
        captured payload replays forever. Not exporting that."""
        body = b'{"a":1}'
        self.assertNotEqual(sign("s", "111", body), sign("s", "222", body))

    def test_the_signature_depends_on_the_secret_and_the_body(self):
        self.assertNotEqual(sign("s1", "1", b"x"), sign("s2", "1", b"x"))
        self.assertNotEqual(sign("s", "1", b"x"), sign("s", "1", b"y"))

    def test_the_format_matches_the_inbound_convention(self):
        self.assertTrue(sign("s", "1", b"x").startswith("sha256="))


class TestBackoff(BaseAPITestCase):
    def test_it_grows_and_is_capped(self):
        first = backoff_for(1)
        later = backoff_for(4)
        self.assertGreater(later, first)
        self.assertLessEqual(backoff_for(50), timedelta(minutes=60))


class WebhookApiBase(BaseAPITestCase):
    def _public_dns(self):
        return patch(
            "origin.services.outbound_url.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
        )

    def _endpoint(self, **overrides):
        e = WebhookEndpoint(
            team=self.team,
            url=overrides.pop("url", "https://example.com/hook"),
            events=overrides.pop("events", [EVENT_TASK_CREATED]),
            **overrides,
        )
        e.set_secret(generate_secret())
        e.save()
        return e


class TestWebhookManagement(WebhookApiBase):
    def test_creating_returns_the_secret_once(self):
        self.authenticate(self.user)
        with self._public_dns():
            res = self.client.post(
                WEBHOOKS,
                {
                    "team_id": str(self.team.team_id),
                    "url": "https://example.com/hook",
                    "events": [EVENT_TASK_CREATED],
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.data["secret"].startswith("whsec_"))
        listed = self.client.get(WEBHOOKS, {"team_id": str(self.team.team_id)})
        self.assertNotIn("secret", listed.data["webhooks"][0])

    def test_the_secret_is_recoverable_for_signing_but_not_stored_plainly(self):
        """Unlike an API key: we must USE this value, not just verify it."""
        raw = generate_secret()
        e = WebhookEndpoint(team=self.team, url="https://example.com/h", events=[])
        e.set_secret(raw)
        e.save()
        e.refresh_from_db()
        self.assertNotEqual(e.secret_encrypted, raw)
        self.assertEqual(e.secret, raw)

    def test_a_private_url_is_refused_by_the_endpoint(self):
        self.authenticate(self.user)
        with patch(
            "origin.services.outbound_url.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ):
            res = self.client.post(
                WEBHOOKS,
                {
                    "team_id": str(self.team.team_id),
                    "url": "https://localhost/hook",
                    "events": [EVENT_TASK_CREATED],
                },
                format="json",
            )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(WebhookEndpoint.objects.exists())

    def test_unknown_events_are_refused(self):
        self.authenticate(self.user)
        with self._public_dns():
            res = self.client.post(
                WEBHOOKS,
                {
                    "team_id": str(self.team.team_id),
                    "url": "https://example.com/hook",
                    "events": ["task.exploded"],
                },
                format="json",
            )
        self.assertEqual(res.status_code, 400)

    def test_a_viewer_cannot_create_one(self):
        """A webhook is a standing instruction to send this team's data
        to an address somebody chose."""
        self.authenticate(self.user2)
        with self._public_dns():
            res = self.client.post(
                WEBHOOKS,
                {
                    "team_id": str(self.team.team_id),
                    "url": "https://example.com/hook",
                    "events": [EVENT_TASK_CREATED],
                },
                format="json",
            )
        self.assertEqual(res.status_code, 403)

    def test_an_editor_can(self):
        row = TeamMembers.objects.get(team=self.team, attendee=self.user2)
        row.member_role = EDITOR
        row.save(update_fields=["member_role"])
        self.authenticate(self.user2)
        with self._public_dns():
            res = self.client.post(
                WEBHOOKS,
                {
                    "team_id": str(self.team.team_id),
                    "url": "https://example.com/hook",
                    "events": [EVENT_TASK_CREATED],
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201)

    def test_a_non_member_gets_404(self):
        outsider = User.objects.create_user(
            username="whout", email="whout@example.com", password="pw"
        )
        self.authenticate(outsider)
        res = self.client.get(WEBHOOKS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 404)


class TestDeliveryTick(WebhookApiBase):
    def _queue(self, endpoint, **overrides):
        return WebhookDelivery.objects.create(
            endpoint=endpoint, event=EVENT_TASK_CREATED, payload={"id": 1}, **overrides
        )

    def _run(self):
        from django.core.management import call_command

        call_command("webhook_deliver_tick")

    def test_a_successful_delivery_is_marked_sent(self):
        e = self._endpoint()
        d = self._queue(e)
        with patch(
            "origin.management.commands.webhook_deliver_tick.post_delivery",
            return_value=(200, ""),
        ):
            self._run()
        d.refresh_from_db()
        self.assertEqual(d.status, WebhookDelivery.STATUS_SENT)
        self.assertIsNotNone(d.sent_at)

    def test_a_failure_is_retried_with_backoff_not_immediately(self):
        e = self._endpoint()
        d = self._queue(e)
        with patch(
            "origin.management.commands.webhook_deliver_tick.post_delivery",
            return_value=(500, "HTTP 500"),
        ):
            self._run()
        d.refresh_from_db()
        self.assertEqual(d.status, WebhookDelivery.STATUS_PENDING)
        self.assertEqual(d.attempts, 1)
        self.assertGreater(d.next_attempt_at, timezone.now())

    def test_a_delivery_not_yet_due_is_left_alone(self):
        e = self._endpoint()
        d = self._queue(e, next_attempt_at=timezone.now() + timedelta(minutes=30), attempts=1)
        with patch(
            "origin.management.commands.webhook_deliver_tick.post_delivery",
            return_value=(200, ""),
        ) as sender:
            self._run()
        sender.assert_not_called()
        d.refresh_from_db()
        self.assertEqual(d.status, WebhookDelivery.STATUS_PENDING)

    def test_it_gives_up_after_max_attempts(self):
        e = self._endpoint()
        d = self._queue(e, attempts=4)
        with patch(
            "origin.management.commands.webhook_deliver_tick.post_delivery",
            return_value=(500, "HTTP 500"),
        ):
            self._run()
        d.refresh_from_db()
        self.assertEqual(d.status, WebhookDelivery.STATUS_FAILED)

    def test_a_repeatedly_failing_endpoint_is_disabled(self):
        e = self._endpoint(consecutive_failures=MAX_CONSECUTIVE_FAILURES - 1)
        self._queue(e)
        with patch(
            "origin.management.commands.webhook_deliver_tick.post_delivery",
            return_value=(500, "HTTP 500"),
        ):
            self._run()
        e.refresh_from_db()
        self.assertFalse(e.is_active)
        self.assertIsNotNone(e.disabled_at)

    def test_success_resets_the_failure_counter(self):
        e = self._endpoint(consecutive_failures=3)
        self._queue(e)
        with patch(
            "origin.management.commands.webhook_deliver_tick.post_delivery",
            return_value=(200, ""),
        ):
            self._run()
        e.refresh_from_db()
        self.assertEqual(e.consecutive_failures, 0)

    def test_a_stale_claim_is_revived(self):
        """A row orphaned by a crashed pass must rejoin, not sit forever."""
        e = self._endpoint()
        d = self._queue(
            e,
            status=WebhookDelivery.STATUS_SENDING,
            claimed_at=timezone.now() - timedelta(hours=1),
        )
        with patch(
            "origin.management.commands.webhook_deliver_tick.post_delivery",
            return_value=(200, ""),
        ):
            self._run()
        d.refresh_from_db()
        self.assertEqual(d.status, WebhookDelivery.STATUS_SENT)

    def test_an_inactive_endpoint_drops_the_delivery(self):
        e = self._endpoint(is_active=False)
        d = self._queue(e)
        with patch(
            "origin.management.commands.webhook_deliver_tick.post_delivery",
            return_value=(200, ""),
        ) as sender:
            self._run()
        sender.assert_not_called()
        d.refresh_from_db()
        self.assertEqual(d.status, WebhookDelivery.STATUS_FAILED)

    def test_a_customer_failure_does_not_red_the_cron(self):
        """CronCommand fails the run on any ERROR log. A flaky customer
        endpoint must not do that, or every integration outage becomes
        our alert."""
        e = self._endpoint()
        self._queue(e)
        with patch(
            "origin.management.commands.webhook_deliver_tick.post_delivery",
            return_value=(500, "HTTP 500"),
        ):
            self._run()  # would raise CommandError if anything logged ERROR


class TestUndecryptableEndpointSecret(WebhookApiBase):
    """One endpoint whose secret no key opens must not take the rest down.

    Reached by dropping a key from `FIELD_ENCRYPTION_KEYS` before
    `rotate_field_keys` has run, by restoring a backup taken under a key
    that is gone, or by one corrupted value. The whole keyring being wrong
    is a different case: nothing is deliverable, so failing every run is
    right. Here the keyring is fine and exactly one row is not.
    """

    SENDER = "origin.management.commands.webhook_deliver_tick.post_delivery"

    def _bad_endpoint(self, url="https://bad.example.com/hook"):
        e = self._endpoint(url=url)
        # A valid Fernet token under a key that is not in the ring.
        WebhookEndpoint.objects.filter(pk=e.pk).update(
            secret_encrypted=Fernet(Fernet.generate_key()).encrypt(b"x").decode()
        )
        e.refresh_from_db()
        return e

    def _queue(self, endpoint, **overrides):
        return WebhookDelivery.objects.create(
            endpoint=endpoint, event=EVENT_TASK_CREATED, payload={"id": 1}, **overrides
        )

    def _run(self):
        from django.core.management import call_command

        call_command("webhook_deliver_tick")

    def _run_expecting_red(self):
        """Our key problem is an ERROR, so the run ends red (CronCommand)."""
        with self.assertRaises(CommandError):
            self._run()

    def test_the_bad_row_does_not_stop_other_endpoints(self):
        bad = self._bad_endpoint()
        self._queue(bad)  # created first, so it sorts first
        good = self._endpoint(url="https://good.example.com/hook")
        delivered = self._queue(good)
        with patch(self.SENDER, return_value=(200, "")) as sender:
            self._run_expecting_red()
        delivered.refresh_from_db()
        self.assertEqual(delivered.status, WebhookDelivery.STATUS_SENT)
        # Only the good endpoint was ever signed for.
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(sender.call_args.kwargs["url"], good.url)

    def test_the_bad_row_is_released_not_left_sending(self):
        bad = self._bad_endpoint()
        row = self._queue(bad)
        with patch(self.SENDER, return_value=(200, "")):
            self._run_expecting_red()
        row.refresh_from_db()
        self.assertEqual(row.status, WebhookDelivery.STATUS_PENDING)
        self.assertIsNone(row.claimed_at)

    def test_it_does_not_use_up_an_attempt_or_fail_the_row(self):
        """Our key mistake is not the receiver's fault: no retries burned,
        so fixing the key later still delivers it."""
        bad = self._bad_endpoint()
        row = self._queue(bad, attempts=4)  # one failure from FAILED
        with patch(self.SENDER, return_value=(200, "")):
            self._run_expecting_red()
        row.refresh_from_db()
        self.assertEqual(row.attempts, 4)
        self.assertNotEqual(row.status, WebhookDelivery.STATUS_FAILED)

    def test_it_does_not_count_against_the_endpoints_failures(self):
        """Ten of these would otherwise auto-disable a customer's endpoint
        over a mistake that is ours."""
        bad = self._bad_endpoint()
        for _ in range(MAX_CONSECUTIVE_FAILURES + 1):
            self._queue(bad)
        with patch(self.SENDER, return_value=(200, "")):
            self._run_expecting_red()
        bad.refresh_from_db()
        self.assertEqual(bad.consecutive_failures, 0)
        self.assertTrue(bad.is_active)

    def test_it_waits_instead_of_spinning_every_pass(self):
        bad = self._bad_endpoint()
        row = self._queue(bad)
        with patch(self.SENDER, return_value=(200, "")):
            self._run_expecting_red()
        row.refresh_from_db()
        self.assertGreater(row.next_attempt_at, timezone.now())
        # A second pass inside the hold leaves it alone and logs nothing.
        with patch(self.SENDER, return_value=(200, "")) as sender:
            self._run()  # no ERROR now, so this must not raise
        sender.assert_not_called()

    def test_it_logs_at_error_once_per_endpoint_with_its_id_and_no_secret(self):
        bad = self._bad_endpoint()
        for _ in range(5):
            self._queue(bad)
        # A plain handler, not `assertLogs`: that one sets propagate=False,
        # which would hide the record from CronCommand's tripwire too.
        records: list[logging.LogRecord] = []
        handler = logging.Handler(level=logging.ERROR)
        handler.emit = records.append
        logger = logging.getLogger("origin.webhooks")
        logger.addHandler(handler)
        try:
            with patch(self.SENDER, return_value=(200, "")):
                self._run_expecting_red()
        finally:
            logger.removeHandler(handler)
        self.assertEqual(len(records), 1)
        message = records[0].getMessage()
        self.assertIn(str(bad.id), message)
        self.assertNotIn(bad.secret_encrypted, message)

    def test_a_fixed_key_delivers_it_afterwards(self):
        """The point of not spending the attempt: nothing is lost."""
        bad = self._bad_endpoint()
        row = self._queue(bad)
        with patch(self.SENDER, return_value=(200, "")):
            self._run_expecting_red()
        bad.set_secret(generate_secret())  # the operator fixes the key
        bad.save()
        WebhookDelivery.objects.filter(pk=row.pk).update(next_attempt_at=None)
        with patch(self.SENDER, return_value=(200, "")):
            self._run()
        row.refresh_from_db()
        self.assertEqual(row.status, WebhookDelivery.STATUS_SENT)

    def test_a_missing_keyring_still_fails_the_run_at_start(self):
        """The whole ring being unset is the other case and stays loud."""
        self._queue(self._endpoint())
        with override_settings(FIELD_ENCRYPTION_KEYS=[]):
            with patch(self.SENDER, return_value=(200, "")) as sender:
                with self.assertRaises(Exception):
                    self._run()
        sender.assert_not_called()
