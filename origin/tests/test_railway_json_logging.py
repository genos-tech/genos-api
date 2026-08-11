"""RailwayJSONFormatter — the structured-log shape Railway classifies by level.

Guards the contract that fixes "my ERROR logs show up as INFO in Railway":
Railway stamps every stdout line `info` unless the line is single-line JSON with
a recognised `level` field. These tests pin the exact field names + level values
Railway accepts (debug/info/warn/error, lowercased) and the single-line
guarantee for tracebacks.

SimpleTestCase (no DB) — run with the rest of the suite:
    coverage run manage.py test origin.tests --settings=apis.settings_test
"""

import json
import logging
import sys
from unittest import mock

from django.test import SimpleTestCase

from apis.logging_formatters import RailwayJSONFormatter


def _record(level, msg, name="origin.test", exc_info=None):
    return logging.LogRecord(
        name=name, level=level, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=exc_info,
    )


class RailwayJSONFormatterTests(SimpleTestCase):
    def setUp(self):
        self.fmt = RailwayJSONFormatter()

    def test_emits_single_line_json_with_core_fields(self):
        line = self.fmt.format(_record(logging.INFO, "hello"))
        self.assertNotIn("\n", line, "Railway requires the whole JSON log on one line")
        obj = json.loads(line)
        self.assertEqual(obj["level"], "info")
        self.assertEqual(obj["message"], "hello")
        self.assertEqual(obj["logger"], "origin.test")
        self.assertTrue(obj["timestamp"].endswith("Z"))

    def test_level_names_map_to_railways_canonical_set(self):
        cases = {
            logging.DEBUG: "debug",
            logging.INFO: "info",
            logging.WARNING: "warn",  # not "warning" — Railway's canonical token
            logging.ERROR: "error",
            logging.CRITICAL: "error",  # closest canonical bucket
        }
        for level, expected in cases.items():
            obj = json.loads(self.fmt.format(_record(level, "m")))
            self.assertEqual(obj["level"], expected, f"{level} -> {obj['level']}")

    def test_origin_error_is_queryable_as_error(self):
        # The CronCommand tripwire keys off ERROR on the `origin` logger. After
        # formatting that must surface as level=error with the logger name, so
        # an external alert of `@level:error AND @logger:origin*` can match.
        obj = json.loads(self.fmt.format(_record(logging.ERROR, "boom", name="origin.webhooks")))
        self.assertEqual(obj["level"], "error")
        self.assertEqual(obj["logger"], "origin.webhooks")

    def test_exception_traceback_stays_on_one_line(self):
        try:
            raise ValueError("kaboom")
        except ValueError:
            rec = _record(logging.ERROR, "failed", exc_info=sys.exc_info())
        line = self.fmt.format(rec)
        self.assertNotIn("\n", line, "traceback newlines must be JSON-escaped")
        obj = json.loads(line)
        self.assertIn("kaboom", obj["message"])
        self.assertIn("Traceback", obj["message"])

    def test_non_ascii_message_is_not_escaped(self):
        obj = json.loads(self.fmt.format(_record(logging.INFO, "こんにちは")))
        self.assertEqual(obj["message"], "こんにちは")

    def test_railway_context_fields_included_when_env_present(self):
        with mock.patch.dict("os.environ", {
            "RAILWAY_SERVICE_NAME": "backend-django",
            "RAILWAY_DEPLOYMENT_ID": "dep_123",
        }):
            obj = json.loads(self.fmt.format(_record(logging.INFO, "m")))
        self.assertEqual(obj["service"], "backend-django")
        self.assertEqual(obj["deployment"], "dep_123")

    def test_railway_context_absent_when_env_unset(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            obj = json.loads(self.fmt.format(_record(logging.INFO, "m")))
        self.assertNotIn("service", obj)
        self.assertNotIn("deployment", obj)
