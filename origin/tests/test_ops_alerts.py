"""Alerts that reach a person (MIP-772).

What's pinned, roughly in order of how silently each would break:

1. **Every cron reports.** Each railway-*.toml passes `--heartbeat` with
   its own service name, and each name has a schedule to be missed
   against. A cron left out is a cron whose death nobody hears about.
2. **The heartbeat tells success from failure,** including a run the
   tripwire fails after `handle()` returned normally.
3. **One incident, one message.** Recurrences update the open row; the
   tick mails everything waiting in one email, once.
4. **Each source opens an alert:** `alert.*` security events (through the
   real endpoints), 5xx bursts, missed crons, expiring certificates.
5. **The outside probe's endpoint** answers 404 without its token and
   503 when the tick has stopped.
"""

from __future__ import annotations

import logging
import re
import ssl
import time
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command, get_commands, load_command_class
from django.core.management.base import CommandError
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from origin.management.cron_command import CronCommand, HeartbeatCommand
from origin.models.common.ops_models import CronHeartbeat, OpsAlert, OpsAlertKind
from origin.models.common.security_event_models import SecurityAction
from origin.services import ops_alerts, throttles
from origin.tests.test_cron_command import _EXEC_OPTS
from origin.tests.test_security_events import SecurityEventCase

REPO = Path(settings.BASE_DIR)
_START = re.compile(r'^startCommand = "(.*)"$', re.M)
_HEARTBEAT = re.compile(r"--heartbeat ([a-z0-9-]+)")

TOKEN = "probe-token-for-tests"
ALERTING_URL = "/api/v2/health/alerting/"


def _tomls():
    return sorted(p for p in REPO.glob("railway-*.toml"))


class TestEveryCronReports(TestCase):
    def test_every_cron_toml_passes_its_heartbeat(self):
        for toml in _tomls():
            with self.subTest(toml=toml.name):
                start = _START.search(toml.read_text())
                self.assertIsNotNone(start, "no startCommand")
                names = _HEARTBEAT.findall(start.group(1))
                self.assertEqual(len(names), 1, "expected exactly one --heartbeat")

    def test_heartbeat_names_are_unique(self):
        names = [
            _HEARTBEAT.search(_START.search(t.read_text()).group(1)).group(1) for t in _tomls()
        ]
        self.assertEqual(len(names), len(set(names)), names)

    def test_every_watched_cron_has_a_schedule_and_every_schedule_a_cron(self):
        # The tick is the one cron nothing inside can watch: the outside
        # probe does, through AlertingHealthView.
        names = {
            _HEARTBEAT.search(_START.search(t.read_text()).group(1)).group(1) for t in _tomls()
        }
        self.assertIn(ops_alerts.TICK_NAME, names)
        # The probe beats through AlertingHealthView, not a toml.
        self.assertEqual(
            names - {ops_alerts.TICK_NAME},
            set(ops_alerts.CRON_SCHEDULES) - {ops_alerts.PROBE_NAME},
        )

    def test_the_web_service_has_no_heartbeat(self):
        # railway.toml is backend-django, not a cron; a heartbeat there
        # would be missed forever.
        self.assertNotIn("--heartbeat", (REPO / "railway.toml").read_text())

    def test_every_cron_command_takes_the_flag(self):
        # A plain BaseCommand would refuse `--heartbeat` and fail every run.
        # Followed through the entrypoint, which must pass "$@" on.
        manage = re.compile(r"manage\.py (\w+)(?: \\\n\s*)? \"\$@\"")
        for toml in _tomls():
            start = _START.search(toml.read_text()).group(1)
            with self.subTest(toml=toml.name):
                direct = re.match(r"python /app/manage\.py (\w+) ", start)
                if direct:
                    command = direct.group(1)
                else:
                    script = re.match(r"sh /app/(\S+\.sh) ", start).group(1)
                    found = manage.findall((REPO / script).read_text())
                    self.assertEqual(len(found), 1, f'{script} must exec one command with "$@"')
                    command = found[0]
                app = get_commands()[command]
                cmd = load_command_class(app, command)
                self.assertIsInstance(cmd, HeartbeatCommand, command)


