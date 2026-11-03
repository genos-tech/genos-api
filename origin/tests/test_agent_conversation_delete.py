"""Deleting Genos conversations (the history list's "delete" and "delete all").

What Genos remembers about a person is more than the rows the list shows:

  * `AgentSession` / `AgentRun` (question, answer, steps, calls, judgements,
    feedback) -- including rows the tier history window hides, and standalone
    runs that belong to no session;
  * the session's rolling summary (a column on the session);
  * two OpenSearch lanes built off the runs: `conversation:<run_id>` (the
    asker's own recall, read by `search_past_conversations`) and
    `spotlight_answer:<run_id>` (the anonymous team lane).

These tests pin that a delete erases all of it, and only the caller's. The
traps they exist for: `AgentRun.session` is SET_NULL, so deleting just the
session leaves the runs alive -- and a former follow-up with a NULL session
reads as a standalone first turn to the team-lane chunker; the history window
is not a reason to keep a row; and the index purge must run after the commit
and must never be able to fail the delete.

No LLM and no OpenSearch: the bulk client is replaced by an in-memory index
(`FakeIndex`) that honours deletes, and `search` is replaced by a lookup into it.
"""

from datetime import timedelta
from unittest import mock
from urllib.parse import urlencode

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine import purge, quota
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.search_past_conversations import (
    _run as search_past_conversations_run,
)
from origin.search_engine.agent_views import _load_prior_turns
from origin.search_engine.chunkers.conversation_chunker import (
    conversation_chunks_for_run,
    iter_conversation_chunks,
)
from origin.search_engine.chunkers.spotlight_answer_chunker import iter_spotlight_answer_chunks
from origin.search_engine.models import (
    AgentLlmCall,
    AgentRun,
    AgentRunFeedback,
    AgentRunJudgement,
    AgentSession,
    AgentStep,
    AiRequestCost,
    AiSpendEvent,
    RagChunk,
)

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas

LIST_URL = "/api/v2/agent/sessions/"

_WINDOWED_QUOTAS = {
    **TEST_QUOTAS,
    "free": {**TEST_QUOTAS["free"], "agent_history_retention_days": 30},
}

_OLD = timedelta(days=90)  # well past the 30-day window above


def _detail_url(session):
    return f"{LIST_URL}{session.session_id}/"


class FakeIndex:
    """An in-memory stand-in for the OpenSearch index: `bulk` honours deletes."""

    def __init__(self):
        self.docs: dict[str, dict] = {}

    def add(self, chunk):
        self.docs[chunk.chunk_id] = {
            "chunk_id": chunk.chunk_id,
            "entity_type": chunk.entity_type,
            "entity_id": chunk.entity_id,
            "title": chunk.title,
            "snippet": chunk.snippet_text,
            "text": chunk.search_text,
            "acl_user_ids": list(chunk.acl_user_ids),
            "updated_at": chunk.updated_at,
        }

    def bulk(self, client, actions, **kwargs):
        actions = list(actions)
        for action in actions:
            assert action["_op_type"] == "delete"
            self.docs.pop(action["_id"], None)
        return len(actions), []

    def search(self, *, query, team_id, user_id, entity_types, **kwargs):
        """What `search(entity_types=["conversation"])` returns: the lane's
        docs the user is on the ACL of (the ACL is enforced by the index)."""
        results = [
            {
                "entity_type": d["entity_type"],
                "entity_id": d["entity_id"],
                "title": d["title"],
                "snippet": d["snippet"],
                "chunks": [{"chunk_id": d["chunk_id"], "text": d["text"]}],
                "updated_at": d["updated_at"],
            }
            for d in self.docs.values()
            if d["entity_type"] in entity_types and user_id in d["acl_user_ids"]
        ]
        return {"results": results}


