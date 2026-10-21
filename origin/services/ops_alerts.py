"""Alerts that reach a person (MIP-772).

Everything that should page Ken becomes an `OpsAlert` row. The
`ops_alert_tick` cron mails the unsent ones through Resend, all of them in
one message, at most once every five minutes. The sources:

- `alert.*` security events (`security_events` calls `raise_security_alert`);
- a cron that failed or stopped succeeding (`CronHeartbeat`, written by
  `HeartbeatCommand`; `check_crons` finds the silent ones);
- a burst of server errors (`ServerErrorBurstMiddleware`);
- the OpenSearch certificates nearing expiry (`check_opensearch_certs`).

Uptime, the public certificates and the domain are checked from outside
Railway by genos-frontend's `uptime.yml`, which also watches this pipeline
through `AlertingHealthView`: a stack that is down can't report itself.

**One incident, one message.** A row is keyed by what makes two alerts the
same incident. While it is open, a recurrence updates it instead of adding
one. A cron or certificate that fires again within `REOPEN_WINDOW` of
resolving reopens the same row, already sent, so a flapping cron doesn't
mail on every flap. Nothing mails a resolution, so the last word was
"failing" and stays true. A security alert or error burst after the last
one went quiet is a new incident, and mails.

**Nothing here raises into its caller.** A request or a cron must not fail
because an alert couldn't be written; a failed write logs a WARNING (not
ERROR, which the cron tripwire treats as a failed run).
"""

from __future__ import annotations

import base64
import binascii
import logging
import socket
import ssl
import time
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from apis.request_id import describe_exception
from origin.models.common.ops_models import CronHeartbeat, OpsAlert, OpsAlertKind
from origin.services import throttles

logger = logging.getLogger(__name__)

# The service that sends the alerts. It can't alert about itself, so
# `AlertingHealthView` reports its heartbeat to the outside probe instead.
TICK_NAME = "ops-alert-tick"

# How long each cron may go without a successful run before it counts as
# missed: its interval, plus enough slack that one skipped or slow run
# doesn't page. Keyed by the Railway service name its start command passes
# as `--heartbeat`; `test_ops_alerts` holds every railway-*.toml to it.
CRON_SCHEDULES: dict[str, timedelta] = {
    # */5
    "email-notify": timedelta(minutes=20),
    "reminder-tick": timedelta(minutes=20),
    "webhook-deliver": timedelta(minutes=20),
    # */10
    "opensearch-reindexer": timedelta(minutes=35),
    # hourly
    "agent-digest": timedelta(hours=3),
    "agent-judge-sampler": timedelta(hours=3),
    "email-digest": timedelta(hours=3),
    # daily
    "ai-cost-dashboard-daily": timedelta(hours=26),
    "demo-user-cleanup": timedelta(hours=26),
    "opensearch-maintenance": timedelta(hours=26),
    "security-event-prune": timedelta(hours=26),
    # weekly, monthly
    "ai-cost-dashboard-weekly": timedelta(days=7, hours=3),
    "ai-cost-dashboard-monthly": timedelta(days=32),
}

# A resolved incident that fires again this soon is the same incident.
REOPEN_WINDOW = timedelta(hours=6)

# Security alerts and error bursts are events, not states: nothing reports
# them over. They resolve after this long without a recurrence.
QUIET_PERIOD = timedelta(hours=1)
_POINT_KINDS = (OpsAlertKind.SECURITY, OpsAlertKind.SERVER_ERRORS)

# Runbook section for each kind, under OPS_RUNBOOK_URL.
RUNBOOK_ANCHORS = {
    OpsAlertKind.SECURITY: "security-alerts",
    OpsAlertKind.CRON_FAILED: "cron-failed-or-missed",
    OpsAlertKind.CRON_MISSED: "cron-failed-or-missed",
    OpsAlertKind.SERVER_ERRORS: "server-errors",
    OpsAlertKind.CERT_EXPIRY: "certificate-expiring",
}

_SUMMARY_MAX = 255
_ERROR_MAX = 255


