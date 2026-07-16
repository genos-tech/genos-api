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

from django.db import connections
from django.db.utils import OperationalError
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


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