class ConversationDeleteTestCase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        quota.invalidate_effective_tier([self.user.id, self.user2.id])
        self.uid = str(self.user.id)
        self.uid2 = str(self.user2.id)
        self.tid = str(self.team.pk)

        self.index = FakeIndex()
        self.get_client = mock.MagicMock()
        for patcher in (
            mock.patch.object(purge.os_helpers, "bulk", side_effect=self.index.bulk),
            mock.patch.object(purge, "get_client", self.get_client),
            mock.patch.object(purge, "get_index_alias", return_value="test-alias"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.bulk = purge.os_helpers.bulk

        # A project + task both users can see, so a run that fetched it is
        # collectable into the team lane.
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Delete Project",
            owner=self.user,
            project_system_user=self.user,
        )
        for u in (self.user, self.user2):
            ProjectMembers.objects.get_or_create(project=self.project, attendee=u)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id, self.user2.id])
        super().tearDown()

    # ---- builders ---------------------------------------------------------

    def make_session(self, *, user=None, team_id=None, age=None, **scope):
        user = user or self.user
        kwargs = dict(scope)
        if age is not None:
            kwargs["last_active_at"] = timezone.now() - age
        return AgentSession.objects.create(
            team_id=team_id or self.tid, user_id=str(user.id), **kwargs
        )

    def make_run(
        self,
        session=None,
        *,
        user=None,
        team_id=None,
        status="done",
        query="how do we ship?",
        answer="Ship it on Friday.",
        age=None,
        cites_task=True,
        index=None,
    ):
        """A run (optionally old), with the chunks both lanes would hold."""
        user = user or self.user
        run = AgentRun.objects.create(
            session=session,
            team_id=team_id or self.tid,
            user_id=str(user.id),
            query=query,
            status=status,
            final_answer_text=answer,
        )
        if age is not None:
            AgentRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - age)
            run.refresh_from_db()
        if cites_task:
            AgentStep.objects.create(
                run=run,
                step_index=0,
                tool_name="fetch_task",
                result_json={
                    "task_id": self.task.task_id,
                    "title": "T",
                    "project_id": self.project.project_id,
                },
            )
        if index if index is not None else status == "done":
            self.index_run(run)
        return run

    def index_run(self, run):
        """Put the run's chunks in the fake index + RagChunk, as a reindex
        would. The conversation chunk is built by the real chunker; the team
        lane's is a stand-in doc (its content is not under test here)."""
        entity = conversation_chunks_for_run(run)
        self.index.add(entity.chunks[0])
        for lane in ("conversation", "spotlight_answer"):
            cid = f"{lane}:{run.run_id}"
            RagChunk.objects.create(
                chunk_id=cid,
                entity_type=lane,
                entity_id=cid,
                chunk_type=lane,
                team_id=run.team_id,
                text_hash="h",
                embedding_model="m",
            )
            if lane == "spotlight_answer":
                self.index.docs[cid] = {
                    "chunk_id": cid,
                    "entity_type": lane,
                    "entity_id": cid,
                    "acl_user_ids": [str(self.user.id), str(self.user2.id)],
                }

    def lane_ids(self, run):
        return {f"conversation:{run.run_id}", f"spotlight_answer:{run.run_id}"}

    def indexed(self, run):
        return self.lane_ids(run) & set(self.index.docs)

    def tracked(self, run):
        return set(
            RagChunk.objects.filter(
                entity_id__in=[f"conversation:{run.run_id}", f"spotlight_answer:{run.run_id}"]
            ).values_list("entity_id", flat=True)
        )

    def delete(self, url, params=None, *, client=None):
        """Issue a DELETE and run its on_commit callbacks (the test's own
        transaction never commits, so Django would otherwise drop them)."""
        if params:
            url = f"{url}?{urlencode(params)}"
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.client).delete(url)

    def run_exists(self, run):
        return AgentRun.objects.filter(pk=run.pk).exists()

    def session_exists(self, session):
        return AgentSession.objects.filter(pk=session.pk).exists()


# ---------------------------------------------------------------------------
# DELETE /api/v2/agent/sessions/<id>/
# ---------------------------------------------------------------------------


