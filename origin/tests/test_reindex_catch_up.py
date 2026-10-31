"""`opensearch_reindex --catch-up`: the incremental window reaches back to
the cron's last successful run, so a skipped or failed tick doesn't leave
what was written during it out of search for good.
"""

import datetime as dt
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from origin.models.common.ops_models import CronHeartbeat
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine import ingestion
from origin.search_engine.management.commands import opensearch_reindex
from origin.search_engine.models import SearchAclDirty
from origin.tests.test_base import BaseAPITestCase

CRON = "opensearch-reindexer"


def _bn(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


def _patch_command(test):
    patches = [
        mock.patch.object(opensearch_reindex, "get_client"),
        mock.patch.object(opensearch_reindex, "sweep_orphans", return_value={}),
        mock.patch.object(opensearch_reindex.Command, "_warn_on_schema_mismatch"),
        # The heartbeat write would close the test's transaction connection.
        mock.patch("origin.services.ops_alerts.close_old_connections"),
    ]
    for p in patches:
        p.start()
        test.addCleanup(p.stop)


class TestMissedTickIsCaughtUp(BaseAPITestCase):
    """A comment written while the cron wasn't running, end to end through
    the real task chunker and `_ingest_entity`; OpenSearch and the embedder
    are mocked."""

    def setUp(self):
        super().setUp()
        patches = [
            mock.patch.object(
                ingestion, "embed_texts", side_effect=lambda texts: [[0.1] * 8 for _ in texts]
            ),
            mock.patch.object(ingestion, "_bulk_index"),
            mock.patch.object(
                ingestion, "get_active_embedding_model_name", return_value="test-embed-v1"
            ),
            mock.patch.object(ingestion, "get_index_alias", return_value="rag-test"),
            mock.patch.object(ingestion, "get_client"),
            mock.patch.object(ingestion.os_helpers, "bulk", return_value=(0, [])),
        ]
        started = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        self.bulk = started[1]
        _patch_command(self)

        project = ProjectMaster.objects.create(team=self.team, project_name="P", owner=self.user)
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=project, title="Old task", content=_bn("body")
        )
        ingestion.ingest_all(entity_types=["task"])
        self.bulk.reset_mock()
        # Creating the members queued ACL marks, which would re-emit the
        # task whatever the window.
        SearchAclDirty.objects.all().delete()

        # Written an hour ago, during ticks that never ran; the last run
        # that succeeded was two hours ago.
        an_hour_ago = timezone.now() - dt.timedelta(hours=1)
        TaskMaster.objects.filter(pk=self.task.pk).update(
            ts_updated_at=timezone.now() - dt.timedelta(days=30)
        )
        TaskComments.objects.create(
            task=self.task, sender=self.user, comment_id=1, comment_body=_bn("missed comment")
        )
        TaskComments.objects.filter(task=self.task).update(ts_updated_at=an_hour_ago)
        CronHeartbeat.objects.create(
            name=CRON, last_succeeded_at=timezone.now() - dt.timedelta(hours=2)
        )

    def _indexed_ids(self):
        return {a["_id"] for call in self.bulk.call_args_list for a in call.args[0]}

    def _run(self, *extra):
        call_command(
            "opensearch_reindex",
            "--since-minutes",
            "11",
            "--entity-types",
            "task",
            "--heartbeat",
            CRON,
            *extra,
            stdout=mock.Mock(),
        )

    def test_catch_up_indexes_the_comment_from_the_missed_window(self):
        self._run("--catch-up")
        self.assertIn(f"task:{self.task.task_id}:comment:1", self._indexed_ids())

    def test_the_fixed_window_alone_loses_it(self):
        self._run()
        self.assertNotIn(f"task:{self.task.task_id}:comment:1", self._indexed_ids())

    def test_the_run_records_its_success_so_the_next_window_starts_there(self):
        self._run("--catch-up")
        last_ok = CronHeartbeat.objects.get(name=CRON).last_succeeded_at
        self.assertGreater(last_ok, timezone.now() - dt.timedelta(minutes=1))


class TestCatchUpWindow(TestCase):
    """Which `since` the command hands to `ingest_all`."""

    def setUp(self):
        _patch_command(self)
        p = mock.patch.object(
            opensearch_reindex, "ingest_all", return_value=ingestion.IngestionStats()
        )
        self.ingest_all = p.start()
        self.addCleanup(p.stop)

    def _since(self, *args):
        call_command("opensearch_reindex", *args, stdout=mock.Mock())
        return self.ingest_all.call_args.kwargs["since"]

    def _catch_up(self):
        return self._since("--since-minutes", "11", "--catch-up", "--heartbeat", CRON)

    def test_an_old_success_widens_the_window_to_it_minus_the_overlap(self):
        last_ok = timezone.now() - dt.timedelta(hours=5)
        CronHeartbeat.objects.create(name=CRON, last_succeeded_at=last_ok)
        self.assertEqual(self._catch_up(), last_ok - dt.timedelta(minutes=11))

    def test_a_recent_success_moves_the_window_only_by_the_gap(self):
        last_ok = timezone.now() - dt.timedelta(minutes=1)
        CronHeartbeat.objects.create(name=CRON, last_succeeded_at=last_ok)
        self.assertEqual(self._catch_up(), last_ok - dt.timedelta(minutes=11))

    def test_a_success_stamped_in_the_future_keeps_the_fixed_window(self):
        # Clock skew between containers must not shrink the window.
        CronHeartbeat.objects.create(
            name=CRON, last_succeeded_at=timezone.now() + dt.timedelta(hours=1)
        )
        since = self._catch_up()
        expected = timezone.now() - dt.timedelta(minutes=11)
        self.assertLess(abs((since - expected).total_seconds()), 5)

    def test_no_success_yet_keeps_the_fixed_window(self):
        since = self._catch_up()
        expected = timezone.now() - dt.timedelta(minutes=11)
        self.assertLess(abs((since - expected).total_seconds()), 5)

    def test_another_services_heartbeat_is_not_used(self):
        CronHeartbeat.objects.create(
            name="email-digest", last_succeeded_at=timezone.now() - dt.timedelta(hours=5)
        )
        since = self._catch_up()
        self.assertGreater(since, timezone.now() - dt.timedelta(minutes=12))

    def test_without_the_flag_an_old_success_is_ignored(self):
        CronHeartbeat.objects.create(
            name=CRON, last_succeeded_at=timezone.now() - dt.timedelta(hours=5)
        )
        since = self._since("--since-minutes", "11", "--heartbeat", CRON)
        self.assertGreater(since, timezone.now() - dt.timedelta(minutes=12))

    def test_catch_up_needs_a_heartbeat(self):
        with self.assertRaises(CommandError):
            self._since("--since-minutes", "11", "--catch-up")
        self.ingest_all.assert_not_called()

    def test_catch_up_needs_since_minutes(self):
        with self.assertRaises(CommandError):
            self._since("--catch-up", "--heartbeat", CRON)
        self.ingest_all.assert_not_called()
