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

import ast
import json
import logging
import logging.config
import random
import string
import sys
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from apis import log_redaction
from apis.log_redaction import RedactingFormatter, redact, redact_line
from apis.logging_formatters import RailwayJSONFormatter
from origin.management.cron_command import CronCommand
from origin.services.email_unsubscribe import make_token, unsubscribe_url

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


class UnsubscribeTokenTests(TestCase):
    """The unsubscribe link carries a long-lived signed token that contains the
    user's id (MIP-1788). Django logs the path of every 4xx
    (`Bad Request: /api/v2/email/unsubscribe/<token>/`), and the throttles log
    it too, so the formatters rewrite it to the route's template."""

    def _token(self):
        return make_token(uuid.uuid4(), "all")

    def test_the_path_of_a_refused_request_names_the_route_and_not_the_token(self):
        token = self._token() + "x"  # tampered, so the view answers 400
        with self.assertLogs("django.request", "WARNING") as logs:
            res = self.client.get(f"/api/v2/email/unsubscribe/{token}/")
        self.assertEqual(res.status_code, 400)
        # The leak at its source: Django writes the token.
        self.assertIn(token, "\n".join(logs.output))
        for formatter in (RedactingFormatter("%(message)s"), RailwayJSONFormatter()):
            line = formatter.format(logs.records[0])
            with self.subTest(formatter=type(formatter).__name__):
                self.assertNotIn(token, line)
                self.assertNotIn(token.split(":")[0], line)
                self.assertIn("/api/v2/email/unsubscribe/<token>/", line)

    @override_settings(API_PUBLIC_BASE_URL="https://api.example.com")
    def test_a_full_url_and_the_list_unsubscribe_header_are_rewritten(self):
        url = unsubscribe_url(uuid.uuid4(), "all")
        token = url.split("/unsubscribe/")[1].rstrip("/")
        for text in (
            f"mailed {url}",
            f"List-Unsubscribe: <{url}>",
            f"POST /api/v2/email/unsubscribe/{token}/?x=1 400",
            f"path=/api/v2/email/unsubscribe/{token}",
            f"Bad Request: /api/v2/email/unsubscribe/{token}/",
        ):
            with self.subTest(text=text):
                out = redact_line(text)
                self.assertNotIn(token, out)
                self.assertIn("unsubscribe/<token>", out)

    def test_a_token_the_shape_rules_would_miss_is_rewritten_too(self):
        """`_LONG` needs upper case, lower case and a digit in one run; the route rule does
        not depend on what the token looks like."""
        token = "abcdefghijklmnopqrstuvwxyzabcdefghijklmnop"
        out = redact_line(f"Bad Request: /api/v2/email/unsubscribe/{token}/")
        self.assertEqual(out, "Bad Request: /api/v2/email/unsubscribe/<token>/")

    def test_other_text_is_not_touched(self):
        for text in (
            "[email] unsubscribed user=1 scope=all",
            "unsubscribe/<token>",
            "/api/v2/email/unsubscribe/",
            "/api/v2/team/mute-unsubscribe/abc",
            "the unsubscribe link was clicked",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_line(text), text)


class NoPrintInAppCodeTests(SimpleTestCase):
    """`print()` goes to stdout, past the formatters that redact (MIP-1788). Request-path
    code logs through `logging`; management commands print on purpose (their output
    is the command's answer), and tests and scripts are not served."""

    def test_no_print_call_outside_commands_and_tests(self):
        root = Path(__file__).resolve().parents[1]
        offenders = []
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).parts
            if "tests" in rel or "migrations" in rel or rel[:2] == ("management", "commands"):
                continue
            tree = ast.parse(path.read_text("utf-8"))
            offenders += [
                f"{'/'.join(rel)}:{node.lineno}"
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ]
        self.assertEqual(offenders, [])


class IsAddressTests(SimpleTestCase):
    """`is_address` decides what a failed sign-in may keep (MIP-1789); it must agree
    with the shape `mask_email` masks and stay cheap on hostile text."""

    def test_agrees_with_the_masking_shape(self):
        from origin.services.pii_mask import is_address, mask_email  # noqa: PLC0415

        for text, expected in (
            ("kenji@gmail.com", True),
            ("a.b+c_d%e-f@mail.example.co.jp", True),
            ("x@y.z", False),  # one-letter TLD
            ("user@localhost", False),
            ("hunter2 ghost@example.com", False),
            ("ghost@example.com ", False),
            ("a@b@example.com", False),
            ("@example.com", False),
            ("ghost@.example.com", False),
            ("ghost@example..com", False),
            ("ghost@example.c0m", False),
            ("", False),
            ("hunter2-pasted-by-mistake", False),
            ("é@example.com", False),
        ):
            with self.subTest(text=text):
                self.assertEqual(is_address(text), expected)
                if expected:
                    self.assertNotEqual(mask_email(text), text)

    def test_hostile_text_is_cheap(self):
        from origin.services.pii_mask import is_address  # noqa: PLC0415

        for text in ("%" * 200_000, "a@" + "a." * 100_000, "@" * 100_000):
            started = time.perf_counter()
            self.assertFalse(is_address(text))
            self.assertLess(time.perf_counter() - started, 0.5)
