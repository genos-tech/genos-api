"""OpenSearch capacity alerts (MIP-2385): stubbed node stats, no OpenSearch needed."""

from datetime import UTC, datetime
from unittest import mock

from django.test import TestCase, override_settings

from origin.models.common.ops_models import OpsAlert, OpsAlertKind
from origin.services import ops_alerts

MB = 2**20


def _stats(*, disk_used=10, heap=40, size=5120 * MB):
    return {
        "nodes": {
            "n1": {
                "fs": {
                    "total": {
                        "total_in_bytes": size,
                        "available_in_bytes": size * (100 - disk_used) // 100,
                    }
                },
                "jvm": {"mem": {"heap_used_percent": heap}},
            }
        }
    }


class FakeClient:
    def __init__(self, stats, docs=21_973):
        self._stats, self._docs = stats, docs
        self.nodes = mock.Mock()
        self.nodes.stats.side_effect = lambda **kw: self._stats

    def count(self, index):
        return {"count": self._docs}


@override_settings(
    OPS_OPENSEARCH_DISK_PERCENT=70,
    OPS_OPENSEARCH_HEAP_PERCENT=80,
    OPS_OPENSEARCH_HEAP_TICKS=3,
    OPS_OPENSEARCH_DOC_LIMIT=150_000,
)
class TestCapacity(TestCase):
    now = datetime(2027, 1, 1, tzinfo=UTC)

    def run_check(self, stats=None, docs=21_973):
        client = FakeClient(stats or _stats(), docs)
        with mock.patch("origin.search_engine.opensearch_client.get_client", return_value=client):
            return ops_alerts.check_opensearch_capacity(now=self.now)

    def keys(self):
        return set(OpsAlert.objects.filter(resolved_at__isnull=True).values_list("key", flat=True))

    def test_healthy_is_quiet(self):
        self.assertEqual(self.run_check(), [])
        self.assertFalse(OpsAlert.objects.exists())

    def test_disk_over_limit_alerts_and_recovery_resolves(self):
        self.run_check(_stats(disk_used=71))
        alert = OpsAlert.objects.get(key="capacity:disk")
        self.assertEqual(alert.kind, OpsAlertKind.OPENSEARCH_CAPACITY)
        self.assertIn("71%", alert.summary)
        self.run_check(_stats(disk_used=40))
        self.assertEqual(self.keys(), set())

    def test_disk_at_the_limit_is_quiet(self):
        self.run_check(_stats(disk_used=70))
        self.assertFalse(OpsAlert.objects.exists())

    def test_heap_needs_to_be_sustained(self):
        self.run_check(_stats(heap=90))
        self.run_check(_stats(heap=90))
        self.assertFalse(OpsAlert.objects.exists())
        self.run_check(_stats(heap=90))
        self.assertEqual(self.keys(), {"capacity:heap"})

    def test_heap_streak_survives_an_unreachable_cache(self):
        # ops-alert-tick has no REDIS_URL: the cache raises on every call (MIP-2385 review).
        # The streak must not depend on it, and each tick is a fresh process (DB only).
        with mock.patch(
            "origin.services.throttles.throttle_cache", side_effect=ConnectionError("redis")
        ):
            for _ in range(3):
                skipped = self.run_check(_stats(heap=90))
        self.assertEqual(skipped, [])
        self.assertEqual(self.keys(), {"capacity:heap"})

    def test_uncountable_streak_shows_as_not_checked(self):
        with mock.patch.object(ops_alerts, "_heap_streak", side_effect=RuntimeError("db down")):
            skipped = self.run_check(_stats(heap=90))
        self.assertTrue(any("streak could not be counted" in line for line in skipped))
        self.assertEqual(self.keys(), set())

    def test_a_heap_dip_resets_the_streak(self):
        for heap in (90, 90, 50, 90, 90):
            self.run_check(_stats(heap=heap))
        self.assertFalse(OpsAlert.objects.exists())

    def test_heap_alert_resolves_when_it_drops(self):
        for _ in range(3):
            self.run_check(_stats(heap=95))
        self.run_check(_stats(heap=50))
        self.assertEqual(self.keys(), set())

    def test_doc_count_over_limit_alerts(self):
        self.run_check(docs=150_001)
        self.assertEqual(self.keys(), {"capacity:docs"})
        self.run_check(docs=1000)
        self.assertEqual(self.keys(), set())

    def test_one_incident_per_resource_across_ticks(self):
        self.run_check(_stats(disk_used=80))
        self.run_check(_stats(disk_used=81))
        self.assertEqual(OpsAlert.objects.filter(key="capacity:disk").count(), 1)
        self.assertEqual(OpsAlert.objects.get(key="capacity:disk").occurrences, 2)

    def test_an_unreachable_node_is_reported_not_alerted(self):
        with mock.patch(
            "origin.search_engine.opensearch_client.get_client", side_effect=ConnectionError("x")
        ):
            skipped = ops_alerts.check_opensearch_capacity(now=self.now)
        self.assertEqual(len(skipped), 1)
        self.assertFalse(OpsAlert.objects.exists())

    def test_a_failing_count_does_not_hide_disk(self):
        client = FakeClient(_stats(disk_used=90))
        client.count = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch("origin.search_engine.opensearch_client.get_client", return_value=client):
            skipped = ops_alerts.check_opensearch_capacity(now=self.now)
        self.assertEqual(len(skipped), 1)
        self.assertEqual(self.keys(), {"capacity:disk"})
