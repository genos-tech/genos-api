"""`GET /api/v2/health/` — the Railway deploy-readiness probe.

Two properties are load-bearing and worth pinning:

  * 200 WITHOUT any credentials — the prober has no JWT, so any
    accidental auth requirement (e.g. someone switching the view to the
    AuthenticatedAPIView base) would make every future deploy hang at
    "healthcheck failing" and never go live.

  * 503 when the database is unreachable — the check must mean "ready
    to serve", not "gunicorn is up", so a container with a broken
    DATABASE_URL never steals traffic from a working deployment.
"""

from unittest.mock import patch

from django.db.utils import OperationalError
from django.test import TestCase
from rest_framework.test import APIClient


class HealthEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_ok_without_authentication(self):
        res = self.client.get("/api/v2/health/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"status": "ok"})

    def test_ignores_a_garbage_authorization_header(self):
        # Empty authentication_classes: a stray/expired token must not
        # be parsed at all, let alone 401 the probe.
        self.client.credentials(HTTP_AUTHORIZATION="Bearer not-a-jwt")
        res = self.client.get("/api/v2/health/")
        self.assertEqual(res.status_code, 200)

    def test_503_when_database_is_unreachable(self):
        with patch(
            "origin.views.common.health_views.connections"
        ) as mock_connections:
            mock_connections.__getitem__.return_value.ensure_connection.side_effect = (
                OperationalError("db down")
            )
            res = self.client.get("/api/v2/health/")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data["status"], "unhealthy")
