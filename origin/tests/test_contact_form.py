"""POST /api/v2/contact/ — the public contact form (MIP-1261, decision D11).

What these pin down:
  * one plain-text email to support, Reply-To the sender, nothing else sent;
  * no database write (no INSERT/UPDATE/DELETE issued) and no log line that
    carries the sender's name, address or message text;
  * validation returns field codes only, never an echo of the data;
  * the honeypot and the per-IP / per-sender limits; a mail failure is a 503.
"""

import logging
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from origin.services import throttles
from origin.views.common.contact_views import ContactEmailThrottle, ContactIpThrottle

URL = "/api/v2/contact/"
SUPPORT = "genos.support@genosai.dev"
SENDER = "taro.yamada.test@example.com"
NAME = "Taro Yamada"
MESSAGE = "We would like to talk about a rollout for about forty people."


def _payload(**overrides):
    data = {
        "name": NAME,
        "email": SENDER,
        "topic": "enterprise",
        "message": MESSAGE,
        "language": "en",
    }
    data.update(overrides)
    return data


class ContactFormBase(TestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        cache.clear()
        self.addCleanup(cache.clear)
        # A cache error elsewhere opens a breaker that would make these throttles allow.
        self.addCleanup(setattr, throttles, "_down_until", 0.0)

    def post(self, data=None, ip="203.0.113.7", **kwargs):
        return self.client.post(
            URL,
            data if data is not None else _payload(),
            format="json",
            REMOTE_ADDR=ip,
            **kwargs,
        )


class TestSendingTheEmail(ContactFormBase):
    def test_valid_submission_is_accepted_and_mails_support_once(self):
        from django.core import mail

        res = self.post()

        self.assertEqual(res.status_code, 202)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, [SUPPORT])
        self.assertEqual(msg.reply_to, [SENDER])
        self.assertEqual(msg.bcc, [])
        self.assertEqual(msg.cc, [])
        self.assertFalse(msg.message().is_multipart())  # plain text only
        self.assertIn("Topic: Enterprise enquiry", msg.body)
        self.assertIn(MESSAGE, msg.body)
        self.assertIn(NAME, msg.body)

    def test_response_does_not_echo_the_data(self):
        res = self.post()
        body = res.content.decode()
        for value in (NAME, SENDER, MESSAGE):
            self.assertNotIn(value, body)

    def test_control_characters_and_crlf_cannot_inject_headers(self):
        from django.core import mail

        res = self.post(
            _payload(
                name="Taro\r\nBcc: hidden@example.com",
                message="Hello\x00\x07 there\r\nBcc: other@example.com",
            )
        )

        self.assertEqual(res.status_code, 202)
        msg = mail.outbox[0]
        self.assertEqual(msg.bcc, [])
        self.assertNotIn("\x00", msg.body)
        self.assertNotIn("\x07", msg.body)
        self.assertNotIn("Bcc:", msg.message().as_string().split("\n\n", 1)[0])

    def test_mail_failure_is_a_503_and_the_type_only_is_logged(self):
        with mock.patch(
            "origin.views.common.contact_views.send_contact_email",
            side_effect=OSError(f"smtp refused for {SENDER}"),
        ):
            with self.assertLogs("origin.views.common.contact_views", "ERROR") as logs:
                res = self.post()

        self.assertEqual(res.status_code, 503)
        self.assertNotIn(SENDER, "\n".join(logs.output))
        self.assertIn("OSError", "\n".join(logs.output))


class TestNothingIsStored(ContactFormBase):
    def test_no_write_is_issued_to_the_database(self):
        with CaptureQueriesContext(connection) as ctx:
            res = self.post()
        self.assertEqual(res.status_code, 202)
        writes = [
            q["sql"]
            for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(writes, [])

    def test_no_log_line_carries_name_email_or_message(self):
        records = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        # `origin` does not propagate to root, so watch both: the app's
        # loggers and anything that reaches the root logger.
        handler = Collect(level=logging.DEBUG)
        for name in (None, "origin"):
            log = logging.getLogger(name)
            old_level = log.level
            log.addHandler(handler)
            log.setLevel(logging.DEBUG)
            self.addCleanup(log.removeHandler, handler)
            self.addCleanup(log.setLevel, old_level)
        res = self.post()
        self.assertEqual(res.status_code, 202)
        text = "\n".join(records)
        for value in (NAME, SENDER, MESSAGE, "Taro", "Yamada"):
            self.assertNotIn(value, text)
        self.assertIn("contact.sent topic=enterprise", text)


class TestValidation(ContactFormBase):
    def assert_field_error(self, data, field):
        from django.core import mail

        res = self.post(data)
        self.assertEqual(res.status_code, 400)
        self.assertIn(field, res.json()["errors"])
        self.assertEqual(mail.outbox, [])
        return res

    def test_bad_email_is_rejected(self):
        self.assert_field_error(_payload(email="not-an-address"), "email")

    def test_missing_name_is_rejected(self):
        self.assert_field_error(_payload(name="   "), "name")

    def test_unknown_topic_is_rejected(self):
        self.assert_field_error(_payload(topic="sales"), "topic")

    def test_message_over_4000_characters_is_rejected(self):
        self.assert_field_error(_payload(message="x" * 4001), "message")

    def test_message_of_exactly_4000_characters_is_accepted(self):
        self.assertEqual(self.post(_payload(message="x" * 4000)).status_code, 202)

    def test_error_body_names_fields_and_never_values(self):
        res = self.post(_payload(email="bad@", message=""))
        body = res.content.decode()
        self.assertEqual(set(res.json()["errors"]), {"email", "message"})
        self.assertNotIn("bad@", body)

    def test_unknown_language_falls_back_to_english(self):
        from django.core import mail

        self.assertEqual(self.post(_payload(language="fr")).status_code, 202)
        self.assertIn("Page language: en", mail.outbox[0].body)


class TestHoneypot(ContactFormBase):
    def test_filled_honeypot_reports_success_and_sends_nothing(self):
        from django.core import mail

        res = self.post(_payload(website="http://spam.example"))
        self.assertEqual(res.status_code, 202)
        self.assertEqual(mail.outbox, [])


class TestThrottles(ContactFormBase):
    def test_sender_address_is_limited_whatever_the_ip(self):
        for i in range(5):
            self.assertEqual(self.post(ip=f"198.51.100.{i}").status_code, 202)
        self.assertEqual(self.post(ip="198.51.100.99").status_code, 429)

    def test_one_ip_is_limited_across_sender_addresses(self):
        with mock.patch.object(ContactIpThrottle, "rate", "3/hour"):
            for i in range(3):
                self.assertEqual(self.post(_payload(email=f"p{i}@example.com")).status_code, 202)
            self.assertEqual(self.post(_payload(email="p9@example.com")).status_code, 429)

    def test_the_sender_key_is_a_hash_not_the_address(self):
        throttle = ContactEmailThrottle()
        key = throttle.get_cache_key(type("R", (), {"data": {"email": SENDER}})(), None)
        self.assertIsNotNone(key)
        self.assertNotIn(SENDER, key)
        self.assertNotIn("example.com", key)

    def test_cache_outage_fails_closed(self):
        with mock.patch.object(ContactEmailThrottle, "cache", new_callable=mock.PropertyMock) as c:
            c.return_value.get.side_effect = OSError("redis down")
            c.return_value.add.side_effect = OSError("redis down")
            with self.assertLogs("origin.services.throttles", "ERROR"):
                res = self.post()
        self.assertEqual(res.status_code, 429)