def runbook_url(kind: str) -> str:
    base = getattr(settings, "OPS_RUNBOOK_URL", "")
    anchor = RUNBOOK_ANCHORS.get(kind, "")
    return f"{base}#{anchor}" if base and anchor else base


# ── Incidents ───────────────────────────────────────────────────────────


def _upsert(kind, key, summary, detail, now) -> OpsAlert:
    alert = OpsAlert.objects.select_for_update().filter(key=key, resolved_at__isnull=True).first()
    if alert is None and kind in _POINT_KINDS:
        # A new export or burst after the last one went quiet is news.
        return OpsAlert.objects.create(
            key=key,
            kind=kind,
            summary=summary[:_SUMMARY_MAX],
            detail=detail,
            opened_at=now,
            last_seen_at=now,
        )
    if alert is None:
        alert = (
            OpsAlert.objects.select_for_update()
            .filter(key=key, resolved_at__gte=now - REOPEN_WINDOW)
            .order_by("-resolved_at")
            .first()
        )
        if alert is None:
            return OpsAlert.objects.create(
                key=key,
                kind=kind,
                summary=summary[:_SUMMARY_MAX],
                detail=detail,
                opened_at=now,
                last_seen_at=now,
            )
    # `kind` too: a missed cron that then fails is one incident, now a failure.
    OpsAlert.objects.filter(pk=alert.pk).update(
        resolved_at=None,
        kind=kind,
        summary=summary[:_SUMMARY_MAX],
        detail=detail,
        last_seen_at=now,
        occurrences=F("occurrences") + 1,
    )
    alert.refresh_from_db()
    return alert


def raise_alert(kind: str, key: str, summary: str, detail: dict | None = None, *, now=None):
    """Open the incident `key`, or record a recurrence of the open one.
    Returns the row, or None if it couldn't be written."""
    now = now or timezone.now()
    for attempt in (1, 2):
        try:
            # A savepoint, so a failed write can't poison the caller's
            # transaction.
            with transaction.atomic():
                return _upsert(kind, key, summary, detail or {}, now)
        except IntegrityError:
            # Two processes opened the same key at once; the second
            # attempt finds the other's row and updates it.
            if attempt == 2:
                logger.warning("ops.alert write lost a race twice key=%s", key)
        except Exception as exc:
            logger.warning("ops.alert write failed key=%s: %s", key, describe_exception(exc))
            return None
    return None


def resolve_alert(key: str, *, now=None) -> int:
    """Close the open incident `key`, if there is one."""
    try:
        return OpsAlert.objects.filter(key=key, resolved_at__isnull=True).update(
            resolved_at=now or timezone.now()
        )
    except Exception as exc:
        logger.warning("ops.alert resolve failed key=%s: %s", key, describe_exception(exc))
        return 0


def resolve_quiet(*, now=None) -> int:
    """Close the event-like incidents that have gone quiet. Only once
    they've been sent: an alert must not resolve before anyone heard of it."""
    now = now or timezone.now()
    return OpsAlert.objects.filter(
        kind__in=_POINT_KINDS,
        resolved_at__isnull=True,
        notified_at__isnull=False,
        last_seen_at__lt=now - QUIET_PERIOD,
    ).update(resolved_at=now)


# ── Security alerts ─────────────────────────────────────────────────────

_SECURITY_SUMMARIES = {
    "alert.export": "Account data exported",
    "alert.forbidden_burst": "Burst of refused requests",
}


def raise_security_alert(event) -> None:
    """Page on an `alert.*` SecurityEvent. One incident per alert type
    and caller, so a scan that trips the burst alert every minute for an
    hour is one message."""
    who = (
        f"key:{event.api_key_id}"
        if event.api_key_id
        else f"user:{event.actor_id}"
        if event.actor_id
        else f"ip:{event.ip or '-'}"
    )
    meta = event.metadata or {}
    summary = _SECURITY_SUMMARIES.get(event.action, event.action)
    if "refused" in meta:
        summary = f"{summary}: {meta['refused']} in {meta.get('window_seconds', '?')} s"
    raise_alert(
        OpsAlertKind.SECURITY,
        f"security:{event.action}:{who}",
        f"{summary} ({who})",
        {
            "action": event.action,
            "security_event_id": event.event_id,
            "caller": who,
            "source": event.source,
            "ip": event.ip or "",
            **{k: meta[k] for k in ("path", "method") if k in meta},
        },
    )


