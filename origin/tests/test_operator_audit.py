"""Audit of operator management commands in production (MIP-2260)."""

from __future__ import annotations

import io
from unittest import mock

from django.core.management.base import BaseCommand, CommandError
from django.test import TestCase

from origin.models.common.ops_models import OpsAlert
from origin.models.common.security_event_models import (
    SecurityAction,
    SecurityEvent,
    SecuritySource,
)
from origin.services import operator_audit

PROD = {"RAILWAY_ENVIRONMENT": "production"}


class _Cmd(BaseCommand):
    """A stand-in command whose body is set per test."""

    body = staticmethod(lambda: None)

    def add_arguments(self, parser):
        parser.add_argument("pos", nargs="*")
        parser.add_argument("--email", default="")
        parser.add_argument("--reason", default="")
        parser.add_argument("--heartbeat", default="")
        parser.add_argument("-c", dest="c", default="")

    def handle(self, *args, **options):
        type(self).body()


def _run(name, *args, body=lambda: None, env=PROD):
    _Cmd.body = staticmethod(body)
    cmd = _Cmd(stdout=io.StringIO(), stderr=io.StringIO())
    # `run_from_argv` closes every connection when it ends, which would end the test's own.
    with (
        mock.patch.dict("os.environ", env, clear=False),
        mock.patch("django.core.management.base.connections.close_all"),
    ):
        # The class method installed by AppConfig.ready() is what manage.py goes through.
        BaseCommand.run_from_argv(cmd, ["manage.py", name, *args])


def _rows(action=None):
    qs = SecurityEvent.objects.all().order_by("event_id")
    return list(qs.filter(action=action) if action else qs)


class RedactArgsTests(TestCase):
    def test_sensitive_flag_values_are_dropped(self):
        out = operator_audit.redact_args(
            ["--email", "someone@example.com", "--token=abc", "--dry-run", "--reason", "cleanup"]
        )
        self.assertEqual(
            out, ["--email", "[REDACTED]", "--token=[REDACTED]", "--dry-run", "--reason", "cleanup"]
        )
        self.assertNotIn("someone", " ".join(out))

    def test_email_and_secret_in_a_positional_or_plain_value_are_masked(self):
        out = operator_audit.redact_args(["me@example.com", "--note=mail me@example.com"])
        joined = " ".join(out)
        self.assertNotIn("me@example.com", joined)

    def test_long_argument_lists_and_values_are_cut(self):
        out = operator_audit.redact_args(["x" * 500] + ["a"] * 40)
        self.assertLessEqual(len(out[0]), 120)
        self.assertEqual(out[-1], "...(+21 more)")


class OperatorAuditTests(TestCase):
    def test_not_production_writes_nothing(self):
        _run("somecmd", env={"RAILWAY_ENVIRONMENT": "staging"})
        _run("somecmd", env={"RAILWAY_ENVIRONMENT": ""})
        self.assertEqual(_rows(), [])

    def test_listed_command_writes_started_and_finished_without_alert(self):
        _run("migrate", "--reason", "deploy")
        rows = _rows()
        self.assertEqual([r.action for r in rows], [SecurityAction.OPS_COMMAND] * 2)
        self.assertEqual([r.metadata["phase"] for r in rows], ["started", "finished"])
        self.assertEqual(rows[0].source, SecuritySource.OPERATOR)
        self.assertEqual(rows[0].target_id, "migrate")
        self.assertTrue(rows[0].actor_label.startswith("os:"))
        self.assertEqual(rows[0].metadata["args"], ["--reason", "deploy"])
        self.assertIn("duration_seconds", rows[1].metadata)
        self.assertFalse(OpsAlert.objects.exists())

    def test_email_flag_value_never_reaches_the_row(self):
        _run("migrate", "--email", "victim@example.com", "--reason=x victim@example.com")
        for row in _rows():
            self.assertNotIn("victim", str(row.metadata) + row.target_label)

    def test_unlisted_interactive_command_alerts(self):
        _run("repair_everything", "--reason", "x")
        actions = [r.action for r in _rows()]
        self.assertIn(SecurityAction.ALERT_OPS_COMMAND_UNLISTED, actions)
        alert = OpsAlert.objects.get()
        self.assertIn("repair_everything", alert.summary)
        self.assertEqual(alert.kind, "security")

    def test_failed_command_is_recorded_failed_and_still_raises(self):
        def boom():
            raise CommandError("nope")

        with self.assertRaises(SystemExit):
            _run("migrate", body=boom)
        phases = [r.metadata["phase"] for r in _rows(SecurityAction.OPS_COMMAND)]
        self.assertEqual(phases, ["started", "failed"])

    def test_cron_writes_no_row_only_a_log_line(self):
        with self.assertLogs("origin.services.operator_audit", "INFO") as logs:
            _run("some_tick", "--heartbeat", "some-tick")
        self.assertEqual(_rows(), [])
        self.assertFalse(OpsAlert.objects.exists())
        self.assertIn("cron=some-tick", logs.output[0])

    def test_shell_is_opened_and_closed_without_its_content(self):
        _run("shell", "-c", "User.objects.all().delete()")
        rows = _rows()
        actions = [r.action for r in rows]
        self.assertIn(SecurityAction.OPS_SHELL_OPENED, actions)
        self.assertEqual(actions[-1], SecurityAction.OPS_SHELL_CLOSED)
        self.assertIn(SecurityAction.ALERT_OPS_COMMAND_UNLISTED, actions)
        opened = rows[0]
        self.assertFalse(opened.metadata["content_captured"])
        self.assertEqual(opened.metadata["args"], [])
        self.assertNotIn("delete", str([r.metadata for r in rows]))

    def test_audit_failure_never_stops_the_command(self):
        ran = []
        with mock.patch.object(
            operator_audit, "record_security_event", side_effect=RuntimeError("db")
        ):
            _run("migrate", body=lambda: ran.append(1))
        self.assertEqual(ran, [1])

    def test_command_started_without_run_from_argv_is_not_counted(self):
        """`call_command` (a command inside a command) is not an operator action."""
        from django.core.management import call_command

        with mock.patch.dict("os.environ", PROD):
            call_command("check")
        self.assertEqual(_rows(), [])
