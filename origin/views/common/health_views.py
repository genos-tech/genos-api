"""Deploy-readiness healthcheck.

Railway's zero-downtime deploys are gated on `healthcheckPath`
(railway.toml): the OLD deployment keeps serving until the new
container answers 200 here. Without it, traffic switches as soon as
the container starts — and this service's entrypoint runs
collectstatic, migrations, and OpenSearch index checks BEFORE gunicorn
listens, so every merge to main produced a minutes-long window where
the edge returned errors with no CORS headers (surfacing in the
browser as a CORS wall on every endpoint).

The DB ping makes the check mean "ready to serve", not just "gunicorn
is up": a container with a broken DATABASE_URL must never take traffic
from a working deployment.
"""

import hmac
import os
import re
from datetime import timedelta

from django.conf import settings
from django.db import connections
from django.db.utils import DatabaseError, OperationalError
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from origin.models.common.ops_models import CronHeartbeat, OpsAlert
from origin.services import ops_alerts


class HealthView(APIView):
    # Unauthenticated on purpose — the prober has no JWT. Empty
    # authentication_classes also skips token parsing entirely, so a
    # stray Authorization header can't 401 the probe.
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            connections["default"].ensure_connection()
        except OperationalError:
            return Response(
                {"status": "unhealthy", "db": "unreachable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({"status": "ok"})


class AlertingHealthView(APIView):
    """Whether the alert pipeline itself is alive (MIP-772).

    `ops_alert_tick` can't report that it stopped, so the outside uptime
    probe (genos-frontend's `uptime.yml`) asks here instead: 200 while the
    tick has succeeded within `_TICK_MAX_AGE`, 503 when it hasn't, or when
    an alert has waited longer than that to be sent. The probe failing is
    what then reaches Ken, by GitHub's own failed-run email.

    Each authorized call is also the probe's own heartbeat (`uptime-probe`
    in `CRON_SCHEDULES`): GitHub quietly disables a schedule, so the tick
    alerts when the probe stops asking. Each watches the other.

    Behind `OPS_HEALTH_TOKEN` because "alerting is down right now" is worth
    knowing to an attacker too. Without the right bearer token it answers
    404, as if the route didn't exist. The answer carries no ids or names.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    # Two ticks missed, plus the slack of a slow one.
    _TICK_MAX_AGE = timedelta(minutes=15)

    def get(self, request):
        expected = settings.OPS_HEALTH_TOKEN
        header = request.META.get("HTTP_AUTHORIZATION", "")
        presented = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
        if not expected or not hmac.compare_digest(presented.encode(), expected.encode()):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        now = timezone.now()
        ops_alerts.probe_seen(now=now)
        try:
            beat = CronHeartbeat.objects.filter(name=ops_alerts.TICK_NAME).first()
            stuck = OpsAlert.objects.filter(
                notified_at__isnull=True, opened_at__lt=now - self._TICK_MAX_AGE
            ).count()
        except DatabaseError:
            return Response(
                {"status": "unhealthy", "db": "unreachable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        last = beat.last_succeeded_at if beat else None
        age = int((now - last).total_seconds()) if last else None
        healthy = age is not None and age <= self._TICK_MAX_AGE.total_seconds() and not stuck
        return Response(
            {
                "status": "ok" if healthy else "unhealthy",
                "tick_age_seconds": age,
                "unsent_alerts": stuck,
            },
            status=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        )


# The bearer token a cron that runs outside Django sends (MIP-1858). Read here, at
# request time, and not in settings: the same value is set on genos-collab, and an
# empty one makes the endpoint answer 404.
EXTERNAL_CRON_TOKEN_ENV = "EXTERNAL_CRON_HEARTBEAT_TOKEN"


class ExternalCronHeartbeatView(APIView):
    """A cron that runs outside Django (genos-collab's orphan sweep) reports each run.

    `POST api/v2/health/cron/<name>/` with `{"phase": "started" | "succeeded" |
    "failed", "error": "<exception type>"}`. It records the same heartbeat as a
    `--heartbeat` command, so a run that stops is missed and alerts. Only the names
    in `ops_alerts.EXTERNAL_CRONS` are accepted. Behind a bearer token, else 404 as
    if the route did not exist. The failure label is kept only when it is a plain
    type name; anything else is stored as "unknown", so nothing a run carries can
    reach an alert.
    """

    authentication_classes = []
    permission_classes = [AllowAny]

    PHASES = ("started", "succeeded", "failed")
    _TYPE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,63}")

    def post(self, request, name):
        expected = os.environ.get(EXTERNAL_CRON_TOKEN_ENV, "").strip()
        header = request.META.get("HTTP_AUTHORIZATION", "")
        presented = header[len("Bearer ") :] if header.startswith("Bearer ") else ""
        if not expected or not hmac.compare_digest(presented.encode(), expected.encode()):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if name not in ops_alerts.EXTERNAL_CRONS:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        data = request.data if isinstance(request.data, dict) else {}
        phase = data.get("phase")
        if phase not in self.PHASES:
            return Response(
                {"detail": "phase must be started, succeeded or failed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if phase == "started":
            ops_alerts.cron_started(name)
        elif phase == "succeeded":
            ops_alerts.cron_succeeded(name)
        else:
            raw = data.get("error")
            label = raw if isinstance(raw, str) and self._TYPE_NAME.fullmatch(raw) else "unknown"
            ops_alerts.cron_failed(name, label)
        return Response({"status": "ok"}, status=status.HTTP_200_OK)