# ── Cron heartbeats ─────────────────────────────────────────────────────


def _beat(name: str, now, **fields) -> None:
    # The run may have just held its connection idle for minutes.
    close_old_connections()
    try:
        if not CronHeartbeat.objects.filter(name=name).update(**fields):
            CronHeartbeat.objects.create(name=name, first_seen_at=now, **fields)
    except Exception as exc:
        # Most likely the cron deployed before backend-django migrated.
        logger.warning("cron.heartbeat write failed name=%s: %s", name, describe_exception(exc))


def cron_started(name: str) -> None:
    now = timezone.now()
    _beat(name, now, last_started_at=now)


def cron_succeeded(name: str) -> None:
    now = timezone.now()
    _beat(name, now, last_succeeded_at=now)
    resolve_alert(f"cron:{name}", now=now)


def cron_failed(name: str, error: str) -> None:
    now = timezone.now()
    error = (error or "")[:_ERROR_MAX]
    _beat(name, now, last_failed_at=now, last_error=error)
    raise_alert(
        OpsAlertKind.CRON_FAILED,
        f"cron:{name}",
        f"Cron {name} failed",
        {"service": name, "error": error},
        now=now,
    )


def check_crons(*, now=None) -> list[str]:
    """Open a `cron:<name>` incident for each cron that has gone longer
    than its schedule allows without succeeding. Returns their names.

    A cron that has never succeeded is measured from its first start. One
    that has never started has no row and isn't checked: it hasn't been
    deployed with `--heartbeat` yet.
    """
    now = now or timezone.now()
    overdue = []
    for beat in CronHeartbeat.objects.filter(name__in=CRON_SCHEDULES):
        since = beat.last_succeeded_at or beat.first_seen_at
        if now - since <= CRON_SCHEDULES[beat.name]:
            continue
        overdue.append(beat.name)
        never = beat.last_succeeded_at is None
        raise_alert(
            OpsAlertKind.CRON_MISSED,
            f"cron:{beat.name}",
            (
                f"Cron {beat.name} has never succeeded"
                if never
                else f"Cron {beat.name} hasn't succeeded for {_age(now - since)}"
            ),
            {
                "service": beat.name,
                "last_succeeded_at": since.isoformat() if not never else "",
                "last_started_at": beat.last_started_at.isoformat() if beat.last_started_at else "",
                "last_error": beat.last_error,
            },
            now=now,
        )
    return overdue


