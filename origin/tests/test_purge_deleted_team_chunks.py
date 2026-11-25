"""MIP-2384: chunks of a team that no longer exists are purged (sweep, command, delete hook).

OpenSearch is mocked as in `test_search_purge`; the RagChunk bookkeeping is real.
"""

import uuid
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError

from origin.models.common.team_models import TeamMaster
from origin.search_engine import purge
from origin.search_engine.models import AgentRun, RagChunk
from origin.tests.test_search_purge import PurgeTestCase


class DeadTeamTestCase(PurgeTestCase):
    def _dead_chunk(self, chunk_id, etype="conversation", team_id=None):
        dead = team_id or self.dead_team
        run = uuid.uuid4()
        return RagChunk.objects.create(
            chunk_id=chunk_id,
            entity_type=etype,
            entity_id=f"{etype}:{run}",
            chunk_type="test",
            team_id=dead,
            text_hash="h",
            embedding_model="m",
        )

    def setUp(self):
        super().setUp()
        self.dead_team = uuid.uuid4()  # no TeamMaster row
        # An agent run of a live team: its chunk is live, an unrelated dead-team chunk is not.
        self.live_run = AgentRun.objects.create(
            team_id=str(self.team.team_id), user_id=str(self.user.id), query="q"
        )
        self._chunk("live", "conversation", f"conversation:{self.live_run.run_id}")


class TestDeadTeamIds(DeadTeamTestCase):
    def test_only_teams_without_a_row_are_dead(self):
        self._dead_chunk("d1")
        self.assertEqual(purge.dead_team_ids(), [self.dead_team])

    def test_soft_closed_team_is_not_dead(self):
        TeamMaster.objects.filter(pk=self.team.pk).update(is_deleted=True)
        self.assertEqual(purge.dead_team_ids(), [])


class TestSweepDeadTeams(DeadTeamTestCase):
    def test_sweep_purges_chunks_of_a_deleted_team_even_when_the_run_exists(self):
        # The production case: the AgentRun row survived its team.
        run = AgentRun.objects.create(
            team_id=str(self.dead_team), user_id=str(self.user.id), query="q"
        )
        RagChunk.objects.create(
            chunk_id="d1",
            entity_type="conversation",
            entity_id=f"conversation:{run.run_id}",
            chunk_type="test",
            team_id=self.dead_team,
            text_hash="h",
            embedding_model="m",
        )
        self._dead_chunk("d2", "spotlight_answer")

        stats = purge.sweep_orphans()

        self.assertEqual(set(RagChunk.objects.values_list("chunk_id", flat=True)), {"live"})
        self.assertEqual(stats["dead_teams"], 1)
        self.assertEqual(stats["dead_team_chunks"], 2)
        self.assertEqual(stats["chunks_purged"], 2)
        self.assertEqual(self._bulk_deleted_ids(), {"d1", "d2"})

    def test_dry_run_counts_without_deleting(self):
        self._dead_chunk("d1")

        stats = purge.sweep_orphans(dry_run=True)

        self.assertEqual(stats["dead_team_chunks"], 1)
        self.assertEqual(stats["chunks_purged"], 1)
        self.assertEqual(RagChunk.objects.count(), 2)
        self.bulk_mock.assert_not_called()

    def test_failed_opensearch_delete_keeps_the_tracking_row(self):
        self._dead_chunk("d1")
        self.bulk_mock.side_effect = RuntimeError("opensearch down")

        with self.assertRaises(RuntimeError):
            purge.sweep_orphans()

        self.assertTrue(RagChunk.objects.filter(chunk_id="d1").exists())


class TestPurgeDeletedTeamChunksCommand(DeadTeamTestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("purge_deleted_team_chunks", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_is_the_default_and_prints_counts_only(self):
        self._dead_chunk("d1")
        self._dead_chunk("d2", "spotlight_answer")

        text = self._run()

        self.assertIn("1 deleted team(s)", text)
        self.assertIn("2 chunk(s) in total would be deleted", text)
        self.assertNotIn(str(self.dead_team), text)
        self.assertEqual(RagChunk.objects.count(), 3)
        self.bulk_mock.assert_not_called()

    def test_apply_deletes_dead_team_chunks_and_keeps_live_ones(self):
        self._dead_chunk("d1")

        text = self._run("--apply")

        self.assertIn("1 tracking row(s) removed; 0 left", text)
        self.assertEqual(set(RagChunk.objects.values_list("chunk_id", flat=True)), {"live"})

    def test_apply_twice_is_a_no_op(self):
        self._dead_chunk("d1")
        self._run("--apply")
        self.assertIn("0 deleted team(s)", self._run("--apply"))

    def test_apply_and_dry_run_exclude_each_other(self):
        with self.assertRaises(CommandError):
            self._run("--apply", "--dry-run")


class TestTeamDeleteHook(DeadTeamTestCase):
    def test_deleting_a_team_purges_its_chunks_after_commit(self):
        other = TeamMaster.objects.create(
            team_name="other", team_email="o@example.com", owner=self.user
        )
        self._dead_chunk("o1", team_id=other.team_id)

        with self.captureOnCommitCallbacks(execute=True):
            other.delete()

        self.assertFalse(RagChunk.objects.filter(chunk_id="o1").exists())
        self.assertTrue(RagChunk.objects.filter(chunk_id="live").exists())
        self.assertIn("o1", self._bulk_deleted_ids())
