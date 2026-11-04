"""Turning "let my answers help teammates" back ON re-collects the answers (MIP-972).

The off switch purges the user's chunks from the team lane at once. The on
switch used to do nothing by itself: the cron reindex is incremental (the last
few minutes) and no cron runs a full one, so older answers stayed out of the
index while `GET /agent/shared-answers/` kept listing them under "Answers
shared with your team", which teammates could not actually see.

Now a real false -> true change starts, after the commit, a background job
(`shared_answers.reshare_team_lane_for_user`) that feeds the user's newest
runs through the chunker's own builder (`judge_run`) and the ordinary
ingestion path. These tests pin:

  * list and index agree again after off -> on, run for run, and the result is
    what a full reindex would have written (a batch pass afterwards finds
    nothing new, changed or stale);
  * only the real change triggers it (on -> on and off -> off do not), only
    after the commit (nothing starts on a rollback), and only for the caller;
  * the rule is the chunker's: a withdrawn run, a follow-up turn, a run built
    from private sources and another user's runs stay out;
  * it is bounded (the newest `RESHARE_SCAN_LIMIT` runs audited), idempotent,
    and an unreachable index neither fails the PATCH nor makes the job embed
    every run for nothing;
  * switching it off again while the job runs wins.

Same in-memory index as test_shared_answers; the job is run inline (see that
module's docstring for why no real thread is started).
"""

import logging
from datetime import timedelta
from unittest import mock

from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from origin.models.common.user_models import CustomUser
from origin.search_engine import ingestion, shared_answers
from origin.search_engine.llm import spend
from origin.search_engine.models import AgentRun, RagChunk

from .test_shared_answers import PREF_URL, SHARED_URL, SharedAnswersTestCase


class RolledBack(Exception):
    pass


def withhold(run):
    """The asker withdrew this one answer."""
    AgentRun.objects.filter(pk=run.pk).update(withheld_from_team_answers=True)
    run.refresh_from_db()