# ── heartbeats ──────────────────────────────────────────────────────────


class _Clean(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        pass


class _LogsError(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        logging.getLogger("origin.tests.ops").error("pass failed for %s", "x@example.com")


class _Crashes(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        raise ValueError("leaks x@example.com")


class _PlainCrashes(HeartbeatCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        logging.getLogger("origin.tests.ops").error("tolerated")
        raise CommandError("render failed")


class _NoReconnect:
    """`_beat` closes stale connections first, as a cron should after a long
    pass. Inside a TestCase that would close the test's own transaction."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch("origin.services.ops_alerts.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)


class TestHeartbeat(_NoReconnect, TestCase):
    def beat(self, name="demo-user-cleanup"):
        return CronHeartbeat.objects.get(name=name)

    def test_no_flag_records_nothing(self):
        _Clean().execute(**_EXEC_OPTS)
        self.assertFalse(CronHeartbeat.objects.exists())

    def test_a_clean_run_records_success(self):
        _Clean().execute(**_EXEC_OPTS, heartbeat="demo-user-cleanup")
        beat = self.beat()
        self.assertIsNotNone(beat.last_started_at)
        self.assertIsNotNone(beat.last_succeeded_at)
        self.assertIsNone(beat.last_failed_at)
        self.assertFalse(OpsAlert.objects.exists())

    def test_the_flag_goes_through_the_parser(self):
        # The wiring railway-*.toml relies on: `--heartbeat` on a command
        # that never declared it.
        call_command(_Clean(), "--heartbeat", "demo-user-cleanup")
        self.assertIsNotNone(self.beat().last_succeeded_at)

    def test_a_tripwire_failure_is_a_failure(self):
        # handle() returned normally; the run failed afterwards, in the
        # tripwire. Recorded as a success, this cron would never alert.
        with self.assertRaises(CommandError):
            _LogsError().execute(**_EXEC_OPTS, heartbeat="demo-user-cleanup")
        beat = self.beat()
        self.assertIsNone(beat.last_succeeded_at)
        self.assertIsNotNone(beat.last_failed_at)
        self.assertNotIn("example.com", beat.last_error)
        alert = OpsAlert.objects.get(key="cron:demo-user-cleanup")
        self.assertEqual(alert.kind, OpsAlertKind.CRON_FAILED)
        self.assertEqual(alert.detail["service"], "demo-user-cleanup")

    def test_a_crash_keeps_the_type_not_the_text(self):
        with self.assertRaises(ValueError):
            _Crashes().execute(**_EXEC_OPTS, heartbeat="demo-user-cleanup")
        self.assertEqual(self.beat().last_error, "ValueError")

    def test_a_heartbeat_command_has_no_tripwire(self):
        # ai_cost_dashboard and agent_digest: an ERROR line alone doesn't
        # fail them, but a raise does, and is recorded.
        with self.assertRaises(CommandError):
            _PlainCrashes().execute(**_EXEC_OPTS, heartbeat="agent-digest")
        self.assertEqual(self.beat("agent-digest").last_error, "CommandError: render failed")

    def test_success_resolves_the_failure(self):
        with self.assertRaises(CommandError):
            _LogsError().execute(**_EXEC_OPTS, heartbeat="demo-user-cleanup")
        _Clean().execute(**_EXEC_OPTS, heartbeat="demo-user-cleanup")
        alert = OpsAlert.objects.get(key="cron:demo-user-cleanup")
        self.assertIsNotNone(alert.resolved_at)

    def test_a_failing_cron_is_one_incident(self):
        for _ in range(3):
            with self.assertRaises(CommandError):
                _LogsError().execute(**_EXEC_OPTS, heartbeat="demo-user-cleanup")
        alert = OpsAlert.objects.get(key="cron:demo-user-cleanup")
        self.assertEqual(alert.occurrences, 3)

    def test_a_missing_table_does_not_fail_the_run(self):
        # A cron deployed before backend-django migrated 0205.
        with mock.patch.object(CronHeartbeat.objects, "filter", side_effect=RuntimeError("no")):
            with self.assertLogs("origin.services.ops_alerts", "WARNING") as logs:
                _Clean().execute(**_EXEC_OPTS, heartbeat="demo-user-cleanup")
        self.assertFalse([r for r in logs.records if r.levelno >= logging.ERROR])


# ── incidents ───────────────────────────────────────────────────────────


class TestIncidents(TestCase):
    def test_a_recurrence_updates_the_open_row(self):
        first = ops_alerts.raise_alert(OpsAlertKind.CRON_MISSED, "cron:x", "one")
        second = ops_alerts.raise_alert(OpsAlertKind.CRON_MISSED, "cron:x", "two")
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.occurrences, 2)
        self.assertEqual(second.summary, "two")

    def test_a_flapping_cron_reopens_the_sent_row(self):
        alert = ops_alerts.raise_alert(OpsAlertKind.CRON_FAILED, "cron:x", "failed")
        OpsAlert.objects.filter(pk=alert.pk).update(notified_at=timezone.now())
        ops_alerts.resolve_alert("cron:x")
        again = ops_alerts.raise_alert(OpsAlertKind.CRON_FAILED, "cron:x", "failed")
        self.assertEqual(again.pk, alert.pk)
        self.assertIsNone(again.resolved_at)
        self.assertIsNotNone(again.notified_at, "a reopened incident must not mail again")

    def test_a_cron_failing_again_much_later_is_a_new_incident(self):
        alert = ops_alerts.raise_alert(OpsAlertKind.CRON_FAILED, "cron:x", "failed")
        ops_alerts.resolve_alert("cron:x", now=timezone.now() - timedelta(hours=7))
        again = ops_alerts.raise_alert(OpsAlertKind.CRON_FAILED, "cron:x", "failed")
        self.assertNotEqual(again.pk, alert.pk)
        self.assertIsNone(again.notified_at)

    def test_a_new_security_alert_after_a_quiet_one_is_news(self):
        alert = ops_alerts.raise_alert(OpsAlertKind.SECURITY, "security:a", "export")
        ops_alerts.resolve_alert("security:a")
        again = ops_alerts.raise_alert(OpsAlertKind.SECURITY, "security:a", "export")
        self.assertNotEqual(again.pk, alert.pk)

    def test_quiet_alerts_resolve_only_once_sent(self):
        long_ago = timezone.now() - timedelta(hours=2)
        unsent = ops_alerts.raise_alert(OpsAlertKind.SECURITY, "security:a", "a", now=long_ago)
        sent = ops_alerts.raise_alert(
            OpsAlertKind.SERVER_ERRORS, "server_errors", "b", now=long_ago
        )
        state = ops_alerts.raise_alert(OpsAlertKind.CRON_FAILED, "cron:x", "c", now=long_ago)
        OpsAlert.objects.filter(pk__in=[sent.pk, state.pk]).update(notified_at=long_ago)
        self.assertEqual(ops_alerts.resolve_quiet(), 1)
        unsent.refresh_from_db()
        sent.refresh_from_db()
        state.refresh_from_db()
        self.assertIsNone(unsent.resolved_at)
        self.assertIsNotNone(sent.resolved_at)
        self.assertIsNone(state.resolved_at, "a cron alert resolves on success, not on silence")

    def test_a_failed_write_never_raises(self):
        with mock.patch.object(OpsAlert.objects, "select_for_update", side_effect=RuntimeError):
            with self.assertLogs("origin.services.ops_alerts", "WARNING"):
                self.assertIsNone(ops_alerts.raise_alert(OpsAlertKind.SECURITY, "k", "s"))


# ── missed crons ────────────────────────────────────────────────────────


class TestMissedCrons(_NoReconnect, TestCase):
    def test_a_cron_past_its_schedule_alerts(self):
        now = timezone.now()
        CronHeartbeat.objects.create(
            name="email-notify",
            first_seen_at=now - timedelta(days=3),
            last_succeeded_at=now - timedelta(minutes=21),
        )
        CronHeartbeat.objects.create(
            name="email-digest",
            first_seen_at=now - timedelta(days=3),
            last_succeeded_at=now - timedelta(minutes=70),
        )
        self.assertEqual(ops_alerts.check_crons(now=now), ["email-notify"])
        alert = OpsAlert.objects.get(key="cron:email-notify")
        self.assertEqual(alert.kind, OpsAlertKind.CRON_MISSED)
        self.assertIn("21 min", alert.summary)

    def test_a_cron_that_never_succeeded_counts_from_its_first_start(self):
        now = timezone.now()
        CronHeartbeat.objects.create(name="reminder-tick", first_seen_at=now - timedelta(hours=1))
        ops_alerts.check_crons(now=now)
        self.assertIn("never succeeded", OpsAlert.objects.get(key="cron:reminder-tick").summary)

    def test_an_unknown_name_is_not_checked(self):
        # A hand run with a made-up --heartbeat must not page forever.
        CronHeartbeat.objects.create(
            name="by-hand", first_seen_at=timezone.now() - timedelta(days=30)
        )
        self.assertEqual(ops_alerts.check_crons(), [])

    def test_missed_then_failed_is_one_incident(self):
        now = timezone.now()
        CronHeartbeat.objects.create(name="email-notify", first_seen_at=now - timedelta(hours=2))
        ops_alerts.check_crons(now=now)
        ops_alerts.cron_failed("email-notify", "ValueError")
        alert = OpsAlert.objects.get(key="cron:email-notify")
        self.assertEqual(alert.kind, OpsAlertKind.CRON_FAILED, "the email must say it failed")


# ── 5xx bursts ──────────────────────────────────────────────────────────


@override_settings(OPS_5XX_BURST=3)
class TestServerErrorBurst(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(setattr, throttles, "_down_until", 0.0)
        self.factory = RequestFactory()

    def answer(self, status, n=1):
        middleware = ops_alerts.ServerErrorBurstMiddleware(lambda r: HttpResponse(status=status))
        for _ in range(n):
            middleware(self.factory.get("/api/v2/task/1/"))

    def test_a_burst_opens_one_alert(self):
        with self.assertLogs("origin.services.ops_alerts", "WARNING"):
            self.answer(500, n=10)
        alert = OpsAlert.objects.get(key="server_errors")
        self.assertEqual(alert.occurrences, 1)
        self.assertEqual(alert.detail["status"], 500)

    def test_under_the_threshold_nothing_opens(self):
        self.answer(500, n=2)
        self.assertFalse(OpsAlert.objects.exists())

    def test_deliberate_503s_and_client_errors_are_not_counted(self):
        self.answer(503, n=10)
        self.answer(404, n=10)
        self.assertFalse(OpsAlert.objects.exists())

    def test_an_unhandled_exception_is_counted(self):
        # Through the real stack: the middleware sits outside Django's
        # exception-to-500 conversion.
        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        self.client.raise_request_exception = False
        with mock.patch("origin.views.common.health_views.HealthView.get", boom):
            with self.assertLogs("django.request", "ERROR"):
                for _ in range(3):
                    self.assertEqual(self.client.get("/api/v2/health/").status_code, 500)
        self.assertTrue(OpsAlert.objects.filter(key="server_errors").exists())

    def test_a_dead_cache_leaves_the_request_alone(self):
        with mock.patch.object(throttles, "throttle_cache", side_effect=throttles.CacheDown):
            self.answer(500, n=10)
        self.assertFalse(OpsAlert.objects.exists())


# ── security alerts, through the real endpoints ─────────────────────────


class TestSecurityAlertsPage(SecurityEventCase):
    def test_an_export_opens_an_alert(self):
        self.authenticate(self.user)
        with self.assertLogs("origin.services.security_events", "WARNING"):
            self.client.get("/api/v2/user/account/export/")
        alert = OpsAlert.objects.get(kind=OpsAlertKind.SECURITY)
        self.assertEqual(alert.key, f"security:{SecurityAction.ALERT_EXPORT}:user:{self.user.id}")
        self.assertEqual(alert.detail["action"], SecurityAction.ALERT_EXPORT)

    @override_settings(SECURITY_FORBIDDEN_BURST=3)
    def test_a_long_scan_is_one_incident(self):
        # Four minutes of a scan: the burst alert fires each minute. Only
        # the burst counter's clock moves; `time.time` itself is everyone's.
        self.authenticate(self.user2)
        start = 60.0 * int(time.time() // 60)
        with self.assertLogs("origin.services.security_events", "WARNING"):
            for minute in range(4):
                clock = mock.Mock(time=mock.Mock(return_value=start + 60.0 * minute))
                with mock.patch("origin.services.security_events.time", clock):
                    for _ in range(3):
                        self.client.put(
                            "/api/v2/team/profile/",
                            {"team_id": str(self.team.team_id), "team_name": "Mine now"},
                            format="json",
                        )
        self.assertEqual(len(self.events(SecurityAction.ALERT_FORBIDDEN_BURST)), 4)
        alert = OpsAlert.objects.get(kind=OpsAlertKind.SECURITY)
        self.assertEqual(alert.occurrences, 4)
        self.assertIn("3 in 60 s", alert.summary)
        self.assertNotIn("example.com", alert.summary)


# ── certificates ────────────────────────────────────────────────────────


def _ca_b64(not_after: datetime) -> str:
    import base64

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test ca")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(not_after - timedelta(days=1825))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return base64.b64encode(cert.public_bytes(serialization.Encoding.PEM)).decode()


class TestCertificates(TestCase):
    now = datetime(2027, 1, 1, tzinfo=UTC)

    def run_check(self, ca_days=400, node=None):
        cfg = {
            **settings.SEARCH_ENGINE,
            "OPENSEARCH_USE_SSL": True,
            "OPENSEARCH_CA_CERT": _ca_b64(self.now + timedelta(days=ca_days)),
        }
        node = node or (lambda cfg: self.now + timedelta(days=400))
        with override_settings(SEARCH_ENGINE=cfg):
            with mock.patch.object(ops_alerts, "_node_not_after", side_effect=node):
                return ops_alerts.check_opensearch_certs(now=self.now)

    def test_far_from_expiry_nothing_opens(self):
        self.assertEqual(self.run_check(), [])
        self.assertFalse(OpsAlert.objects.exists())

    def test_the_ca_alerts_thirty_days_out(self):
        self.run_check(ca_days=29)
        alert = OpsAlert.objects.get(key="cert:opensearch-ca")
        self.assertEqual(alert.kind, OpsAlertKind.CERT_EXPIRY)
        self.assertIn("expires in 29 days", alert.summary)

    def test_the_node_alerts_and_an_expired_one_says_so(self):
        self.run_check(node=lambda cfg: self.now - timedelta(days=1))
        self.assertIn("expired", OpsAlert.objects.get(key="cert:opensearch-node").summary)

    def test_a_node_that_no_longer_verifies_alerts(self):
        def fails(cfg):
            raise ssl.SSLCertVerificationError("certificate has expired")

        self.run_check(node=fails)
        self.assertTrue(OpsAlert.objects.filter(key="cert:opensearch-node").exists())

    def test_an_unreachable_node_is_reported_not_alerted(self):
        def down(cfg):
            raise ConnectionRefusedError

        skipped = self.run_check(node=down)
        self.assertEqual(len(skipped), 1)
        self.assertFalse(OpsAlert.objects.exists())

    def test_a_renewal_resolves_the_alert(self):
        self.run_check(ca_days=10)
        self.run_check(ca_days=1825)
        self.assertIsNotNone(OpsAlert.objects.get(key="cert:opensearch-ca").resolved_at)

    def test_no_tls_means_no_check(self):
        with override_settings(
            SEARCH_ENGINE={**settings.SEARCH_ENGINE, "OPENSEARCH_USE_SSL": False}
        ):
            self.assertEqual(len(ops_alerts.check_opensearch_certs()), 1)


# ── the tick ────────────────────────────────────────────────────────────


@override_settings(
    OPS_ALERT_EMAIL="ken@example.com",
    OPS_RUNBOOK_URL="https://docs.example/ALERTS.md",
    SEARCH_ENGINE={**settings.SEARCH_ENGINE, "OPENSEARCH_USE_SSL": False},
)
class TestTick(_NoReconnect, TestCase):
    def tick(self, *args):
        out = StringIO()
        call_command("ops_alert_tick", *args, stdout=out)
        return out.getvalue()

    def test_nothing_waiting_sends_nothing(self):
        self.assertIn("No alerts to send", self.tick())
        self.assertEqual(mail.outbox, [])

    def test_everything_waiting_goes_in_one_email_once(self):
        ops_alerts.raise_alert(
            OpsAlertKind.CRON_FAILED, "cron:x", "Cron x failed", {"service": "x"}
        )
        ops_alerts.raise_alert(OpsAlertKind.SECURITY, "security:y", "Account data exported")
        self.tick()
        self.tick()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["ken@example.com"])
        self.assertEqual(message.subject, "[Genos alert] Cron x failed (+1 more)")
        self.assertIn("Account data exported", message.body)
        self.assertIn("https://docs.example/ALERTS.md#cron-failed-or-missed", message.body)
        self.assertIn("https://docs.example/ALERTS.md#security-alerts", message.body)
        self.assertFalse(OpsAlert.objects.filter(notified_at__isnull=True).exists())

    def test_a_recurrence_of_a_sent_alert_is_not_resent(self):
        ops_alerts.raise_alert(OpsAlertKind.SERVER_ERRORS, "server_errors", "10+ server errors")
        self.tick()
        ops_alerts.raise_alert(OpsAlertKind.SERVER_ERRORS, "server_errors", "10+ server errors")
        self.tick()
        self.assertEqual(len(mail.outbox), 1)

    def test_only_listed_detail_reaches_the_email(self):
        ops_alerts.raise_alert(
            OpsAlertKind.SECURITY, "k", "s", {"caller": "user:1", "secret": "do-not-mail"}
        )
        self.tick()
        self.assertIn("user:1", mail.outbox[0].body)
        self.assertNotIn("do-not-mail", mail.outbox[0].body)
        self.assertNotIn("do-not-mail", mail.outbox[0].alternatives[0][0])

    def test_the_text_part_is_not_html_escaped_and_the_html_part_is(self):
        # A cron's error usually quotes something: CommandError's
        # "'gs://' names no bucket" arrived as "&#x27;gs://&#x27;".
        ops_alerts.raise_alert(
            OpsAlertKind.CRON_FAILED,
            "cron:x",
            "Cron x failed",
            {"service": "x", "error": "CommandError: 'gs://' <b>&"},
        )
        self.tick()
        message = mail.outbox[0]
        self.assertIn("Error: CommandError: 'gs://' <b>&", message.body)
        self.assertIn("&#x27;gs://&#x27; &lt;b&gt;&amp;", message.alternatives[0][0])

    def test_a_failed_send_fails_the_run_and_keeps_the_rows(self):
        ops_alerts.raise_alert(OpsAlertKind.SECURITY, "k", "s")
        with mock.patch(
            "origin.services.email.send_templated_email",
            side_effect=OSError("mail to ken@example.com refused"),
        ):
            with self.assertRaises(CommandError) as ctx:
                self.tick()
        self.assertNotIn("example.com", str(ctx.exception))
        self.assertTrue(OpsAlert.objects.filter(notified_at__isnull=True).exists())

    @override_settings(OPS_ALERT_EMAIL="")
    def test_no_recipient_fails_the_run_while_alerts_wait(self):
        ops_alerts.raise_alert(OpsAlertKind.SECURITY, "k", "s")
        with self.assertRaises(CommandError):
            self.tick()

    def test_a_dry_run_sends_and_stamps_nothing(self):
        ops_alerts.raise_alert(OpsAlertKind.SECURITY, "k", "s")
        self.assertIn("Would send", self.tick("--dry-run"))
        self.assertEqual(mail.outbox, [])
        self.assertTrue(OpsAlert.objects.filter(notified_at__isnull=True).exists())

    def test_the_tick_finds_missed_crons_and_records_its_own_heartbeat(self):
        CronHeartbeat.objects.create(
            name="email-notify", first_seen_at=timezone.now() - timedelta(hours=2)
        )
        self.tick("--heartbeat", ops_alerts.TICK_NAME)
        self.assertIn("email-notify", mail.outbox[0].subject)
        self.assertIsNotNone(CronHeartbeat.objects.get(name=ops_alerts.TICK_NAME).last_succeeded_at)


# ── the outside probe's endpoint ────────────────────────────────────────


@override_settings(OPS_HEALTH_TOKEN=TOKEN)
class TestAlertingHealth(TestCase):
    def get(self, token=TOKEN):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.get(ALERTING_URL, **headers)

    def beat(self, minutes_ago):
        CronHeartbeat.objects.create(
            name=ops_alerts.TICK_NAME,
            last_succeeded_at=timezone.now() - timedelta(minutes=minutes_ago),
        )

    def test_without_the_token_it_is_not_there(self):
        self.beat(1)
        self.assertEqual(self.get(token=None).status_code, 404)
        self.assertEqual(self.get(token="wrong").status_code, 404)

    @override_settings(OPS_HEALTH_TOKEN="")
    def test_unconfigured_it_is_not_there(self):
        self.assertEqual(self.get(token="").status_code, 404)
        self.assertEqual(self.get(token="anything").status_code, 404)

    def test_a_recent_tick_is_healthy(self):
        self.beat(4)
        res = self.get()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "ok")

    def test_a_stale_tick_is_unhealthy(self):
        self.beat(16)
        self.assertEqual(self.get().status_code, 503)

    def test_a_tick_that_never_ran_is_unhealthy(self):
        self.assertEqual(self.get().status_code, 503)

    def test_an_alert_stuck_unsent_is_unhealthy(self):
        # The tick runs, but can't send: no recipient, a dead Resend key.
        self.beat(1)
        ops_alerts.raise_alert(
            OpsAlertKind.SECURITY, "k", "s", now=timezone.now() - timedelta(minutes=20)
        )
        res = self.get()
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data["unsent_alerts"], 1)

    def test_the_answer_names_nothing(self):
        self.beat(1)
        ops_alerts.raise_alert(OpsAlertKind.SECURITY, "security:x:user:1", "Account data exported")
        self.assertEqual(set(self.get().data), {"status", "tick_age_seconds", "unsent_alerts"})


@override_settings(OPS_HEALTH_TOKEN=TOKEN)
class TestTheProbeIsWatchedBack(_NoReconnect, TestCase):
    """GitHub disables a schedule quietly; the tick has to notice."""

    def probe(self, token=TOKEN):
        return self.client.get(ALERTING_URL, HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_an_authorized_call_is_the_probe_heartbeat(self):
        self.probe()
        beat = CronHeartbeat.objects.get(name=ops_alerts.PROBE_NAME)
        self.assertIsNotNone(beat.last_succeeded_at)

    def test_a_refused_call_is_not(self):
        # Anyone can GET the URL; only the probe has the token.
        self.probe(token="guess")
        self.assertFalse(CronHeartbeat.objects.filter(name=ops_alerts.PROBE_NAME).exists())

    def test_a_silent_probe_alerts_and_its_return_resolves(self):
        self.probe()
        later = timezone.now() + timedelta(hours=3)
        self.assertEqual(ops_alerts.check_crons(now=later), [ops_alerts.PROBE_NAME])
        alert = OpsAlert.objects.get(key=f"cron:{ops_alerts.PROBE_NAME}")
        self.assertIn("uptime probe", alert.summary)
        self.probe()
        alert.refresh_from_db()
        self.assertIsNotNone(alert.resolved_at)
