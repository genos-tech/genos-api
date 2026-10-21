"""Operational alerting state (MIP-772).

`CronHeartbeat` is one row per scheduled job: when it last started,
succeeded and failed. A job writes it only when its start command passes
`--heartbeat <name>` (`CronCommand`), so a run started some other way (the
demo seeder calls `opensearch_reindex` in-process) can't stand in for the
cron.

`OpsAlert` is the delivery outbox for everything that should reach a
person: security alerts, failed or missed crons, bursts of server errors,
an expiring certificate. One row is one incident. While it is open, a
recurrence updates the row instead of adding one, and `ops_alert_tick`
mails each row once. That is what keeps an outage to one message.

Alerts live here rather than on `SecurityEvent` because that table is
append-only, and an outbox has to record what it has sent.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q
from django.utils import timezone


class CronHeartbeat(models.Model):
    # The Railway service name (`opensearch-reindexer`), which is what the
    # dashboard shows and what the alert names.
    name = models.CharField(max_length=64, primary_key=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_started_at = models.DateTimeField(null=True, blank=True)
    last_succeeded_at = models.DateTimeField(null=True, blank=True)
    last_failed_at = models.DateTimeField(null=True, blank=True)
    # What the last failure was, without its data: the exception's type,
    # or the template of the first ERROR line, never a formatted message.
    last_error = models.CharField(max_length=255, blank=True, default="")

    def __str__(self) -> str:
        return self.name


class OpsAlertKind(models.TextChoices):
    SECURITY = "security", "Security alert"
    CRON_FAILED = "cron_failed", "Cron failed"
    CRON_MISSED = "cron_missed", "Cron missed"
    SERVER_ERRORS = "server_errors", "Server errors"
    CERT_EXPIRY = "cert_expiry", "Certificate expiring"


class OpsAlert(models.Model):
    # What makes two alerts the same incident: `cron:opensearch-reindexer`,
    # `security:alert.export:<actor>`, `server_errors`.
    key = models.CharField(max_length=128)
    kind = models.CharField(max_length=24, choices=OpsAlertKind.choices)
    summary = models.CharField(max_length=255)
    detail = models.JSONField(default=dict, blank=True)
    opened_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    occurrences = models.PositiveIntegerField(default=1)
    resolved_at = models.DateTimeField(null=True, blank=True)
    # Null until a message about it has gone out.
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["opened_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["key"],
                condition=Q(resolved_at__isnull=True),
                name="opsalert_one_open_per_key",
            ),
        ]
        indexes = [
            models.Index(
                fields=["opened_at"],
                condition=Q(notified_at__isnull=True),
                name="opsalert_unsent",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.key} ({'resolved' if self.resolved_at else 'open'})"
