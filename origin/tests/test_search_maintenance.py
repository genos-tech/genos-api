"""Tests for `origin.search_engine.maintenance` + the
`opensearch_maintain` command.

OpenSearch is never touched: `maintenance.get_client` is patched with a
MagicMock whose `indices.stats` returns a canned payload. What's under
test is the decision logic — threshold gating, force/dry-run/compaction
modes, and the two "must not red the cron" paths (missing alias, client
timeout), which have to stay below ERROR or the CronCommand tripwire
turns a healthy run into a failed one.
"""

from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from opensearchpy.exceptions import ConnectionTimeout

from origin.search_engine import maintenance


def _stats_payload(docs=1000, deleted=0, store=10_000, segments=5):
    return {
        "_all": {
            "primaries": {
                "docs": {"count": docs, "deleted": deleted},
                "store": {"size_in_bytes": store},
                "segments": {"count": segments},
            }
        }
    }


class MaintenanceTestCase(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.client = mock.MagicMock()
        self.client.indices.exists_alias.return_value = True
        self.client.indices.stats.return_value = _stats_payload()
        p1 = mock.patch.object(maintenance, "get_client", return_value=self.client)
        p2 = mock.patch.object(maintenance, "get_index_alias", return_value="test-alias")
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)


class TestMaintainIndex(MaintenanceTestCase):
    def test_skips_below_threshold(self):
        # 2% deleted < the 5% default — stats only, no merge.
        self.client.indices.stats.return_value = _stats_payload(docs=980, deleted=20)

        report = maintenance.maintain_index()

        self.assertEqual(report["action"], "skipped_below_threshold")
        self.assertEqual(report["before"]["deleted_ratio"], 0.02)
        self.assertIsNone(report["after"])
        self.client.indices.forcemerge.assert_not_called()

    def test_expunges_at_threshold(self):
        # 10% deleted >= 5% — expunge merge runs, after-stats collected.
        self.client.indices.stats.return_value = _stats_payload(docs=900, deleted=100)

        report = maintenance.maintain_index()

        self.assertEqual(report["action"], "expunge_deletes")
        self.assertIsNotNone(report["after"])
        self.client.indices.forcemerge.assert_called_once_with(
            index="test-alias",
            request_timeout=maintenance.MERGE_REQUEST_TIMEOUT_S,
            only_expunge_deletes=True,
        )

    def test_force_merges_below_threshold(self):
        self.client.indices.stats.return_value = _stats_payload(docs=1000, deleted=0)

        report = maintenance.maintain_index(force=True)

        self.assertEqual(report["action"], "expunge_deletes")
        self.client.indices.forcemerge.assert_called_once()

    def test_dry_run_never_merges(self):
        self.client.indices.stats.return_value = _stats_payload(docs=100, deleted=900)

        report = maintenance.maintain_index(dry_run=True)

        self.assertEqual(report["action"], "skipped_dry_run")
        self.assertEqual(report["before"]["deleted_ratio"], 0.9)
        self.client.indices.forcemerge.assert_not_called()

    def test_max_num_segments_compaction(self):
        report = maintenance.maintain_index(max_num_segments=1)

        self.assertEqual(report["action"], "full_merge")
        self.client.indices.forcemerge.assert_called_once_with(
            index="test-alias",
            request_timeout=maintenance.MERGE_REQUEST_TIMEOUT_S,
            max_num_segments=1,
        )

    def test_missing_alias_warns_but_does_not_error(self):
        self.client.indices.exists_alias.return_value = False

        with self.assertLogs(maintenance.log, level="WARNING") as captured:
            report = maintenance.maintain_index()

        self.assertEqual(report["action"], "index_missing")
        self.assertIsNone(report["before"])
        self.client.indices.stats.assert_not_called()
        self.client.indices.forcemerge.assert_not_called()
        # WARNING only — an ERROR would trip the CronCommand tripwire.
        self.assertFalse(any(r.levelname == "ERROR" for r in captured.records))

    def test_merge_timeout_warns_but_does_not_raise(self):
        self.client.indices.stats.return_value = _stats_payload(docs=500, deleted=500)
        self.client.indices.forcemerge.side_effect = ConnectionTimeout(
            "TIMEOUT", "timed out", Exception()
        )

        with self.assertLogs(maintenance.log, level="WARNING") as captured:
            report = maintenance.maintain_index()

        self.assertEqual(report["action"], "merge_timed_out")
        self.assertIsNone(report["after"])
        self.assertFalse(any(r.levelname == "ERROR" for r in captured.records))

    def test_zero_doc_index_has_zero_ratio(self):
        self.client.indices.stats.return_value = _stats_payload(docs=0, deleted=0)

        report = maintenance.maintain_index()

        self.assertEqual(report["action"], "skipped_below_threshold")
        self.assertEqual(report["before"]["deleted_ratio"], 0.0)


class TestCommand(MaintenanceTestCase):
    def _call(self, *args):
        out = StringIO()
        with mock.patch(
            "origin.search_engine.management.commands.opensearch_maintain.get_client",
            return_value=self.client,
        ):
            call_command("opensearch_maintain", *args, stdout=out)
        return out.getvalue()

    def test_unreachable_cluster_fails_loud(self):
        self.client.ping.return_value = False
        with mock.patch(
            "origin.search_engine.management.commands.opensearch_maintain.get_client",
            return_value=self.client,
        ):
            with self.assertRaises(CommandError):
                call_command("opensearch_maintain")
        self.client.indices.forcemerge.assert_not_called()

    def test_dry_run_prints_stats(self):
        self.client.ping.return_value = True
        self.client.indices.stats.return_value = _stats_payload(docs=900, deleted=100)

        out = self._call("--dry-run")

        self.assertIn("skipped_dry_run", out)
        self.assertIn('"deleted_ratio": 0.1', out)
        self.client.indices.forcemerge.assert_not_called()

    def test_threshold_flag_reaches_module(self):
        self.client.ping.return_value = True
        self.client.indices.stats.return_value = _stats_payload(docs=980, deleted=20)

        out = self._call("--min-deleted-ratio", "0.01")

        self.assertIn("expunge_deletes", out)
        self.client.indices.forcemerge.assert_called_once()