class DeleteSessionTests(ConversationDeleteTestCase):
    def test_deletes_the_session_and_all_of_its_runs(self):
        session = self.make_session(
            rolling_summary_text="SECRET-SUMMARY", rolling_summary_through=2
        )
        runs = [self.make_run(session, query=f"q{i}") for i in range(3)]

        res = self.delete(_detail_url(session))

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"deleted_sessions": 1, "deleted_runs": 3})
        self.assertFalse(self.session_exists(session))
        for run in runs:
            self.assertFalse(self.run_exists(run))
        # The rolling summary lives on the session, so it goes with it.
        self.assertFalse(
            AgentSession.objects.filter(rolling_summary_text="SECRET-SUMMARY").exists()
        )

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(_WINDOWED_QUOTAS))
    def test_deletes_runs_and_sessions_the_history_window_hides(self):
        # A downgraded user can no longer see this session (the list hides it
        # and the detail 404s), but it is still their data and still erasable.
        hidden = self.make_session(age=_OLD, rolling_summary_text="HIDDEN-SUMMARY")
        old_runs = [self.make_run(hidden, age=_OLD), self.make_run(hidden, age=_OLD)]
        listed = self.client.get(LIST_URL, {"team_id": self.tid}).data["sessions"]
        self.assertEqual(listed, [])  # the window really is hiding it
        self.assertEqual(
            self.client.get(_detail_url(hidden), {"team_id": self.tid}).status_code, 404
        )

        res = self.delete(_detail_url(hidden))

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["deleted_runs"], 2)
        self.assertFalse(self.session_exists(hidden))
        for run in old_runs:
            self.assertFalse(self.run_exists(run))
            self.assertEqual(self.indexed(run), set())

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(_WINDOWED_QUOTAS))
    def test_deletes_old_runs_inside_a_visible_session(self):
        # The window is session-anchored: a fresh session keeps its old runs
        # visible, but a delete must take them regardless of age.
        session = self.make_session()
        old = self.make_run(session, age=_OLD)
        new = self.make_run(session)

        self.assertEqual(self.delete(_detail_url(session)).status_code, 200)

        self.assertFalse(self.run_exists(old))
        self.assertFalse(self.run_exists(new))

    def test_thread_and_note_scoped_sessions_are_deletable(self):
        import uuid

        thread = self.make_session(chat_type=3, chat_id=uuid.uuid4(), thread_id=uuid.uuid4())
        note = self.make_session(note_type=1, note_id=7)
        runs = [self.make_run(thread), self.make_run(note)]

        for session in (thread, note):
            res = self.delete(_detail_url(session))
            self.assertEqual(res.status_code, 200, session)
            self.assertFalse(self.session_exists(session))
        for run in runs:
            self.assertFalse(self.run_exists(run))

    def test_another_users_session_is_the_same_404_as_a_missing_one(self):
        theirs = self.make_session(user=self.user2)
        their_run = self.make_run(theirs, user=self.user2)

        foreign = self.delete(_detail_url(theirs))
        missing = self.delete("/api/v2/agent/sessions/00000000-0000-4000-8000-000000000000/")
        malformed = self.delete("/api/v2/agent/sessions/not-a-uuid/")

        for res in (foreign, missing, malformed):
            self.assertEqual(res.status_code, 404)
        self.assertEqual(foreign.data, missing.data)
        self.assertEqual(foreign.data, malformed.data)
        self.assertTrue(self.session_exists(theirs))
        self.assertTrue(self.run_exists(their_run))
        self.assertEqual(self.indexed(their_run), self.lane_ids(their_run))
        self.bulk.assert_not_called()

    def test_leaves_the_callers_other_sessions_alone(self):
        keep = self.make_session()
        keep_run = self.make_run(keep)
        drop = self.make_session()
        self.make_run(drop)

        self.delete(_detail_url(drop))

        self.assertTrue(self.session_exists(keep))
        self.assertTrue(self.run_exists(keep_run))
        self.assertEqual(self.indexed(keep_run), self.lane_ids(keep_run))

    def test_requires_authentication(self):
        session = self.make_session()
        res = self.delete(_detail_url(session), client=APIClient())
        self.assertEqual(res.status_code, 401)
        self.assertTrue(self.session_exists(session))

    def test_cascades_to_the_runs_dependent_rows(self):
        session = self.make_session()
        run = self.make_run(session)
        AgentLlmCall.objects.create(run=run, team_id=self.tid)
        AgentRunJudgement.objects.create(run=run, team_id=self.tid)
        # A rating a teammate left on the run goes with it.
        AgentRunFeedback.objects.create(run=run, team_id=self.tid, user_id=self.uid2, rating=1)

        self.delete(_detail_url(session))

        self.assertFalse(AgentStep.objects.filter(run_id=run.run_id).exists())
        self.assertFalse(AgentLlmCall.objects.filter(run_id=run.run_id).exists())
        self.assertFalse(AgentRunJudgement.objects.filter(run_id=run.run_id).exists())
        self.assertFalse(AgentRunFeedback.objects.filter(run_id=run.run_id).exists())

    def test_cost_ledger_rows_survive(self):
        import uuid

        session = self.make_session()
        run = self.make_run(session)
        cost = AiRequestCost.objects.create(
            request_id=uuid.uuid4(),
            run_id=run.run_id,
            user_id=self.uid,
            team_id=self.tid,
            started_at=timezone.now(),
        )
        event = AiSpendEvent.objects.create(
            request_id=uuid.uuid4(), run_id=run.run_id, user_id=self.uid, team_id=self.tid
        )

        self.assertEqual(self.delete(_detail_url(session)).status_code, 200)

        self.assertTrue(AiRequestCost.objects.filter(pk=cost.pk).exists())
        self.assertTrue(AiSpendEvent.objects.filter(pk=event.pk).exists())

    def test_a_running_run_blocks_the_delete_and_nothing_is_deleted(self):
        session = self.make_session()
        done = self.make_run(session)
        live = self.make_run(session, status="running", answer="")

        res = self.delete(_detail_url(session))

        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "run_in_flight")
        self.assertTrue(self.session_exists(session))
        self.assertTrue(self.run_exists(done))
        self.assertTrue(self.run_exists(live))
        self.assertEqual(self.indexed(done), self.lane_ids(done))
        self.bulk.assert_not_called()

    def test_a_run_awaiting_approval_does_not_block_the_delete(self):
        # Paused with no worker alive; /decide/ for it will 404.
        session = self.make_session()
        paused = self.make_run(session, status="awaiting_approval", answer="partial")

        res = self.delete(_detail_url(session))

        self.assertEqual(res.status_code, 200)
        self.assertFalse(self.run_exists(paused))