def open_server_side_cursors():
    """Names of the `.iterator()` cursors this connection still has open."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT name FROM pg_cursors")
        return [name for (name,) in cursor.fetchall() if name.startswith("_django_curs_")]


class ReshareTestCase(SharedAnswersTestCase):
    def make_unindexed(self, session=None, **kwargs):
        """A run as the answer path leaves it: in the DB, not in the index."""
        kwargs.setdefault("index", False)
        return self.make_run(session, **kwargs)

    def index_down(self):
        """Every write to OpenSearch fails, with a NEW exception each time.

        Not `side_effect = ConnectionError(...)`: the mock would keep that one
        instance, its traceback keeps the frames of the pass that raised it,
        and a pass that stopped early is suspended inside `.iterator()` with a
        server-side cursor open. The cursor then outlives the test, and the
        garbage collector closes it during whichever later test is running:
        it died with the first test's rollback, so that `CLOSE` aborts the
        later test's transaction ("current transaction is aborted").
        """

        def down(*args, **kwargs):
            raise ConnectionError("opensearch is down")

        self.bulk.side_effect = down

    def mixed_history(self):
        """One of everything the rule has to tell apart, for the caller and a
        teammate. Nothing is indexed."""
        session = self.make_session()
        self.collectable = self.make_unindexed(session, query="collectable first turn")
        self.follow_up = self.make_unindexed(session, query="follow-up, never collected")
        self.standalone = self.make_unindexed(None, query="standalone")
        self.withheld = self.make_unindexed(None, query="withdrawn")
        withhold(self.withheld)
        self.private = self.make_unindexed(None, cites_task=False, query="private sources")
        self.errored = self.make_unindexed(None, status="error", query="errored")
        self.theirs = self.make_unindexed(None, user=self.user2, query="a teammate's")
        self.mine_collected = {str(self.collectable.run_id), str(self.standalone.run_id)}

    # ---- observations -----------------------------------------------------

    def team_docs(self):
        """Run ids that have a document in the team lane."""
        return {
            eid.split(":", 1)[1] for eid in self.index.docs if eid.startswith("spotlight_answer:")
        }

    def team_tracked(self):
        """Run ids with a `RagChunk` tracking row in the team lane."""
        return {
            eid.split(":", 1)[1]
            for eid in RagChunk.objects.filter(entity_type="spotlight_answer").values_list(
                "entity_id", flat=True
            )
        }

    def listed_ids(self):
        res = self.client.get(SHARED_URL)
        self.assertEqual(res.status_code, 200)
        return {a["run_id"] for a in res.data["answers"]}

    def reindex(self):
        """The batch reindex (`opensearch_reindex`), team lane only."""
        return ingestion.ingest_all(entity_types=["spotlight_answer"])

    def reshare(self):
        shared_answers.reshare_team_lane_for_user(self.uid)

    def go_off_then_on(self):
        self.assertEqual(self.set_preference(False).status_code, 200)
        self.assertEqual(self.set_preference(True).status_code, 200)


# ---------------------------------------------------------------------------
# The point of the change: the list and the index agree after off -> on
# ---------------------------------------------------------------------------


class AgreementTests(ReshareTestCase):
    def test_after_off_then_on_the_list_and_the_index_hold_the_same_answers(self):
        self.mixed_history()
        self.reindex()  # sharing has been on all along: everything eligible is indexed
        everyone = self.mine_collected | {str(self.theirs.run_id)}
        self.assertEqual(self.team_docs(), everyone)
        self.assertEqual(self.listed_ids(), self.mine_collected)

        # OFF: their chunks are purged and the list is empty. A teammate's stay.
        self.assertEqual(self.set_preference(False).status_code, 200)
        self.assertEqual(self.team_docs(), {str(self.theirs.run_id)})
        self.assertEqual(self.team_tracked(), {str(self.theirs.run_id)})
        self.assertEqual(self.listed_ids(), set())
        self.assertEqual(self.client.get(SHARED_URL).data["answers"], [])

        # ON: the list is immediately full again; the index catches up when the
        # job (started after the commit) has run. This is the window the job
        # closes: before it, the page claimed answers teammates could not find.
        self.assertEqual(self.set_preference(True).status_code, 200)
        self.assertEqual(self.listed_ids(), self.mine_collected)
        self.assertEqual(len(self.background_jobs), 1)
        self.assertEqual(self.team_docs(), {str(self.theirs.run_id)})

        self.run_background_jobs()

        # Exactly the collectable runs are back, and they are what the list shows.
        self.assertEqual(self.team_docs(), everyone)
        self.assertEqual(self.team_docs() - {str(self.theirs.run_id)}, self.listed_ids())
        self.assertEqual(self.team_tracked(), everyone)  # RagChunk rows like any other chunk

        # Not the withdrawn, the follow-up, the private-source or the errored run,
        # and nothing for the private conversation lane.
        for run in (self.withheld, self.follow_up, self.private, self.errored):
            self.assertNotIn(str(run.run_id), self.team_docs())
        self.assertEqual([k for k in self.index.docs if k.startswith("conversation:")], [])

    def test_what_it_writes_is_what_a_full_reindex_would_have_written(self):
        self.mixed_history()
        self.reindex()
        before = {k: dict(v) for k, v in self.index.docs.items()}
        self.go_off_then_on()
        self.run_background_jobs()

        stats = self.reindex()

        # Nothing new, changed or stale: the job used the batch path's builder
        # and ingestion, so a later full pass has nothing left to do.
        self.assertEqual(
            (
                stats.chunks_new,
                stats.chunks_changed,
                stats.chunks_metadata_only,
                stats.chunks_deleted,
                stats.errors,
            ),
            (0, 0, 0, 0, []),
        )
        self.assertEqual(stats.chunks_unchanged, 3)
        after = self.index.docs
        for cid, doc in before.items():
            for key in ("title", "acl_user_ids", "answer_text", "related_entity_ids", "team_id"):
                self.assertEqual(after[cid][key], doc[key], (cid, key))
            self.assertNotIn("author_id", after[cid])  # still anonymous

    def test_the_other_users_runs_are_never_touched(self):
        self.mixed_history()  # the teammate's run is collectable and NOT indexed

        self.go_off_then_on()
        self.run_background_jobs()

        self.assertEqual(self.team_docs(), self.mine_collected)
        self.assertNotIn(str(self.theirs.run_id), self.team_docs())
        self.assertNotIn(str(self.theirs.run_id), self.team_tracked())

    def test_a_failed_purge_is_healed_when_sharing_comes_back(self):
        self.mixed_history()
        self.reindex()
        # They withdrew one answer, then switched off while the index was down.
        withhold(self.collectable)
        self.bulk.side_effect = ConnectionError("opensearch is down")
        self.assertEqual(self.set_preference(False).status_code, 200)
        self.assertIn(str(self.collectable.run_id), self.team_docs())  # purge missed
        self.bulk.side_effect = self.index.bulk
        self.embed.reset_mock()

        self.assertEqual(self.set_preference(True).status_code, 200)
        self.run_background_jobs()

        # The withdrawn answer's lingering chunk goes (tombstone); the other is
        # unchanged, so it is not embedded again.
        self.assertNotIn(str(self.collectable.run_id), self.team_docs())
        self.assertIn(str(self.standalone.run_id), self.team_docs())
        self.embed.assert_not_called()


# ---------------------------------------------------------------------------
# When it starts
# ---------------------------------------------------------------------------


class TriggerTests(ReshareTestCase):
    def test_off_to_on_starts_exactly_one_job(self):
        self.set_preference(False)
        self.assertEqual(self.background_jobs, [])

        self.set_preference(True)

        self.assertEqual(len(self.background_jobs), 1)

    def test_a_patch_that_keeps_it_on_starts_nothing(self):
        self.mixed_history()
        for _ in range(2):
            res = self.set_preference(True)  # already on: the default
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.data, {"share_answers_with_team": True})
        self.assertEqual(self.background_jobs, [])
        self.assertEqual(self.team_docs(), set())

    def test_off_to_off_starts_nothing(self):
        self.set_preference(False)
        self.set_preference(False)
        self.assertEqual(self.background_jobs, [])

    def test_turning_it_off_starts_nothing(self):
        self.assertEqual(self.set_preference(False).status_code, 200)
        self.assertEqual(self.background_jobs, [])

    def test_nothing_starts_before_the_commit(self):
        self.set_preference(False)

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            res = self.client.patch(PREF_URL, {"share_answers_with_team": True}, format="json")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.background_jobs, [])
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(self.background_jobs), 1)

    def test_a_rolled_back_change_starts_nothing(self):
        self.set_preference(False)

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(RolledBack):
                with transaction.atomic():
                    res = self.client.patch(
                        PREF_URL, {"share_answers_with_team": True}, format="json"
                    )
                    self.assertEqual(res.status_code, 200)
                    raise RolledBack

        self.assertEqual(callbacks, [])
        self.assertEqual(self.background_jobs, [])
        self.user.refresh_from_db()
        self.assertFalse(self.user.share_answers_with_team)

    def test_the_job_is_for_the_caller_only(self):
        self.mixed_history()
        self.set_preference(False, client=self.client)
        self.set_preference(True, client=self.client)
        (job,) = self.background_jobs
        self.background_jobs.clear()

        with mock.patch.object(shared_answers, "reshare_team_lane_for_user") as reshare:
            job()

        reshare.assert_called_once_with(self.uid)


# ---------------------------------------------------------------------------
# The rule, the bound, idempotence
# ---------------------------------------------------------------------------


class JobTests(ReshareTestCase):
    def test_it_collects_exactly_what_the_rule_collects(self):
        self.mixed_history()

        self.reshare()

        self.assertEqual(self.team_docs(), self.mine_collected)
        for run in (self.withheld, self.follow_up, self.private, self.errored, self.theirs):
            self.assertNotIn(str(run.run_id), self.team_docs())

    def test_it_goes_through_judge_run(self):
        self.make_unindexed(None)
        with mock.patch.object(
            shared_answers,
            "judge_run",
            wraps=shared_answers.judge_run,
        ) as judge:
            self.reshare()
        self.assertEqual(judge.call_count, 1)

    def test_the_bound_counts_runs_audited_newest_first(self):
        # Three newer runs the rule refuses and one older run it would take:
        # with a bound of three, the older one is never reached.
        old = self.make_unindexed(None, query="old but collectable", age=timedelta(days=3))
        for i in range(3):
            self.make_unindexed(
                None, cites_task=False, query=f"private {i}", age=timedelta(hours=i + 1)
            )

        with mock.patch.object(shared_answers, "RESHARE_SCAN_LIMIT", 3):
            self.reshare()

        self.assertEqual(self.team_docs(), set())
        self.embed.assert_not_called()

        # A later pass with room for it (the next full reindex) picks it up.
        self.reshare()
        self.assertEqual(self.team_docs(), {str(old.run_id)})

    def test_only_the_newest_runs_within_the_bound_are_indexed(self):
        runs = [
            self.make_unindexed(None, query=f"q{i}", age=timedelta(hours=i)) for i in range(3)
        ]  # runs[0] is the newest

        with mock.patch.object(shared_answers, "RESHARE_SCAN_LIMIT", 2):
            with self.assertLogs(shared_answers.log, logging.INFO) as logs:
                self.reshare()

        self.assertEqual(self.team_docs(), {str(runs[0].run_id), str(runs[1].run_id)})
        self.assertTrue(any("older runs" in line for line in logs.output), logs.output)

    def test_the_bound_is_applied_in_the_query_not_after_loading_the_history(self):
        for i in range(4):
            self.make_unindexed(None, query=f"q{i}", age=timedelta(hours=i))

        with mock.patch.object(shared_answers, "RESHARE_SCAN_LIMIT", 2):
            with CaptureQueriesContext(connection) as queries:
                self.reshare()

        selects = [
            q["sql"]
            for q in queries
            if "search_engine_agentrun" in q["sql"] and "LIMIT" in q["sql"]
        ]
        self.assertTrue(any("LIMIT 3" in sql for sql in selects), selects)  # bound + 1

    def test_it_works_through_a_long_history_without_cutting_it_short(self):
        for i in range(5):
            self.make_unindexed(None, query=f"q{i}", age=timedelta(minutes=i))
        with self.assertLogs(shared_answers.log, logging.INFO) as logs:
            self.reshare()
        self.assertEqual(len(self.team_docs()), 5)
        self.assertFalse(any("older runs" in line for line in logs.output), logs.output)

    def test_a_second_pass_embeds_and_writes_nothing(self):
        self.mixed_history()
        self.reshare()
        embeds, writes = self.embed.call_count, self.bulk.call_count
        docs = {k: dict(v) for k, v in self.index.docs.items()}
        rows = list(RagChunk.objects.order_by("chunk_id").values())
        self.assertEqual(embeds, 2)

        self.reshare()

        self.assertEqual(self.embed.call_count, embeds)
        self.assertEqual(self.bulk.call_count, writes)
        self.assertEqual(self.index.docs, docs)
        self.assertEqual(list(RagChunk.objects.order_by("chunk_id").values()), rows)

    def test_it_refreshes_the_index_once_so_the_answers_are_searchable(self):
        self.make_unindexed(None, query="one")
        self.make_unindexed(None, query="two")

        self.reshare()

        # RAG_BULK_REFRESH is off by default: without this the new chunks would
        # sit invisible until some other refresh.
        self.get_client.return_value.indices.refresh.assert_called_once_with(index="test-alias")

    def test_the_embeds_are_attributed_to_index_spend_not_to_nobody(self):
        seen = []

        def embed(texts):
            ctx = spend.current_context()
            seen.append((ctx.surface if ctx else None, spend.current_purpose()))
            return [[0.1, 0.2, 0.3] for _ in texts]

        self.embed.side_effect = embed
        self.make_unindexed(None)

        self.reshare()

        self.assertEqual(seen, [("index", "index")])

    def test_it_does_nothing_for_someone_who_is_off(self):
        self.make_unindexed(None)
        self.user.share_answers_with_team = False
        self.user.save(update_fields=["share_answers_with_team"])

        with mock.patch.object(shared_answers, "judge_run") as judge:
            self.reshare()

        judge.assert_not_called()  # not even audited
        self.embed.assert_not_called()
        self.assertEqual(self.team_docs(), set())

    def test_an_unknown_user_is_a_no_op(self):
        shared_answers.reshare_team_lane_for_user("00000000-0000-4000-8000-000000000000")
        self.embed.assert_not_called()


# ---------------------------------------------------------------------------
# Failure behaviour
# ---------------------------------------------------------------------------


class FailureTests(ReshareTestCase):
    def test_an_opensearch_failure_does_not_fail_the_patch_and_the_next_reindex_catches_up(self):
        self.mixed_history()
        self.set_preference(False)
        self.index_down()

        res = self.set_preference(True)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"share_answers_with_team": True})
        self.user.refresh_from_db()
        self.assertTrue(self.user.share_answers_with_team)
        self.run_background_jobs()  # must not raise
        self.assertEqual(self.team_docs(), set())
        # Nothing is recorded as indexed that is not, so the reindex retries it.
        self.assertEqual(self.team_tracked(), set())

        self.bulk.side_effect = self.index.bulk
        stats = self.reindex()

        self.assertEqual(stats.errors, [])
        self.assertEqual(self.team_docs(), self.mine_collected | {str(self.theirs.run_id)})

    def test_a_dead_index_stops_the_pass_instead_of_embedding_every_run(self):
        for i in range(8):
            self.make_unindexed(None, query=f"q{i}", age=timedelta(minutes=i))
        self.index_down()

        self.reshare()  # does not raise

        self.assertEqual(self.embed.call_count, shared_answers.RESHARE_ABORT_AFTER_ERRORS)
        self.assertEqual(self.team_docs(), set())
        # The pass stopped with runs left, i.e. with its `.iterator()` unspent.
        self.assertEqual(open_server_side_cursors(), [])

    def test_the_embedder_failing_is_survived_too(self):
        self.make_unindexed(None)
        self.embed.side_effect = RuntimeError("embedding provider is down")
        self.reshare()
        self.assertEqual(self.team_docs(), set())

    def test_a_thread_that_cannot_start_does_not_fail_the_patch(self):
        self.set_preference(False)
        with (
            mock.patch.object(shared_answers, "_run_in_background", self.real_run_in_background),
            mock.patch.object(shared_answers, "threading") as threading,
        ):
            threading.Thread.return_value.start.side_effect = RuntimeError("can't start new thread")
            res = self.set_preference(True)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"share_answers_with_team": True})
        self.user.refresh_from_db()
        self.assertTrue(self.user.share_answers_with_team)

    def test_the_background_runner(self):
        job = mock.Mock(side_effect=RuntimeError("boom"))
        with (
            mock.patch.object(shared_answers, "threading") as threading,
            mock.patch.object(shared_answers, "connection") as connection,
        ):
            self.real_run_in_background(job, name="reshare-team-answers")

            (call,) = threading.Thread.call_args_list
            self.assertIs(call.kwargs["daemon"], True)  # never keeps a worker from exiting
            threading.Thread.return_value.start.assert_called_once_with()

            with self.assertLogs(shared_answers.log, logging.ERROR):
                call.kwargs["target"]()  # a failing job is logged, not raised ...

            job.assert_called_once_with()
            connection.close.assert_called_once_with()  # ... and the thread's connection freed

    def test_the_entry_point_hands_the_runner_a_job_for_that_user(self):
        with mock.patch.object(shared_answers, "reshare_team_lane_for_user") as reshare:
            shared_answers.reshare_team_lane_in_background(self.uid)
            (job,) = self.background_jobs
            reshare.assert_not_called()  # nothing runs on the caller's thread
            job()
        reshare.assert_called_once_with(self.uid)


# ---------------------------------------------------------------------------
# Switching it off again while the job runs
# ---------------------------------------------------------------------------


class SwitchedOffAgainTests(ReshareTestCase):
    def switch_off_during_embed(self):
        def embed(texts):
            CustomUser.objects.filter(pk=self.user.pk).update(share_answers_with_team=False)
            return [[0.1, 0.2, 0.3] for _ in texts]

        self.embed.side_effect = embed

    def test_the_job_stops_at_the_next_run(self):
        for i in range(3):
            self.make_unindexed(None, query=f"q{i}", age=timedelta(minutes=i))
        self.switch_off_during_embed()

        self.reshare()

        self.assertEqual(self.embed.call_count, 1)  # the other two are never embedded
        self.assertEqual(self.team_docs(), set())

    def test_whatever_landed_after_the_off_purge_is_purged_at_the_end(self):
        # The off request's purge ran while this run's chunk was still being
        # written, so it could not remove it. The job checks once more at the end.
        run = self.make_unindexed(None)
        self.switch_off_during_embed()

        self.reshare()

        self.assertEqual(self.embed.call_count, 1)
        self.assertNotIn(str(run.run_id), self.team_docs())
        self.assertEqual(self.team_tracked(), set())