def _age(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


# ── 5xx bursts ──────────────────────────────────────────────────────────

_5XX_WINDOW = 60

# Answered on purpose and already logged: 501, and 503, which this app
# sends when a dependency is missing or down (no GIF key, the AI
# provider, the health check's database). A dead database is the uptime
# probe's to report.
_DELIBERATE_5XX = {501, 503}


def server_error_burst_threshold() -> int:
    return int(getattr(settings, "OPS_5XX_BURST", 10))


def note_server_error(request, status: int) -> None:
    """Count one 5xx answer; open the `server_errors` incident when a
    minute's count reaches the threshold. Counted in the shared throttle
    cache, so it adds up across workers; with the cache down it counts
    nothing and the request is unaffected."""
    key = f"ops5xx:{int(time.time() // _5XX_WINDOW)}"
    try:
        cache = throttles.throttle_cache()
        count = 1 if cache.add(key, 1, _5XX_WINDOW * 2) else cache.incr(key)
    except throttles.CACHE_ERRORS as exc:
        if not isinstance(exc, throttles.CacheDown):
            throttles._trip_breaker()
        return
    except Exception:
        return
    if count != server_error_burst_threshold():
        return
    logger.warning(
        "ops.alert server_errors count=%s window_seconds=%s status=%s path=%s",
        count,
        _5XX_WINDOW,
        status,
        request.path,
    )
    raise_alert(
        OpsAlertKind.SERVER_ERRORS,
        "server_errors",
        f"{count}+ server errors in {_5XX_WINDOW} s",
        {
            "threshold": count,
            "window_seconds": _5XX_WINDOW,
            "status": status,
            "path": request.path,
            "method": request.method,
        },
    )


class ServerErrorBurstMiddleware:
    """Counts every 5xx the app answers. Near the top of MIDDLEWARE so it
    sees an unhandled exception after Django has turned it into a 500."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        status = response.status_code
        if status >= 500 and status not in _DELIBERATE_5XX:
            note_server_error(request, status)
        return response


# ── OpenSearch certificates ─────────────────────────────────────────────


def cert_warn_days() -> int:
    return int(getattr(settings, "OPS_CERT_WARN_DAYS", 30))


def _ca_not_after(ca_b64: str) -> datetime | None:
    from cryptography import x509  # noqa: PLC0415

    try:
        pem = base64.b64decode(ca_b64, validate=True)
        return x509.load_pem_x509_certificate(pem).not_valid_after_utc
    except (binascii.Error, ValueError):
        return None


def _node_not_after(cfg) -> datetime:
    """The node certificate's expiry, read off a verified handshake.
    Raises `ssl.SSLCertVerificationError` when it no longer verifies."""
    from origin.search_engine.opensearch_client import _ssl_context  # noqa: PLC0415

    host, port = cfg["OPENSEARCH_HOST"], int(cfg["OPENSEARCH_PORT"])
    context = _ssl_context(cfg.get("OPENSEARCH_CA_CERT", ""))
    with socket.create_connection((host, port), timeout=5) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=UTC)


def _check_expiry(key: str, label: str, not_after: datetime, now) -> None:
    days = (not_after - now).days
    if days > cert_warn_days():
        resolve_alert(key, now=now)
        return
    raise_alert(
        OpsAlertKind.CERT_EXPIRY,
        key,
        (
            f"{label} expired on {not_after:%Y-%m-%d}"
            if days < 0
            else f"{label} expires in {days} days ({not_after:%Y-%m-%d})"
        ),
        {"certificate": label, "not_after": not_after.isoformat(), "days_left": days},
        now=now,
    )


def check_opensearch_certs(*, now=None) -> list[str]:
    """Alert `OPS_CERT_WARN_DAYS` before the OpenSearch CA or node
    certificate expires (`make-certs.sh` makes both, for five years).

    Returns what it couldn't check, for the run's output. An unreachable
    node isn't an alert here: the reindexer's failures already say so.
    """
    now = now or timezone.now()
    cfg = settings.SEARCH_ENGINE
    if not cfg.get("OPENSEARCH_USE_SSL"):
        return ["OpenSearch certificates: OPENSEARCH_USE_SSL is off on this service"]
    skipped = []
    ca_b64 = cfg.get("OPENSEARCH_CA_CERT", "")
    if ca_b64:
        ca_expiry = _ca_not_after(ca_b64)
        if ca_expiry is None:
            skipped.append("OpenSearch CA: OPENSEARCH_CA_CERT is not a base64 PEM certificate")
        else:
            _check_expiry("cert:opensearch-ca", "OpenSearch CA certificate", ca_expiry, now)
    try:
        node_expiry = _node_not_after(cfg)
    except ssl.SSLCertVerificationError as exc:
        raise_alert(
            OpsAlertKind.CERT_EXPIRY,
            "cert:opensearch-node",
            "OpenSearch node certificate no longer verifies",
            {
                "certificate": "OpenSearch node certificate",
                "error": getattr(exc, "verify_message", "") or "",
            },
            now=now,
        )
    except Exception as exc:
        skipped.append(f"OpenSearch node certificate: {describe_exception(exc)}")
    else:
        _check_expiry("cert:opensearch-node", "OpenSearch node certificate", node_expiry, now)
    return skipped
