"""Answers shared with the team: tell people, list them, let them opt out.

A completed Spotlight answer can be collected into the team-shared
`spotlight_answer` lane (shown anonymously as a "Previous answer" to
teammates who can see all of its sources). This module pins the three ways a
person controls that:

  * the preference "let my answers help teammates" (default on): off means
    the chunker collects none of their runs and the ones already collected are
    purged at once;
  * withdrawing ONE answer (`DELETE /agent/shared-answers/<run_id>/`): a
    per-run flag the chunker honours, purged at once, the conversation kept;
  * the list (`GET /agent/shared-answers/`), whose criteria are the chunker's
    own (`shared_answers.judge_run`), asserted to match it run for run.

The orphan sweep is the safety net for a purge that OpenSearch missed, so it
is asserted to remove a withdrawn / opted-out chunk (the run row survives, so
"run is gone" alone would never catch it) and to leave healthy ones alone.

No LLM and no OpenSearch: same in-memory index as test_agent_conversation_delete.
"""

import uuid
from datetime import timedelta
from unittest import mock

from rest_framework.test import APIClient

from origin.search_engine import purge
from origin.search_engine.chunkers.spotlight_answer_chunker import iter_spotlight_answer_chunks
from origin.search_engine.models import AgentRun, RagChunk
from origin.search_engine.shared_answers import COLLECT, SKIP, WITHHELD, judge_run

from .test_agent_conversation_delete import LIST_URL, ConversationDeleteTestCase

SHARED_URL = "/api/v2/agent/shared-answers/"
PREF_URL = "/api/v2/user/preferences/share-answers/"


def _shared_url(run):
    return f"{SHARED_URL}{run.run_id}/"


class SharedAnswersTestCase(ConversationDeleteTestCase):
    def team_lane(self):
        """{run_id: has_chunk} for every run the team-lane chunker has an
        opinion on (skipped runs are absent; withheld ones are tombstones)."""
        return {
            e.entity_id.split(":", 1)[1]: bool(e.chunks) for e in iter_spotlight_answer_chunks()
        }

    def collected(self, user=None):
        """Run ids the chunker would put in the lane, optionally for one user."""
        ids = {rid for rid, has in self.team_lane().items() if has}
        if user is None:
            return ids
        return {rid for rid in ids if AgentRun.objects.get(pk=rid).user_id == str(user.id)}

    def listed(self, **params):
        res = self.client.get(SHARED_URL, params)
        self.assertEqual(res.status_code, 200)
        return res.data

    def set_preference(self, value, *, client=None):
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.client).patch(
                PREF_URL, {"share_answers_with_team": value}, format="json"
            )

    def team_chunk_indexed(self, run):
        return f"spotlight_answer:{run.run_id}" in self.index.docs

    def conversation_chunk_indexed(self, run):
        return f"conversation:{run.run_id}" in self.index.docs


# ---------------------------------------------------------------------------
# The preference
# ---------------------------------------------------------------------------


