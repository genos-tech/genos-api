"""MIP-2374: the reindex that sign-up, starter seed and team join kick off covers ONE team.

- the chunkers' `team_id` filter gives exactly the chunks a full pass writes for that team;
- `ingest_all(team_id=...)` and `opensearch_reindex --team-id` walk nothing else (no window, no
  orphan sweep, no ACL-mark claim);
- `team_reindex` starts it after the commit, in a child process, deduped and capped;
- the three callers pass their team.
"""

from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from origin.models.chat.unified_models import Channel, Message
from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine import ingestion, team_reindex
from origin.search_engine.chunkers.base import CHAT_TYPE_PM
from origin.search_engine.chunkers.chat_chunker import iter_all_chat_chunks
from origin.search_engine.chunkers.task_chunker import iter_task_chunks
from origin.search_engine.management.commands import opensearch_reindex
from origin.search_engine.models import RagChunk
from origin.tests.test_base import BaseAPITestCase


def _bn(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


def _snapshot(entities):
    """{(type, entity id): sorted (chunk id, text hash, metadata fingerprint)}."""
    out = {}
    for e in entities:
        out[(e.entity_type, e.entity_id)] = sorted(
            (
                c.chunk_id,
                ingestion.hash_text(c.search_text or ""),
                ingestion._meta_fingerprint(c),
            )
            for c in e.chunks
        )
    return out


def _of_team(snapshot, entities, team_id):
    keep = {
        (e.entity_type, e.entity_id)
        for e in entities
        if e.chunks and str(e.chunks[0].team_id) == str(team_id)
    }
    return {k: v for k, v in snapshot.items() if k in keep}


class TeamScopedFixture(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.team_b = TeamMaster.objects.create(
            team_name="Team B", team_email="b@example.com", owner=self.user2
        )
        for team, owner in ((self.team, self.user), (self.team_b, self.user2)):
            project = ProjectMaster.objects.create(
                team=team, project_name=f"P {team.team_name}", owner=owner
            )
            ProjectMembers.objects.create(team=team, project=project, attendee=owner)
            pm = Channel.objects.get(project=project, kind=CHAT_TYPE_PM)
            for seq in (1, 2, 3):
                Message.objects.create(
                    channel=pm, sender=owner, seq=seq, body=_bn(f"hello {team.team_name} {seq}")
                )
            TaskMaster.objects.create(
                team=team,
                project=project,
                title=f"Task of {team.team_name}",
                content=_bn("some body text"),
                reporter=owner,
            )


class TestChunkerTeamFilter(TeamScopedFixture):
    def _check(self, iterator):
        full = list(iterator())
        scoped = list(iterator(team_id=str(self.team.team_id)))
        self.assertTrue(scoped, "the fixture must produce chunks for the team")
        expected = _of_team(_snapshot(full), full, self.team.team_id)
        self.assertEqual(_snapshot(scoped), expected)
        self.assertNotEqual(len(full), len(scoped), "team B must exist in the full pass")

    def test_chat_chunks_equal_the_full_pass_for_the_team(self):
        self._check(iter_all_chat_chunks)

    def test_task_chunks_equal_the_full_pass_for_the_team(self):
        self._check(iter_task_chunks)


class TestIngestAllTeamScope(TeamScopedFixture):
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
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_writes_only_the_team_and_a_full_pass_then_finds_nothing_new_for_it(self):
        stats = ingestion.ingest_all(team_id=str(self.team.team_id))
        self.assertGreater(stats.chunks_new, 0)
        teams = set(RagChunk.objects.values_list("team_id", flat=True))
        self.assertEqual({str(t) for t in teams}, {str(self.team.team_id)})

        again = ingestion.ingest_all(team_id=str(self.team.team_id))
        self.assertEqual((again.chunks_new, again.chunks_changed), (0, 0))

        before = RagChunk.objects.filter(team_id=str(self.team.team_id)).count()
        ingestion.ingest_all()  # a full pass: team A is already there with the same hashes
        self.assertEqual(RagChunk.objects.filter(team_id=str(self.team.team_id)).count(), before)

    def test_refuses_what_a_team_pass_cannot_do(self):
        tid = str(self.team.team_id)
        for kwargs in (
            {"since": mock.sentinel.since},
            {"dirty": mock.sentinel.dirty},
            {"entity_types": ["conversation"]},
            {"entity_types": ["spotlight_answer"]},
        ):
            with self.assertRaises(ValueError):
                ingestion.ingest_all(team_id=tid, **kwargs)


class TestReindexCommandTeamMode(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        patches = {
            "client": mock.patch.object(opensearch_reindex, "get_client"),
            "sweep": mock.patch.object(opensearch_reindex, "sweep_orphans", return_value={}),
            "claim": mock.patch.object(opensearch_reindex.acl_dirty, "claim"),
            "ingest": mock.patch.object(opensearch_reindex, "ingest_all"),
            "warn": mock.patch.object(opensearch_reindex.Command, "_warn_on_schema_mismatch"),
        }
        self.m = {k: p.start() for k, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        self.m["ingest"].return_value = ingestion.IngestionStats()

    def test_runs_the_team_pass_only(self):
        tid = str(self.team.team_id)
        call_command("opensearch_reindex", team_id=tid, entity_types=["note"])
        self.m["ingest"].assert_called_once_with(team_id=tid, entity_types=["note"], dry_run=False)
        self.m["claim"].assert_not_called()
        self.m["sweep"].assert_not_called()

    def test_refuses_the_cron_flags_and_a_bad_id(self):
        tid = str(self.team.team_id)
        for extra in (
            {"since_minutes": 5},
            {"catch_up": True},
            {"repair": True},
            {"purge_orphans_only": True},
        ):
            with self.assertRaises(CommandError):
                call_command("opensearch_reindex", team_id=tid, **extra)
        with self.assertRaises(CommandError):
            call_command("opensearch_reindex", team_id="not-a-uuid")
        with self.assertRaises(CommandError):
            call_command("opensearch_reindex", team_id=tid, entity_types=["conversation"])
        self.m["ingest"].assert_not_called()


@override_settings(SEARCH_TEAM_REINDEX_MODE="subprocess", SEARCH_TEAM_REINDEX_MAX_PROCS=2)
class TestTeamReindexSpawner(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        team_reindex._RUNNING.clear()
        team_reindex._PENDING.clear()
        self.addCleanup(team_reindex._RUNNING.clear)
        self.addCleanup(team_reindex._PENDING.clear)
        self.tid = str(self.team.team_id)
        self.popen = mock.patch.object(team_reindex.subprocess, "Popen").start()
        self.addCleanup(mock.patch.stopall)
        # The reaper would block in wait(): replace its thread.
        self.thread = mock.patch.object(team_reindex.threading, "Thread").start()

    def test_spawns_only_after_the_commit_with_the_team_argv(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            team_reindex.request_team_reindex(self.tid)
        self.popen.assert_not_called()
        for cb in callbacks:
            cb()
        argv = self.popen.call_args.args[0]
        self.assertIn("--team-id", argv)
        self.assertEqual(argv[argv.index("--team-id") + 1], self.tid)
        self.assertNotIn("--since-minutes", argv)

    def test_same_team_in_flight_queues_one_rerun_and_merges_types(self):
        team_reindex._start(self.tid, ())
        team_reindex._start(self.tid, ("note",))
        team_reindex._start(self.tid, ("note_folder",))
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(team_reindex._PENDING[self.tid], {"note", "note_folder"})
        team_reindex._start(self.tid, ())  # a request for every lane widens the queued re-run
        self.assertIsNone(team_reindex._PENDING[self.tid])

    def test_cap_leaves_the_team_to_the_cron(self):
        for _ in range(3):
            team_reindex._start(
                str(
                    TeamMaster.objects.create(
                        team_name=f"t{_}", team_email=f"t{_}@example.com", owner=self.user
                    ).team_id
                ),
                (),
            )
        self.assertEqual(self.popen.call_count, 2)

    def test_spawn_failure_never_propagates(self):
        self.popen.side_effect = OSError("no fork")
        team_reindex._start(self.tid, ())
        self.assertNotIn(self.tid, team_reindex._RUNNING)

    def test_thread_mode_runs_the_team_pass(self):
        with override_settings(SEARCH_TEAM_REINDEX_MODE="thread"):
            team_reindex._start(self.tid, ())
        self.assertEqual(self.thread.call_args.kwargs["target"], team_reindex._run_in_thread)
        with (
            mock.patch.object(ingestion, "ingest_all") as ingest,
            mock.patch.object(team_reindex, "connection"),
        ):
            team_reindex._run_in_thread(self.tid, ("note",))
        ingest.assert_called_once_with(team_id=self.tid, entity_types=["note"])

    def test_off_mode_does_nothing(self):
        with override_settings(SEARCH_TEAM_REINDEX_MODE="off"):
            team_reindex._start(self.tid, ())
        self.popen.assert_not_called()
        self.thread.assert_not_called()

    def test_kick_off_passes_the_team_through(self):
        from origin.services import demo_seeder

        with mock.patch.object(team_reindex, "request_team_reindex") as request:
            demo_seeder.kick_off_demo_reindex(self.tid, entity_types=("note",))
        request.assert_called_once_with(self.tid, entity_types=("note",))