# ---------------------------------------------------------------------------
# DELETE /api/v2/agent/sessions/?team_id=
# ---------------------------------------------------------------------------


class DeleteAllTests(ConversationDeleteTestCase):
    def setUp(self):
        super().setUp()
        self.other_team = TeamMaster.objects.create(
            team_name="Other Team", team_email="other-team@example.com", owner=self.user
        )
        self.other_tid = str(self.other_team.pk)

    def test_requires_team_id(self):
        session = self.make_session()
        res = self.delete(LIST_URL)
        self.assertEqual(res.status_code, 400)
        self.assertTrue(self.session_exists(session))

    def test_deletes_all_of_the_callers_data_in_the_team_including_sessionless_runs(self):
        s1 = self.make_session()
        s2 = self.make_session(note_type=1, note_id=3)
        in_session = [self.make_run(s1), self.make_run(s1), self.make_run(s2)]
        standalone = self.make_run(None)  # no session at all

        res = self.delete(LIST_URL, {"team_id": self.tid})

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"deleted_sessions": 2, "deleted_runs": 4})
        for session in (s1, s2):
            self.assertFalse(self.session_exists(session))
        for run in [*in_session, standalone]:
            self.assertFalse(self.run_exists(run))
            self.assertEqual(self.indexed(run), set())
            self.assertEqual(self.tracked(run), set())

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(_WINDOWED_QUOTAS))
    def test_includes_what_the_history_window_hides(self):
        hidden = self.make_session(age=_OLD)
        hidden_run = self.make_run(hidden, age=_OLD)
        old_standalone = self.make_run(None, age=_OLD)

        res = self.delete(LIST_URL, {"team_id": self.tid})

        self.assertEqual(res.data, {"deleted_sessions": 1, "deleted_runs": 2})
        self.assertFalse(self.session_exists(hidden))
        self.assertFalse(self.run_exists(hidden_run))
        self.assertFalse(self.run_exists(old_standalone))

    def test_leaves_other_users_and_other_teams_alone(self):
        mine = self.make_session()
        my_run = self.make_run(mine)
        # Another member of the SAME team.
        theirs = self.make_session(user=self.user2)
        their_run = self.make_run(theirs, user=self.user2)
        their_standalone = self.make_run(None, user=self.user2)
        # The same user in ANOTHER team.
        elsewhere = self.make_session(team_id=self.other_tid)
        elsewhere_run = self.make_run(elsewhere, team_id=self.other_tid)
        elsewhere_standalone = self.make_run(None, team_id=self.other_tid)

        res = self.delete(LIST_URL, {"team_id": self.tid})

        self.assertEqual(res.data, {"deleted_sessions": 1, "deleted_runs": 1})
        self.assertFalse(self.session_exists(mine))
        self.assertFalse(self.run_exists(my_run))
        for session in (theirs, elsewhere):
            self.assertTrue(self.session_exists(session))
        for run in (their_run, their_standalone, elsewhere_run, elsewhere_standalone):
            self.assertTrue(self.run_exists(run))
            self.assertEqual(self.indexed(run), self.lane_ids(run))

    def test_with_nothing_to_delete_is_a_200_with_zeros(self):
        res = self.delete(LIST_URL, {"team_id": self.tid})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"deleted_sessions": 0, "deleted_runs": 0})
        self.bulk.assert_not_called()

    def test_a_running_run_blocks_everything(self):
        session = self.make_session()
        done = self.make_run(session)
        self.make_run(None, status="running", answer="")

        res = self.delete(LIST_URL, {"team_id": self.tid})

        self.assertEqual(res.status_code, 409)
        self.assertTrue(self.session_exists(session))
        self.assertTrue(self.run_exists(done))
        self.bulk.assert_not_called()

    def test_a_running_run_in_another_team_or_of_another_user_does_not_block(self):
        self.make_run(None, status="running", answer="", team_id=self.other_tid)
        self.make_run(None, status="running", answer="", user=self.user2)
        mine = self.make_run(None)

        res = self.delete(LIST_URL, {"team_id": self.tid})

        self.assertEqual(res.status_code, 200)
        self.assertFalse(self.run_exists(mine))