class PreferenceTests(SharedAnswersTestCase):
    def test_defaults_to_on(self):
        res = self.client.get(PREF_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"share_answers_with_team": True})

    def test_patch_round_trips(self):
        res = self.set_preference(False)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"share_answers_with_team": False})
        self.assertEqual(self.client.get(PREF_URL).data, {"share_answers_with_team": False})
        self.user.refresh_from_db()
        self.assertFalse(self.user.share_answers_with_team)

        self.assertEqual(self.set_preference(True).status_code, 200)
        self.assertEqual(self.client.get(PREF_URL).data, {"share_answers_with_team": True})

    def test_only_a_real_boolean_is_accepted(self):
        for bad in ("false", 0, None, "", [], {}):
            res = self.client.patch(PREF_URL, {"share_answers_with_team": bad}, format="json")
            self.assertEqual(res.status_code, 400, bad)
        self.assertEqual(self.client.patch(PREF_URL, {}, format="json").status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.share_answers_with_team)

    def test_it_is_the_callers_own_and_needs_a_login(self):
        self.set_preference(False)
        self.user2.refresh_from_db()
        self.assertTrue(self.user2.share_answers_with_team)
        self.assertEqual(APIClient().get(PREF_URL).status_code, 401)
        self.assertEqual(
            APIClient()
            .patch(PREF_URL, {"share_answers_with_team": False}, format="json")
            .status_code,
            401,
        )

    def test_turning_it_off_purges_the_team_lane_of_all_their_runs_at_once(self):
        mine = [self.make_run(self.make_session()), self.make_run(None)]
        theirs = self.make_run(self.make_session(user=self.user2), user=self.user2)

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            res = self.client.patch(PREF_URL, {"share_answers_with_team": False}, format="json")
        self.assertEqual(res.status_code, 200)
        # Not before the preference write commits ...
        self.bulk.assert_not_called()
        for run in mine:
            self.assertTrue(self.team_chunk_indexed(run))
        for callback in callbacks:
            callback()
        # ... then their team-lane chunks are gone, nobody else's, and the
        # private conversation memory is untouched.
        for run in mine:
            self.assertFalse(self.team_chunk_indexed(run))
            self.assertNotIn(f"spotlight_answer:{run.run_id}", self.tracked(run))
            self.assertTrue(self.conversation_chunk_indexed(run))
            self.assertIn(f"conversation:{run.run_id}", self.tracked(run))
        self.assertTrue(self.team_chunk_indexed(theirs))
        self.assertTrue(self.conversation_chunk_indexed(theirs))
        self.assertTrue(all(self.run_exists(r) for r in mine))

    def test_turning_it_on_purges_nothing(self):
        run = self.make_run(None)
        self.set_preference(True)
        self.bulk.assert_not_called()
        self.assertTrue(self.team_chunk_indexed(run))

    def test_an_opensearch_failure_does_not_fail_the_preference(self):
        run = self.make_run(None)
        self.bulk.side_effect = ConnectionError("opensearch is down")

        res = self.set_preference(False)

        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.share_answers_with_team)
        self.assertTrue(self.team_chunk_indexed(run))  # still there ...

        # ... until the sweep notices the owner opted out. The run row exists,
        # so "the run is gone" alone would never catch this.
        self.bulk.side_effect = self.index.bulk
        purge.sweep_orphans()
        self.assertFalse(self.team_chunk_indexed(run))
        self.assertTrue(self.conversation_chunk_indexed(run))

    def test_a_repeat_of_off_retries_a_failed_purge(self):
        run = self.make_run(None)
        self.bulk.side_effect = ConnectionError("opensearch is down")
        self.set_preference(False)
        self.assertTrue(self.team_chunk_indexed(run))

        self.bulk.side_effect = self.index.bulk
        self.set_preference(False)

        self.assertFalse(self.team_chunk_indexed(run))


# ---------------------------------------------------------------------------
# The chunker honours the choices
# ---------------------------------------------------------------------------


