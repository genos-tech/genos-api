"""A scoped ask ("Ask about this thread / note") is never collected into the team lane (MIP-978).

Asking from a chat thread or a note puts an LLM-made SUMMARY of that thread /
note into the model's system prompt (`<thread_summary>` / `<note_summary>`).
That summary is not a tool result, so it never becomes a source of the run and
never narrowed the audience the answer was collected for. A first-turn scoped
ask about a PRIVATE thread whose answer quoted the summary, and which also read
one team-visible object, was therefore collected for that object's wider
audience and shown anonymously to teammates who cannot see the thread.

The fix is one rule in `shared_answers` (`is_scoped_session`, built from
`SESSION_SCOPE_COLUMNS`): the first turn of a session bound to a thread or a
note is WITHHELD (a tombstone, so a copy already in the index is purged), never
collected. These tests pin it for every consumer of the rule: the chunker, the
"answers shared with your team" list, the re-collection after an opt-in and the
orphan sweep (which has to remove the copies collected before the fix without
waiting for a full reindex), plus the command that does the same on demand.

No LLM and no OpenSearch: same in-memory index as test_shared_answers.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.search_engine import ingestion, purge, shared_answers
from origin.search_engine.chunkers.spotlight_answer_chunker import iter_spotlight_answer_chunks
from origin.search_engine.management.commands import purge_scoped_team_answers as command
from origin.search_engine.models import AgentRun, AgentSession, RagChunk
from origin.search_engine.shared_answers import (
    COLLECT,
    SESSION_SCOPE_COLUMNS,
    SKIP,
    WITHHELD,
    is_scoped_session,
    judge_run,
    scoped_session_q,
)

from .test_shared_answers import SHARED_URL, SharedAnswersTestCase

PRIVATE_QUOTE = "PRIVATE-THREAD-TEXT: the reorg list is Alice, Bob and Carol"

# One value per scope column, so a test can set exactly ONE of them (a session
# made through the ask view always sets all three of a thread or both of a
# note, which would hide a rule that only looks at one column).
SCOPE_VALUES = {
    "chat_type": 2,
    "chat_id": uuid.UUID("00000000-0000-4000-8000-0000000000c1"),
    "thread_id": uuid.UUID("00000000-0000-4000-8000-0000000000d1"),
    "note_type": 1,
    "note_id": 4242,
}


class ScopedAskTestCase(SharedAnswersTestCase):
    def thread_session(self, **kwargs):
        return self.make_session(
            chat_type=2, chat_id=uuid.uuid4(), thread_id=uuid.uuid4(), **kwargs
        )

    def note_session(self, **kwargs):
        return self.make_session(note_type=1, note_id=4242, **kwargs)

    def scoped_run(self, session, **kwargs):
        """A first-turn done run whose answer quotes the injected summary and
        which read one object everybody on the team can see (`fetch_task`)."""
        kwargs.setdefault("query", "What did we decide in this thread?")
        kwargs.setdefault("answer", f"Per the thread: {PRIVATE_QUOTE}. See [task:1].")
        return self.make_run(session, **kwargs)

    def verdict(self, run):
        run.refresh_from_db()
        return judge_run(run, opted_out=set()).state

    def team_docs(self):
        """Run ids that have a document in the team lane of the index."""
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


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class VerdictTests(ScopedAskTestCase):
    """The two reproduction tests failed before the fix, with
    `AssertionError: 'collect' != 'withheld'`."""

    def test_a_thread_scoped_first_turn_is_not_collected(self):  # reproduction
        run = self.scoped_run(self.thread_session())

        self.assertEqual(self.verdict(run), WITHHELD)

    def test_a_note_scoped_first_turn_is_not_collected(self):  # reproduction
        run = self.scoped_run(self.note_session())

        self.assertEqual(self.verdict(run), WITHHELD)

    def test_no_chunk_carries_the_summary_text_to_a_teammate(self):  # reproduction
        self.scoped_run(self.thread_session())
        self.scoped_run(self.note_session())

        leaked = [
            chunk
            for entity in iter_spotlight_answer_chunks()
            for chunk in entity.chunks
            if PRIVATE_QUOTE in chunk.answer_text and self.uid2 in chunk.acl_user_ids
        ]

        self.assertEqual(leaked, [])

    def test_any_one_scope_column_is_enough(self):
        # Each column on its own, so a rule that reads the wrong column (or
        # only the thread's) cannot pass because the others happen to be set.
        self.assertEqual(set(SCOPE_VALUES), set(SESSION_SCOPE_COLUMNS))
        for column in SESSION_SCOPE_COLUMNS:
            with self.subTest(column=column):
                run = self.scoped_run(self.make_session(**{column: SCOPE_VALUES[column]}))
                self.assertEqual(self.verdict(run), WITHHELD)

    def test_a_scoped_first_turn_is_withheld_whatever_it_read(self):
        # The source audit is not even needed: a run with no readable source
        # was already withheld, and one with a team-visible source is now too.
        for cites_task in (True, False):
            with self.subTest(cites_task=cites_task):
                run = self.scoped_run(self.thread_session(), cites_task=cites_task)
                verdict = judge_run(run, opted_out=set())
                self.assertEqual(verdict.state, WITHHELD)
                self.assertIsNone(verdict.acl)

    def test_unscoped_asks_are_still_collected(self):
        # Regression guard: the rule must not swallow ordinary asks.
        plain_session = self.make_run(self.make_session(), query="a plain first turn")
        standalone = self.make_run(None, query="no session at all")

        self.assertEqual(self.verdict(plain_session), COLLECT)
        self.assertEqual(self.verdict(standalone), COLLECT)
        collected = judge_run(standalone, opted_out=set())
        self.assertEqual(sorted(collected.acl), sorted([self.uid, self.uid2]))

    def test_a_follow_up_in_a_scoped_session_is_still_skipped(self):
        session = self.thread_session()
        first = self.scoped_run(session, query="first")
        follow_up = self.scoped_run(session, query="second")

        # Never collected and never indexed, so there is nothing to purge:
        # SKIP, as for any follow-up. The FIRST turn is the one withheld.
        self.assertEqual(self.verdict(first), WITHHELD)
        self.assertEqual(self.verdict(follow_up), SKIP)

    def test_other_reasons_still_win_over_the_scope(self):
        errored = self.scoped_run(self.thread_session(), status="error")
        self.assertEqual(self.verdict(errored), SKIP)


class ScopeHelperTests(ScopedAskTestCase):
    def test_is_scoped_session_reads_each_column(self):
        self.assertFalse(is_scoped_session(None))
        self.assertFalse(is_scoped_session(self.make_session()))
        for column in SESSION_SCOPE_COLUMNS:
            with self.subTest(column=column):
                session = self.make_session(**{column: SCOPE_VALUES[column]})
                self.assertTrue(is_scoped_session(session))

    def test_the_row_check_and_the_queryset_filter_are_the_same_rule(self):
        runs = {"standalone": self.make_run(None), "plain": self.make_run(self.make_session())}
        for column in SESSION_SCOPE_COLUMNS:
            runs[column] = self.make_run(self.make_session(**{column: SCOPE_VALUES[column]}))

        in_filter = {
            str(pk)
            for pk in AgentRun.objects.filter(scoped_session_q()).values_list("pk", flat=True)
        }
        not_in_filter = {
            str(pk)
            for pk in AgentRun.objects.exclude(scoped_session_q()).values_list("pk", flat=True)
        }

        for name, run in runs.items():
            run.refresh_from_db()
            scoped = is_scoped_session(run.session)
            self.assertEqual(scoped, str(run.pk) in in_filter, name)
            self.assertEqual(not scoped, str(run.pk) in not_in_filter, name)
        # The session-less run is in the NOT-scoped half (a NULL join must not hide it).
        self.assertIn(str(runs["standalone"].pk), not_in_filter)
        self.assertEqual(len(in_filter), len(SESSION_SCOPE_COLUMNS))

    def test_still_shareable_drops_scoped_runs_only(self):
        standalone = self.make_run(None)
        plain = self.make_run(self.make_session())
        scoped = self.make_run(self.thread_session())

        kept = set(
            shared_answers.still_shareable(AgentRun.objects.all()).values_list("pk", flat=True)
        )

        self.assertEqual(kept, {standalone.pk, plain.pk})
        self.assertNotIn(scoped.pk, kept)

    def test_a_new_session_column_has_to_be_classified(self):
        """Fails when `AgentSession` gains a column, so whoever adds one (a
        project scope, a task scope) decides whether it belongs in
        `SESSION_SCOPE_COLUMNS` instead of the rule silently not seeing it."""
        not_a_scope = {
            "session_id",
            "team_id",
            "user_id",
            "created_at",
            "last_active_at",
            "rolling_summary_text",
            "rolling_summary_through",
            "pinned_at",
        }
        columns = {field.name for field in AgentSession._meta.concrete_fields}
        self.assertEqual(columns, not_a_scope | set(SESSION_SCOPE_COLUMNS))


# ---------------------------------------------------------------------------
# The chunker (the batch reindex) and the list
# ---------------------------------------------------------------------------


class ChunkerTests(ScopedAskTestCase):
    def test_a_scoped_first_turn_is_a_tombstone_and_the_rest_still_collect(self):
        thread = self.scoped_run(self.thread_session())
        note = self.scoped_run(self.note_session())
        plain = self.make_run(self.make_session(), query="plain")

        lane = self.team_lane()

        # Present but empty: a tombstone, not a skip (absent), so the pass
        # that visits it purges a copy collected before the rule.
        self.assertIs(lane[str(thread.run_id)], False)
        self.assertIs(lane[str(note.run_id)], False)
        self.assertIs(lane[str(plain.run_id)], True)

    def test_the_session_is_read_with_the_run_not_once_per_run(self):
        for _ in range(3):
            self.scoped_run(self.thread_session())

        with CaptureQueriesContext(connection) as queries:
            list(iter_spotlight_answer_chunks())

        lazy_loads = [q["sql"] for q in queries if 'FROM "search_engine_agentsession"' in q["sql"]]
        self.assertEqual(lazy_loads, [])

    def test_a_reindex_pass_purges_a_copy_collected_before_the_rule(self):
        # `make_run` leaves the stand-in team chunk (and its tracking row) a
        # reindex would have written back then. Ingestion alone, with no sweep.
        scoped = self.scoped_run(self.thread_session())
        plain = self.make_run(self.make_session(), query="plain")
        self.assertIn(str(scoped.run_id), self.team_docs())

        ingestion.ingest_all(entity_types=["spotlight_answer"])

        self.assertNotIn(str(scoped.run_id), self.team_docs())
        self.assertNotIn(str(scoped.run_id), self.team_tracked())
        self.assertIn(str(plain.run_id), self.team_docs())
        self.assertTrue(self.conversation_chunk_indexed(scoped))  # private memory stays


class ListTests(ScopedAskTestCase):
    def test_the_list_is_exactly_what_the_chunker_collects_from_the_caller(self):
        plain = self.make_run(self.make_session(), query="plain")
        standalone = self.make_run(None, query="standalone")
        self.scoped_run(self.thread_session())
        self.scoped_run(self.note_session())
        self.make_run(None, user=self.user2, query="a teammate's")

        self.assertEqual(self.listed_ids(), self.collected(self.user))
        self.assertEqual(self.listed_ids(), {str(plain.run_id), str(standalone.run_id)})

    def test_the_list_goes_through_judge_run(self):
        self.scoped_run(self.thread_session())
        with mock.patch.object(
            shared_answers, "judge_run", wraps=shared_answers.judge_run
        ) as judge:
            self.assertEqual(self.listed_ids(), set())
        self.assertEqual(judge.call_count, 1)

    def test_a_scoped_run_does_not_hide_the_ones_after_it(self):
        for _ in range(3):
            self.scoped_run(self.thread_session())
        plain = self.make_run(self.make_session(), query="older but plain", age=timedelta(days=2))

        self.assertEqual(self.listed_ids(), {str(plain.run_id)})


# ---------------------------------------------------------------------------
# Copies collected before the rule: the sweep, then the opt-in re-collection
# ---------------------------------------------------------------------------


class SweepTests(ScopedAskTestCase):
    def test_the_sweep_removes_a_scoped_runs_team_chunk_and_nothing_else(self):
        scoped_thread = self.scoped_run(self.thread_session())
        scoped_note = self.scoped_run(self.note_session())
        plain = self.make_run(self.make_session(), query="plain")
        standalone = self.make_run(None, query="standalone")
        self.assertEqual(
            self.team_docs(),
            {str(r.run_id) for r in (scoped_thread, scoped_note, plain, standalone)},
        )

        stats = purge.sweep_orphans()

        self.assertEqual(self.team_docs(), {str(plain.run_id), str(standalone.run_id)})
        self.assertEqual(self.team_tracked(), {str(plain.run_id), str(standalone.run_id)})
        self.assertEqual(stats["purged_by_type"], {"spotlight_answer": 2})
        # The private memory of the very same runs is not the team lane's business.
        for run in (scoped_thread, scoped_note, plain, standalone):
            self.assertTrue(self.conversation_chunk_indexed(run))
            self.assertIn(f"conversation:{run.run_id}", self.tracked(run))
        # The runs themselves are untouched.
        self.assertTrue(self.run_exists(scoped_thread))
        self.assertTrue(self.run_exists(scoped_note))

    def test_a_dry_run_sweep_reports_them_and_deletes_nothing(self):
        scoped = self.scoped_run(self.thread_session())

        stats = purge.sweep_orphans(dry_run=True)

        self.assertEqual(stats["purged_by_type"], {"spotlight_answer": 1})
        self.assertIn(str(scoped.run_id), self.team_docs())

    def test_an_incremental_pass_does_not_reach_an_old_run_but_the_sweep_does(self):
        old = self.scoped_run(self.thread_session(), age=timedelta(days=30))

        # What the 10-minute cron runs: only recent runs are revisited, so the
        # old copy would survive until a full reindex ...
        ingestion.ingest_all(
            since=datetime.now(timezone.utc) - timedelta(minutes=11),
            entity_types=["spotlight_answer"],
        )
        self.assertIn(str(old.run_id), self.team_docs())

        # ... which is why the sweep after every pass has to be the one to take it.
        purge.sweep_orphans()
        self.assertNotIn(str(old.run_id), self.team_docs())


class ReshareTests(ScopedAskTestCase):
    def test_turning_sharing_back_on_never_re_collects_a_scoped_run(self):
        scoped = [
            self.scoped_run(self.thread_session(), index=False),
            self.scoped_run(self.note_session(), index=False),
        ]
        plain = self.make_run(self.make_session(), query="plain", index=False)

        shared_answers.reshare_team_lane_for_user(self.uid)

        self.assertEqual(self.team_docs(), {str(plain.run_id)})
        for run in scoped:
            self.assertNotIn(str(run.run_id), self.team_tracked())

    def test_it_also_purges_a_copy_a_scoped_run_already_had(self):
        scoped = self.scoped_run(self.thread_session())  # collected before the rule

        shared_answers.reshare_team_lane_for_user(self.uid)

        self.assertNotIn(str(scoped.run_id), self.team_docs())

    def test_the_opt_in_endpoint_path_agrees_with_the_list(self):
        self.scoped_run(self.thread_session(), index=False)
        plain = self.make_run(self.make_session(), query="plain", index=False)
        self.set_preference(False)

        self.set_preference(True)
        self.run_background_jobs()

        self.assertEqual(self.team_docs(), {str(plain.run_id)})
        self.assertEqual(self.listed_ids(), self.team_docs())


# ---------------------------------------------------------------------------
# python manage.py purge_scoped_team_answers
# ---------------------------------------------------------------------------


class CommandTests(ScopedAskTestCase):
    def setUp(self):
        super().setUp()
        self.client_mock = mock.MagicMock()
        self.client_mock.ping.return_value = True
        patcher = mock.patch.object(command, "get_client", return_value=self.client_mock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_command(self, *args):
        out = StringIO()
        call_command("purge_scoped_team_answers", *args, stdout=out)
        text = out.getvalue()
        return text, json.loads(text[: text.rindex("}") + 1])

    def history(self):
        self.scoped_thread = self.scoped_run(self.thread_session())
        self.scoped_note = self.scoped_run(self.note_session())
        self.follow_up = self.scoped_run(self.scoped_thread.session, query="follow-up")
        self.plain = self.make_run(self.make_session(), query="plain")
        self.standalone = self.make_run(None, query="standalone")
        # A scoped run of a teammate counts too: the rule is not per user.
        self.theirs = self.scoped_run(self.thread_session(user=self.user2), user=self.user2)

    def test_it_is_a_dry_run_by_default_and_prints_counts_only(self):
        self.history()

        text, report = self.run_command()

        self.assertEqual(
            report,
            {
                "mode": "dry-run",
                "scoped_runs": 4,  # thread, note, the follow-up, the teammate's
                "tracked_team_chunks": 4,  # `make_run` tracks one per run
            },
        )
        self.assertEqual(self.run_command("--dry-run")[1], report)
        # Nothing deleted, the index never contacted.
        self.bulk.assert_not_called()
        self.client_mock.ping.assert_not_called()
        self.assertEqual(len(self.team_docs()), 6)
        # Counts only: no id, no question, no answer text.
        for run in AgentRun.objects.all():
            self.assertNotIn(str(run.run_id), text)
            self.assertNotIn(run.query, text)
        self.assertNotIn(PRIVATE_QUOTE, text)
        self.assertNotIn(self.uid, text)

    def test_apply_purges_the_scoped_chunks_and_leaves_everything_else(self):
        self.history()

        text, report = self.run_command("--apply")

        self.assertEqual(
            report,
            {
                "mode": "apply",
                "scoped_runs": 4,
                "tracked_team_chunks": 4,
                "tracked_team_chunks_remaining": 0,
            },
        )
        self.assertEqual(self.team_docs(), {str(self.plain.run_id), str(self.standalone.run_id)})
        self.assertEqual(self.team_tracked(), {str(self.plain.run_id), str(self.standalone.run_id)})
        # Only the team lane, only the scoped sessions' runs.
        for run in AgentRun.objects.all():
            self.assertTrue(self.conversation_chunk_indexed(run))
            self.assertTrue(self.run_exists(run))
        self.assertNotIn(PRIVATE_QUOTE, text)

    def test_apply_also_deletes_a_document_the_tracking_table_lost(self):
        # A purge must not depend on `RagChunk` having kept up with the index.
        scoped = self.scoped_run(self.thread_session())
        RagChunk.objects.filter(entity_id=f"spotlight_answer:{scoped.run_id}").delete()
        self.assertIn(str(scoped.run_id), self.team_docs())

        _, report = self.run_command("--apply")

        self.assertEqual(report["tracked_team_chunks"], 0)
        self.assertNotIn(str(scoped.run_id), self.team_docs())

    def test_it_is_idempotent(self):
        self.history()
        self.run_command("--apply")

        _, again = self.run_command("--apply")

        self.assertEqual(again["tracked_team_chunks"], 0)
        self.assertEqual(again["tracked_team_chunks_remaining"], 0)
        self.assertEqual(self.run_command()[1]["tracked_team_chunks"], 0)

    def test_nothing_scoped_is_a_no_op_that_does_not_need_the_index(self):
        self.make_run(self.make_session())
        self.client_mock.ping.return_value = False

        _, report = self.run_command("--apply")

        self.assertEqual(report["scoped_runs"], 0)
        self.bulk.assert_not_called()

    def test_apply_refuses_when_opensearch_is_unreachable(self):
        self.history()
        self.client_mock.ping.return_value = False

        with self.assertRaises(CommandError):
            self.run_command("--apply")

        self.bulk.assert_not_called()
        self.assertEqual(len(self.team_docs()), 6)

    def test_apply_fails_loudly_when_a_delete_did_not_land(self):
        self.history()
        self.bulk.side_effect = ConnectionError("opensearch went down mid-way")

        with self.assertRaises(CommandError):
            self.run_command("--apply")

        self.assertEqual(len(self.team_docs()), 6)  # still there, still tracked ...
        self.bulk.side_effect = self.index.bulk
        _, report = self.run_command("--apply")  # ... and a second run finishes it
        self.assertEqual(report["tracked_team_chunks_remaining"], 0)
        self.assertEqual(self.team_docs(), {str(self.plain.run_id), str(self.standalone.run_id)})

    def test_dry_run_and_apply_cannot_be_combined(self):
        with self.assertRaises(CommandError):
            call_command("purge_scoped_team_answers", "--dry-run", "--apply", stdout=StringIO())
