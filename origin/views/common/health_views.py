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