class ChunkerTests(SharedAnswersTestCase):
    def test_a_normal_first_turn_answer_is_collected(self):
        run = self.make_run(self.make_session(), query="What is the deploy plan?")
        chunks = [e for e in iter_spotlight_answer_chunks() if e.chunks]
        self.assertEqual([e.entity_id for e in chunks], [f"spotlight_answer:{run.run_id}"])
        chunk = chunks[0].chunks[0]
        self.assertEqual(sorted(chunk.acl_user_ids), sorted([self.uid, self.uid2]))
        self.assertEqual(chunk.snippet_text, "Ship it on Friday.")
        self.assertIsNone(chunk.author_id)  # still anonymous

    def test_preference_off_collects_none_of_their_runs_and_tombstones_them(self):
        mine = [self.make_run(self.make_session()), self.make_run(None)]
        theirs = self.make_run(self.make_session(user=self.user2), user=self.user2)
        self.user.share_answers_with_team = False
        self.user.save(update_fields=["share_answers_with_team"])

        lane = self.team_lane()

        for run in mine:
            self.assertIs(lane[str(run.run_id)], False)  # tombstone, not a chunk
        self.assertIs(lane[str(theirs.run_id)], True)  # a teammate's is unaffected

    def test_preference_back_on_collects_them_again(self):
        run = self.make_run(None)
        self.user.share_answers_with_team = False
        self.user.save(update_fields=["share_answers_with_team"])
        self.assertEqual(self.collected(), set())

        self.user.share_answers_with_team = True
        self.user.save(update_fields=["share_answers_with_team"])

        self.assertEqual(self.collected(), {str(run.run_id)})

    def test_a_withheld_answer_is_a_tombstone_and_the_rest_still_collect(self):
        withheld = self.make_run(self.make_session(), query="withdrawn")
        kept = self.make_run(self.make_session(), query="kept")
        AgentRun.objects.filter(pk=withheld.pk).update(withheld_from_team_answers=True)

        lane = self.team_lane()

        self.assertIs(lane[str(withheld.run_id)], False)
        self.assertIs(lane[str(kept.run_id)], True)

    def test_withholding_does_not_touch_the_conversation_lane(self):
        from origin.search_engine.chunkers.conversation_chunker import iter_conversation_chunks

        run = self.make_run(None)
        AgentRun.objects.filter(pk=run.pk).update(withheld_from_team_answers=True)
        self.user.share_answers_with_team = False
        self.user.save(update_fields=["share_answers_with_team"])

        conv = [e.entity_id for e in iter_conversation_chunks() if e.chunks]

        self.assertEqual(conv, [f"conversation:{run.run_id}"])

    def test_verdicts(self):
        first = self.make_run(self.make_session())
        follow_up = self.make_run(first.session)
        errored = self.make_run(None, status="error")
        no_sources = self.make_run(None, cites_task=False)
        withheld = self.make_run(None)
        AgentRun.objects.filter(pk=withheld.pk).update(withheld_from_team_answers=True)
        for run in (first, follow_up, errored, no_sources, withheld):
            run.refresh_from_db()

        def verdict(run, opted_out=frozenset()):
            return judge_run(run, opted_out=set(opted_out)).state

        self.assertEqual(verdict(first), COLLECT)
        self.assertEqual(verdict(follow_up), SKIP)
        self.assertEqual(verdict(errored), SKIP)
        self.assertEqual(verdict(no_sources), WITHHELD)  # fail-closed: no source audience
        self.assertEqual(verdict(withheld), WITHHELD)
        self.assertEqual(verdict(first, {self.uid}), WITHHELD)


# ---------------------------------------------------------------------------
# GET /api/v2/agent/shared-answers/
# ---------------------------------------------------------------------------


