"""Mail the alerts that are waiting to a person (MIP-772).

    python manage.py ops_alert_tick            # check, then send
    python manage.py ops_alert_tick --dry-run  # check and print, send nothing

Each run:

1. opens an alert for every cron that has gone too long without succeeding
   (`ops_alerts.check_crons`);
2. checks the OpenSearch certificates for expiry (one TLS handshake);
3. closes the security and server-error alerts that have gone quiet;
4. mails every alert not yet sent, in ONE message, to `OPS_ALERT_EMAIL`.

Runs every 5 minutes (railway-ops-alert-tick.toml). A failed send fails
the run and leaves the rows unsent, so the next tick tries again and the
outside probe (`AlertingHealthView`) sees them waiting.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.management.base import CommandError
from django.utils import timezone

from apis.request_id import describe_exception
from origin.management.cron_command import CronCommand
from origin.models.common.ops_models import OpsAlert, OpsAlertKind
from origin.services import ops_alerts

log = logging.getLogger(__name__)

# What an email may show of an alert's detail, in this order. Anything
# else stays in the row: an alert email is not the place for a path with
# an id in it from some future source nobody reviewed.
_FACTS = (
    ("service", "Service"),
    ("error", "Error"),
    ("last_error", "Last error"),
    ("last_succeeded_at", "Last success"),
    ("last_started_at", "Last start"),
    ("action", "Security action"),
    ("security_event_id", "SecurityEvent id"),
    ("caller", "Caller"),
    ("source", "Source"),
    ("ip", "IP"),
    ("method", "Method"),
    ("path", "Path"),
    ("status", "Status"),
    ("threshold", "Threshold"),
    ("certificate", "Certificate"),
    ("not_after", "Expires"),
    ("days_left", "Days left"),
)


def _facts(detail: dict) -> list[tuple[str, str]]:
    return [(label, str(detail[key])) for key, label in _FACTS if detail.get(key) not in (None, "")]


class Command(CronCommand):
    help = "Check crons and certificates, then mail any waiting ops alert."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Run the checks (they record alerts as usual), then print what "
                "would be sent instead of sending it."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        now = timezone.now()

        overdue = ops_alerts.check_crons(now=now)
        if overdue:
            self.stdout.write(f"Overdue crons: {', '.join(overdue)}")

        for skipped in ops_alerts.check_opensearch_certs(now=now):
            self.stdout.write(f"Not checked: {skipped}")

        if not dry_run:
            resolved = ops_alerts.resolve_quiet(now=now)
            if resolved:
                self.stdout.write(f"Resolved {resolved} quiet alert(s).")

        pending = list(OpsAlert.objects.filter(notified_at__isnull=True).order_by("opened_at"))
        if not pending:
            self.stdout.write("No alerts to send.")
            return
        self._send(pending, dry_run=dry_run, now=now)

    def _send(self, pending: list[OpsAlert], *, dry_run: bool, now) -> None:
        subject = _subject(pending)
        if dry_run:
            self.stdout.write(f"Would send: {subject}")
            for alert in pending:
                self.stdout.write(f"  - {alert.summary}")
            return
        to = settings.OPS_ALERT_EMAIL
        if not to:
            raise CommandError(
                f"{len(pending)} alert(s) waiting and OPS_ALERT_EMAIL is not set on this "
                "service, so nobody is told."
            )
        from origin.services.email import send_templated_email  # noqa: PLC0415

        try:
            send_templated_email(
                to=to,
                subject=subject,
                template_base="ops_alert",
                context={"alerts": [_context(alert) for alert in pending]},
            )
        except Exception as exc:
            # The type only: a mail error's text quotes the address (MIP-622).
            raise CommandError(
                f"Could not send {len(pending)} alert(s): {describe_exception(exc)}. "
                "They stay unsent; the next run tries again."
            ) from None
        OpsAlert.objects.filter(pk__in=[a.pk for a in pending]).update(notified_at=now)
        log.info("ops.alert sent count=%s keys=%s", len(pending), ",".join(a.key for a in pending))
        self.stdout.write(self.style.SUCCESS(f"Sent {len(pending)} alert(s)."))


def _subject(pending: list[OpsAlert]) -> str:
    first = pending[0].summary
    if len(pending) == 1:
        return f"[Genos alert] {first}"
    return f"[Genos alert] {first} (+{len(pending) - 1} more)"


def _context(alert: OpsAlert) -> dict:
    return {
        "state": "Resolved" if alert.resolved_at else "Open",
        "summary": alert.summary,
        "kind_label": OpsAlertKind(alert.kind).label,
        "opened_at": alert.opened_at,
        "last_seen_at": alert.last_seen_at,
        "occurrences": alert.occurrences,
        "facts": _facts(alert.detail or {}),
        "runbook": ops_alerts.runbook_url(alert.kind),
    }