# ---------------------------------------------------------------------------
# The index purge
# ---------------------------------------------------------------------------


class PurgeTests(ConversationDeleteTestCase):
    def test_purges_both_lanes_for_every_deleted_run_after_commit(self):
        session = self.make_session()
        runs = [self.make_run(session), self.make_run(session)]
        bystander = self.make_run(self.make_session())
        before = set(self.index.docs)

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            res = self.client.delete(_detail_url(session))

        self.assertEqual(res.status_code, 200)
        # Not before the DB delete commits ...
        self.bulk.assert_not_called()
        self.assertEqual(set(self.index.docs), before)
        self.assertEqual(len(callbacks), 1)
        # ... and then exactly the deleted runs' two lanes.
        for callback in callbacks:
            callback()
        deleted_ids = {
            action["_id"] for call in self.bulk.call_args_list for action in call.args[1]
        }
        self.assertEqual(deleted_ids, self.lane_ids(runs[0]) | self.lane_ids(runs[1]))
        for run in runs:
            self.assertEqual(self.indexed(run), set())
            self.assertEqual(self.tracked(run), set())
        self.assertEqual(self.indexed(bystander), self.lane_ids(bystander))

    def test_purges_even_when_the_run_has_no_tracking_row(self):
        # RagChunk drifted from the index: the document exists, nothing tracks it.
        session = self.make_session()
        run = self.make_run(session)
        RagChunk.objects.filter(entity_id__endswith=str(run.run_id)).delete()

        self.delete(_detail_url(session))

        self.assertEqual(self.indexed(run), set())

    def test_an_opensearch_failure_does_not_undo_the_delete(self):
        session = self.make_session(rolling_summary_text="X")
        run = self.make_run(session)
        self.bulk.side_effect = ConnectionError("opensearch is down")

        res = self.delete(_detail_url(session))

        self.assertEqual(res.status_code, 200)
        self.assertFalse(self.session_exists(session))
        self.assertFalse(self.run_exists(run))

    def test_a_client_that_cannot_be_built_does_not_undo_the_delete(self):
        session = self.make_session()
        run = self.make_run(session)
        self.get_client.side_effect = RuntimeError("no opensearch configured")

        res = self.delete(_detail_url(session))

        self.assertEqual(res.status_code, 200)
        self.assertFalse(self.run_exists(run))

    def test_delete_all_survives_an_opensearch_failure_too(self):
        run = self.make_run(None)
        self.bulk.side_effect = ConnectionError("opensearch is down")

        res = self.delete(LIST_URL, {"team_id": self.tid})

        self.assertEqual(res.status_code, 200)
        self.assertFalse(self.run_exists(run))

    def test_the_sweep_finishes_a_purge_that_opensearch_missed(self):
        # The DB delete went through while OpenSearch was down: the tracking
        # rows are kept, and because the run row is gone the daily sweep
        # reports the entity dead and removes the chunks.
        session = self.make_session()
        run = self.make_run(session)
        self.bulk.side_effect = ConnectionError("opensearch is down")
        self.delete(_detail_url(session))
        self.assertEqual(self.indexed(run), self.lane_ids(run))
        self.assertEqual(self.tracked(run), self.lane_ids(run))

        self.bulk.side_effect = self.index.bulk  # OpenSearch is back
        stats = purge.sweep_orphans()

        self.assertEqual(stats["chunks_purged"], 2)
        self.assertEqual(self.indexed(run), set())
        self.assertEqual(self.tracked(run), set())