class ListTests(SharedAnswersTestCase):
    def mixed_history(self):
        """One of everything the rule has to tell apart."""
        s = self.make_session()
        self.collectable = self.make_run(s, query="collectable first turn")
        self.follow_up = self.make_run(s, query="follow up, never collected")
        self.errored = self.make_run(None, status="error", query="errored")
        self.no_sources = self.make_run(None, cites_task=False, query="no sources")
        self.withheld = self.make_run(None, query="withdrawn")
        AgentRun.objects.filter(pk=self.withheld.pk).update(withheld_from_team_answers=True)
        self.standalone = self.make_run(None, query="standalone")
        self.theirs = self.make_run(None, user=self.user2, query="a teammate's")

    def test_the_list_is_exactly_what_the_chunker_collects_from_the_caller(self):
        self.mixed_history()

        listed = {a["run_id"] for a in self.listed()["answers"]}

        self.assertEqual(listed, self.collected(self.user))
        self.assertEqual(listed, {str(self.collectable.run_id), str(self.standalone.run_id)})

    def test_the_two_stay_equal_as_the_choices_change(self):
        self.mixed_history()
        self.user.share_answers_with_team = False
        self.user.save(update_fields=["share_answers_with_team"])
        self.assertEqual({a["run_id"] for a in self.listed()["answers"]}, self.collected(self.user))
        self.assertEqual(self.listed()["answers"], [])

        self.user.share_answers_with_team = True
        self.user.save(update_fields=["share_answers_with_team"])
        self.assertEqual({a["run_id"] for a in self.listed()["answers"]}, self.collected(self.user))
        self.assertTrue(self.listed()["answers"])

    def test_payload_shape(self):
        run = self.make_run(
            self.make_session(),
            query="Q" * 400,
            answer="See [task:12] for the plan. " + "word " * 100,
        )

        data = self.listed()

        self.assertEqual(set(data), {"share_answers_with_team", "answers", "has_more"})
        self.assertIs(data["share_answers_with_team"], True)
        self.assertIs(data["has_more"], False)
        (item,) = data["answers"]
        self.assertEqual(
            set(item),
            {
                "run_id",
                "session_id",
                "team_id",
                "question",
                "answer_snippet",
                "started_at",
                "audience_size",
            },
        )
        self.assertEqual(item["run_id"], str(run.run_id))
        self.assertEqual(item["session_id"], str(run.session_id))
        self.assertEqual(item["team_id"], self.tid)
        self.assertEqual(len(item["question"]), 300)
        self.assertTrue(item["question"].endswith("…"))
        self.assertNotIn("[task:12]", item["answer_snippet"])  # citations stripped
        self.assertTrue(item["answer_snippet"].startswith("See for the plan."))
        self.assertLessEqual(len(item["answer_snippet"]), 283)
        self.assertEqual(item["audience_size"], 2)  # the project's two members, caller included
        self.assertEqual(item["started_at"], run.started_at.isoformat())

    def test_a_standalone_run_has_a_null_session_id(self):
        self.make_run(None)
        self.assertIsNone(self.listed()["answers"][0]["session_id"])

    def test_newest_first_and_team_filter(self):
        old = self.make_run(None, query="old", age=timedelta(days=2))
        new = self.make_run(None, query="new")
        other_team = self.make_run(None, query="elsewhere", team_id=str(uuid.uuid4()))

        ids = [a["run_id"] for a in self.listed()["answers"]]
        self.assertEqual(set(ids), {str(new.run_id), str(old.run_id), str(other_team.run_id)})
        self.assertLess(ids.index(str(new.run_id)), ids.index(str(old.run_id)))
        scoped = [a["run_id"] for a in self.listed(team_id=self.tid)["answers"]]
        self.assertEqual(set(scoped), {str(new.run_id), str(old.run_id)})

    def test_only_the_callers_own_runs(self):
        mine = self.make_run(None)
        self.make_run(None, user=self.user2)
        self.assertEqual([a["run_id"] for a in self.listed()["answers"]], [str(mine.run_id)])

    def test_has_more_when_cut_short(self):
        for i in range(3):
            self.make_run(None, query=f"q{i}")
        with mock.patch("origin.search_engine.agent_views._SHARED_ANSWERS_LIMIT", 2):
            data = self.listed()
        self.assertEqual(len(data["answers"]), 2)
        self.assertIs(data["has_more"], True)
        with mock.patch("origin.search_engine.agent_views._SHARED_ANSWERS_LIMIT", 3):
            data = self.listed()
        self.assertEqual(len(data["answers"]), 3)
        self.assertIs(data["has_more"], False)

    def test_the_audit_cap_reports_has_more(self):
        for i in range(3):
            self.make_run(None, cites_task=False, query=f"private{i}")  # never collected
        with mock.patch("origin.search_engine.agent_views._SHARED_ANSWERS_SCAN_LIMIT", 2):
            data = self.listed()
        self.assertEqual(data["answers"], [])
        self.assertIs(data["has_more"], True)

    def test_deleting_the_conversation_removes_it_from_the_list(self):
        session = self.make_session()
        run = self.make_run(session)
        other = self.make_run(self.make_session())
        self.assertIn(str(run.run_id), {a["run_id"] for a in self.listed()["answers"]})

        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(
                self.client.delete(f"{LIST_URL}{session.session_id}/").status_code, 200
            )

        self.assertEqual([a["run_id"] for a in self.listed()["answers"]], [str(other.run_id)])
        self.assertNotIn(str(run.run_id), self.collected())

    def test_needs_a_login(self):
        self.assertEqual(APIClient().get(SHARED_URL).status_code, 401)


