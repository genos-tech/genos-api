"""Log redaction (MIP-622): secrets and emails never reach a log line.

`apis/log_redaction.py` masks by shape, in the formatters, so these pin:

* the shared cases (`apis/log_redaction_cases.json`, which genos-sockets
  and genos-collab test their own redactors against);
* that every formatter `settings.LOGGING` declares, and every handler it
  installs, redacts, traceback included;
* that the JSON formatter redacts before encoding, since its escapes would
  hide a quoted secret from the patterns;
* the cron tripwire's message, which Django prints outside logging;
* that a long run with nothing to mask stays cheap, since this runs on
  every line.

SimpleTestCase (no DB).
"""

import json
import logging
import logging.config
import random
import string
import sys
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from apis import log_redaction
from apis.log_redaction import RedactingFormatter, redact
from apis.logging_formatters import RailwayJSONFormatter
from origin.management.cron_command import CronCommand

_CASES = json.loads(
    Path(log_redaction.__file__).with_name("log_redaction_cases.json").read_text("utf-8")
)

# Made up, like everything in the cases file.
_EMAIL = "someone@example.com"
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjo0Mn0.c2lnbmF0dXJl"


def _record(msg, exc_info=None, name="origin.test"):
    return logging.LogRecord(
        name=name,
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )


def _failed_record():
    """An ERROR record whose traceback carries an email and a JWT."""
    try:
        raise ValueError(f"no account for {_EMAIL} with token {_JWT}")
    except ValueError:
        return _record("lookup failed", exc_info=sys.exc_info())


class SharedCasesTests(SimpleTestCase):
    def test_masked(self):
        for case in _CASES["masked"]:
            with self.subTest(case["in"]):
                self.assertEqual(redact(case["in"]), case["out"])

    def test_unchanged(self):
        for text in _CASES["unchanged"]:
            with self.subTest(text):
                self.assertEqual(redact(text), text)

    def test_masking_is_idempotent(self):
        # A line formatted twice (a handler re-emitting a record) must not
        # come out different the second time.
        for case in _CASES["masked"]:
            with self.subTest(case["in"]):
                self.assertEqual(redact(case["out"]), case["out"])


class FormatterTests(SimpleTestCase):
    def test_text_formatter_redacts_message_and_traceback(self):
        line = RedactingFormatter("%(message)s").format(_failed_record())
        self.assertIn("Traceback", line)
        self.assertNotIn(_EMAIL, line)
        self.assertNotIn(_JWT, line)
        self.assertIn("[EMAIL]", line)

    def test_json_formatter_redacts_message_and_traceback(self):
        obj = json.loads(RailwayJSONFormatter().format(_failed_record()))
        self.assertIn("Traceback", obj["message"])
        self.assertNotIn(_EMAIL, obj["message"])
        self.assertNotIn(_JWT, obj["message"])

    def test_json_formatter_redacts_before_encoding(self):
        # Encoded, `"password": "a b"` becomes `\"password\": \"a b\"`,
        # which the quoted-value pattern can't see.
        msg = 'payload {"password": "hunter2 with spaces"}'
        obj = json.loads(RailwayJSONFormatter().format(_record(msg)))
        self.assertEqual(obj["message"], 'payload {"password": "[REDACTED]"}')

    def test_every_configured_formatter_redacts(self):
        # Whichever LOG_FORMAT selects, the console handler's formatter is
        # one of these.
        for name, conf in settings.LOGGING["formatters"].items():
            with self.subTest(name):
                formatter = logging.config.DictConfigurator({}).configure_formatter(dict(conf))
                line = formatter.format(_failed_record())
                self.assertNotIn(_EMAIL, line)
                self.assertNotIn(_JWT, line)

    def test_every_installed_handler_redacts(self):
        loggers = [logging.getLogger(name) for name in settings.LOGGING["loggers"]]
        handlers = {h for lg in [*loggers, logging.getLogger()] for h in lg.handlers}
        self.assertTrue(handlers, "settings.LOGGING installed no handlers")
        for handler in handlers:
            with self.subTest(handler=handler):
                self.assertIsNotNone(handler.formatter)
                line = handler.formatter.format(_failed_record())
                self.assertNotIn(_EMAIL, line)

    def test_record_is_left_alone(self):
        # Redacting the record itself would change what every other handler
        # (the cron tripwire, a test's assertLogs) sees.
        record = _record(f"mail to {_EMAIL}")
        RedactingFormatter("%(message)s").format(record)
        RailwayJSONFormatter().format(record)
        self.assertEqual(record.getMessage(), f"mail to {_EMAIL}")


class _LogsEmail(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        logging.getLogger("origin.tests.cron").error("send to %s failed", _EMAIL)


class CronTripwireTests(SimpleTestCase):
    def test_command_error_is_redacted(self):
        with self.assertRaises(CommandError) as ctx:
            _LogsEmail().execute(force_color=False, no_color=False, skip_checks=True)
        self.assertNotIn(_EMAIL, str(ctx.exception))
        self.assertIn("send to [EMAIL] failed", str(ctx.exception))


class CostTests(SimpleTestCase):
    def test_long_run_without_a_match_is_linear(self):
        # A base64 blob in a logged payload. Before the email pattern was
        # anchored, 160k characters took 50 s.
        rng = random.Random(1)
        alphabet = string.ascii_letters + string.digits + "-_"
        blob = "".join(rng.choice(alphabet) for _ in range(200_000))
        for text in (blob, "a." * 100_000, "x@" + "a." * 100_000):
            started = time.perf_counter()
            redact(text)
            self.assertLess(time.perf_counter() - started, 2.0)