# ---------------------------------------------------------------------------
# A deleted conversation no longer affects answers
# ---------------------------------------------------------------------------


class AfterDeleteTests(ConversationDeleteTestCase):
    def test_prior_turns_no_longer_load_it(self):
        session = self.make_session()
        self.make_run(session, query="remember the budget", answer="It is 5.")
        self.assertEqual(len(_load_prior_turns(session, max_turns=5)), 1)

        self.delete(_detail_url(session))

        self.assertEqual(_load_prior_turns(session, max_turns=5), [])
        self.assertFalse(AgentRun.objects.filter(query="remember the budget").exists())

    def test_search_past_conversations_no_longer_returns_it(self):
        session = self.make_session()
        kept = self.make_run(self.make_session(), query="kept question")
        gone = self.make_run(session, query="deleted question")
        ctx = ToolContext(team_id=self.tid, user_id=self.uid)

        def titles():
            with mock.patch(
                "origin.search_engine.agent.tools.search_past_conversations.search",
                side_effect=self.index.search,
            ):
                out = search_past_conversations_run({"query": "question"}, ctx)
            return {m["question"] for m in out["matches"]}

        self.assertEqual(titles(), {"kept question", "deleted question"})

        self.delete(_detail_url(session))

        self.assertEqual(titles(), {"kept question"})
        self.assertEqual(self.indexed(gone), set())
        self.assertEqual(self.indexed(kept), self.lane_ids(kept))

    def test_neither_chunker_emits_the_deleted_runs(self):
        session = self.make_session()
        first = self.make_run(session, query="first turn")
        follow_up = self.make_run(session, query="follow up")
        gone = {str(first.run_id), str(follow_up.run_id)}
        survivor = self.make_run(self.make_session(), query="survivor")

        def emitted():
            conv = {e.entity_id.split(":", 1)[1] for e in iter_conversation_chunks() if e.chunks}
            team = {
                e.entity_id.split(":", 1)[1] for e in iter_spotlight_answer_chunks() if e.chunks
            }
            return conv, team

        conv, team = emitted()
        # Sanity: the fixtures really are collectable, so the assertions
        # below are not vacuous.
        self.assertTrue(gone <= conv)
        self.assertIn(str(first.run_id), team)
        self.assertNotIn(str(follow_up.run_id), team)  # follow-ups are never collected

        self.delete(_detail_url(session))
        conv, team = emitted()

        self.assertEqual(conv & gone, set())
        self.assertEqual(team & gone, set())
        self.assertIn(str(survivor.run_id), conv)
        self.assertIn(str(survivor.run_id), team)

    def test_a_deleted_follow_up_cannot_become_a_team_lane_first_turn(self):
        # The SET_NULL trap: delete only the session and the follow-up keeps
        # a NULL session, which `_is_first_turn` reads as a standalone ask.
        session = self.make_session()
        self.make_run(session, query="first turn")
        follow_up = self.make_run(session, query="follow up")

        self.delete(_detail_url(session))

        collected = {e.entity_id for e in iter_spotlight_answer_chunks()}
        self.assertNotIn(f"spotlight_answer:{follow_up.run_id}", collected)
        self.assertFalse(self.run_exists(follow_up))

    def test_delete_all_also_removes_sessionless_runs_from_both_chunkers(self):
        standalone = self.make_run(None, query="standalone")

        self.delete(LIST_URL, {"team_id": self.tid})

        self.assertEqual([e for e in iter_conversation_chunks() if e.chunks], [])
        self.assertEqual([e for e in iter_spotlight_answer_chunks() if e.chunks], [])
        self.assertEqual(self.indexed(standalone), set())