# ---------------------------------------------------------------------------
# DELETE /api/v2/agent/shared-answers/<run_id>/
# ---------------------------------------------------------------------------


class WithdrawTests(SharedAnswersTestCase):
    def test_withdraws_flags_purges_the_team_chunk_and_keeps_the_conversation(self):
        session = self.make_session()
        run = self.make_run(session)
        other = self.make_run(self.make_session())

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            res = self.client.delete(_shared_url(run))

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"run_id": str(run.run_id), "shared": False})
        # Flagged in the DB, purge deferred to after the commit.
        run.refresh_from_db()
        self.assertTrue(run.withheld_from_team_answers)
        self.bulk.assert_not_called()
        for callback in callbacks:
            callback()
        deleted = {a["_id"] for call in self.bulk.call_args_list for a in call.args[1]}
        self.assertEqual(deleted, {f"spotlight_answer:{run.run_id}"})  # the team lane ONLY
        self.assertFalse(self.team_chunk_indexed(run))
        self.assertTrue(self.conversation_chunk_indexed(run))  # private memory stays
        self.assertTrue(self.team_chunk_indexed(other))
        # The conversation is untouched.
        self.assertTrue(self.session_exists(session))
        self.assertTrue(self.run_exists(run))
        # Gone from the list, a tombstone for the chunker, others unaffected.
        self.assertEqual(
            [a["run_id"] for a in self.client.get(SHARED_URL).data["answers"]], [str(other.run_id)]
        )
        self.assertIs(self.team_lane()[str(run.run_id)], False)

    def test_it_is_idempotent(self):
        run = self.make_run(None)
        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.client.delete(_shared_url(run)).status_code, 200)
        run.refresh_from_db()
        self.assertTrue(run.withheld_from_team_answers)

    def test_an_answer_that_was_never_collected_is_still_a_200(self):
        run = self.make_run(None, cites_task=False)  # private sources
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(_shared_url(run)).status_code, 200)

    def test_another_users_run_is_the_same_404_as_a_missing_one(self):
        theirs = self.make_run(None, user=self.user2)

        foreign = self.client.delete(_shared_url(theirs))
        missing = self.client.delete(f"{SHARED_URL}{uuid.uuid4()}/")
        malformed = self.client.delete(f"{SHARED_URL}not-a-uuid/")

        for res in (foreign, missing, malformed):
            self.assertEqual(res.status_code, 404)
        self.assertEqual(foreign.data, missing.data)
        self.assertEqual(foreign.data, malformed.data)
        theirs.refresh_from_db()
        self.assertFalse(theirs.withheld_from_team_answers)
        self.assertTrue(self.team_chunk_indexed(theirs))
        self.bulk.assert_not_called()

    def test_needs_a_login(self):
        run = self.make_run(None)
        self.assertEqual(APIClient().delete(_shared_url(run)).status_code, 401)
        run.refresh_from_db()
        self.assertFalse(run.withheld_from_team_answers)

    def test_an_opensearch_failure_does_not_fail_it_and_the_sweep_finishes_it(self):
        run = self.make_run(None)
        healthy = self.make_run(None)
        self.bulk.side_effect = ConnectionError("opensearch is down")

        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.delete(_shared_url(run))

        self.assertEqual(res.status_code, 200)
        run.refresh_from_db()
        self.assertTrue(run.withheld_from_team_answers)
        self.assertTrue(self.team_chunk_indexed(run))

        self.bulk.side_effect = self.index.bulk
        purge.sweep_orphans()

        self.assertFalse(self.team_chunk_indexed(run))
        # The sweep must not take healthy chunks, or either run's private memory.
        self.assertTrue(self.team_chunk_indexed(healthy))
        self.assertTrue(self.conversation_chunk_indexed(healthy))
        self.assertTrue(self.conversation_chunk_indexed(run))
        self.assertEqual(
            self.tracked(healthy),
            {f"conversation:{healthy.run_id}", f"spotlight_answer:{healthy.run_id}"},
        )
        self.assertEqual(
            RagChunk.objects.filter(entity_id=f"spotlight_answer:{run.run_id}").count(), 0
        )
