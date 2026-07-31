"""Attribution completeness — every paid surface binds a spend context.

Phase 0 shipped the meter with exactly one fully-bound surface (the ask
path) and a tripwire (`surface="unattributed"`) for everything else. The
tripwire fired, as designed, on five real gaps: plain `/search/`, both
summary endpoints, the judge cron, the `/decide/` resume leg — and on
the post-run conversation embed, which runs in the stream's `finally`
AFTER the ask's own context has exited.

These tests are the acceptance check for closing them: run each surface
and assert the `unattributed` count is ZERO. That assertion is the
product here — the credit engine charges by `request_id`, so an
unattributed call is spend nobody can ever be asked about, and two of
these surfaces already charge the user a daily ask.

The other structural rule guarded here: a surface must post an
`AiRequestCost` rollup only when it actually spent. A cache hit that
opened a rollup would mint ¥0 phantom requests into every per-request
average the pricing work depends on.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings as dj_settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine import metered
from origin.search_engine.agent.thread_summary import ThreadSummaryError
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage
from origin.search_engine.models import AgentRun, AiRequestCost, AiSpendEvent
from origin.tests.test_base import BaseAPITestCase


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


METER_ON = override_settings(SEARCH_ENGINE=_se(AI_COST_METER=True))
METER_OFF = override_settings(SEARCH_ENGINE=_se(AI_COST_METER=False))


class _StreamingAPIBase(TransactionTestCase):
    """`BaseAPITestCase`'s fixture on a `TransactionTestCase`.

    The streaming flows (`/ask/`, `/decide/`) run their fake agent on
    the real worker thread, whose DB connection sits OUTSIDE a
    `TestCase` transaction — its rows commit for real and would leak
    into every later test (which is exactly how this file first
    failed). `TransactionTestCase` truncates after each test, and the
    runner orders these classes last, so the leak cannot reach the
    plain `TestCase` classes above. The worker is joined by the stream
    itself before any assertion, so none of the thread-race flakiness
    the sprint-bootstrap tests warn about applies here.
    """

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="testuser", email="test@example.com", password="testpass123"
        )
        self.team = TeamMaster.objects.create(
            team_name="Test Team", team_email="team@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)

    def authenticate(self):
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")


def _usage(model="gemini-3.6-flash", provider="gemini", **kw):
    u = CallUsage(provider=provider, model=model)
    for k, v in kw.items():
        setattr(u, k, v)
    return u


def _record_fake_llm_call():
    """Stand-in for 'the mocked-out code path made one paid call'."""
    spend.record_llm_call(_usage(prompt_tokens=100, output_tokens=20))


class _RestoresRecorder:
    """`spend._recorder` is process-global; restore it after each test."""

    def setUp(self):
        super().setUp()
        original = spend._recorder
        self.addCleanup(spend.set_recorder, original)


def _assert_nothing_unattributed(testcase):
    testcase.assertEqual(
        AiSpendEvent.objects.filter(surface=spend.SURFACE_UNATTRIBUTED).count(),
        0,
        "unattributed spend means an entry point is missing its bind — "
        "the exact defect this PR exists to close",
    )


# --------------------------------------------------------------------------- #
# The helper itself                                                           #
# --------------------------------------------------------------------------- #


class MeteredRequestHelperTests(_RestoresRecorder, TestCase):
    """`metered.metered_request` — the lifecycle every non-streaming
    surface now runs through."""

    @METER_ON
    def test_success_writes_a_closed_rollup(self):
        with metered.metered_request(surface="search", user_id="u1", team_id="t1"):
            _record_fake_llm_call()
        row = AiRequestCost.objects.get(surface="search")
        self.assertEqual(row.result, AiRequestCost.RESULT_SUCCESS)
        self.assertIsNotNone(row.finished_at)
        self.assertEqual(row.call_count, 1)
        self.assertGreater(row.computed_jpy_milli, 0)
        _assert_nothing_unattributed(self)

    @METER_ON
    def test_marked_outcome_lands_on_the_rollup(self):
        with metered.metered_request(surface="search", user_id="u1") as outcome:
            _record_fake_llm_call()
            outcome.mark(AiRequestCost.RESULT_PROVIDER_FAILURE)
        row = AiRequestCost.objects.get(surface="search")
        self.assertEqual(row.result, AiRequestCost.RESULT_PROVIDER_FAILURE)
        # A failed request is never charged — the structural rule.
        self.assertEqual(row.charged_jpy_milli, 0)

    @METER_ON
    def test_an_escaping_exception_is_an_application_failure_and_reraises(self):
        with self.assertRaises(ValueError):
            with metered.metered_request(surface="search", user_id="u1"):
                _record_fake_llm_call()
                raise ValueError("boom")
        row = AiRequestCost.objects.get(surface="search")
        self.assertEqual(row.result, AiRequestCost.RESULT_APPLICATION_FAILURE)
        self.assertIsNone(spend.current_context(), "context must not leak past a raise")

    @METER_ON
    def test_nested_inside_another_request_opens_no_second_rollup(self):
        """`search()` runs inside an ask via the search_kb tool. The
        inner bind must neither fragment the spend nor mint a ¥0 row."""
        outer_id = str(uuid.uuid4())
        with spend.spend_context(surface="ask", user_id="u1", request_id=outer_id):
            with metered.metered_request(surface="search", user_id="u1"):
                _record_fake_llm_call()
        self.assertEqual(AiRequestCost.objects.count(), 0)
        event = AiSpendEvent.objects.get()
        self.assertEqual(event.surface, "ask")
        self.assertEqual(str(event.request_id), outer_id)

    @METER_ON
    def test_context_is_none_after_every_exit(self):
        with metered.metered_request(surface="search"):
            pass
        self.assertIsNone(spend.current_context())


# --------------------------------------------------------------------------- #
# /search/                                                                    #
# --------------------------------------------------------------------------- #


class SearchViewAttributionTests(_RestoresRecorder, BaseAPITestCase):
    URL = "/api/v2/search/"

    def _post(self):
        self.authenticate()
        return self.client.post(
            self.URL,
            {"query": "payment retries", "team_id": str(self.team.pk)},
            format="json",
            HTTP_HOST="localhost",
        )

    @METER_ON
    def test_search_spend_is_attributed(self):
        def fake_search(**kwargs):
            _record_fake_llm_call()  # the query rewrite
            return {"results": []}

        with patch("origin.search_engine.views.search", side_effect=fake_search):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)

        event = AiSpendEvent.objects.get()
        self.assertEqual(event.surface, "search")
        self.assertEqual(event.user_id, str(self.user.id))
        row = AiRequestCost.objects.get(surface="search")
        self.assertEqual(row.result, AiRequestCost.RESULT_SUCCESS)
        self.assertEqual(str(event.request_id), str(row.request_id))
        _assert_nothing_unattributed(self)
        self.assertIsNone(spend.current_context())

    @METER_OFF
    def test_meter_off_writes_nothing_and_serves_normally(self):
        with patch("origin.search_engine.views.search", return_value={"results": []}):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AiSpendEvent.objects.count(), 0)
        self.assertEqual(AiRequestCost.objects.count(), 0)


# --------------------------------------------------------------------------- #
# /agent/thread-summary/ and /agent/note-summary/                             #
# --------------------------------------------------------------------------- #


class ThreadSummaryAttributionTests(_RestoresRecorder, BaseAPITestCase):
    URL = "/api/v2/agent/thread-summary/"

    def _post(self):
        self.authenticate()
        return self.client.post(
            self.URL,
            {
                "team_id": str(self.team.pk),
                "chat_type": 2,
                "chat_id": str(uuid.uuid4()),
                "thread_id": str(uuid.uuid4()),
            },
            format="json",
            HTTP_HOST="localhost",
        )

    def _patches(self, *, cached, regen=None):
        messages = [{"m": 1}]
        result = SimpleNamespace(
            summary="s",
            last_updated=timezone.now(),
            message_count=1,
            fingerprint="fp",
        )
        return (
            patch(
                "origin.search_engine.agent_views.peek_cached_summary",
                return_value=(cached, messages, "fp"),
            ),
            patch(
                "origin.search_engine.agent_views.regenerate_summary",
                side_effect=regen or (lambda **kw: (_record_fake_llm_call(), result)[1]),
            ),
            patch(
                "origin.search_engine.agent_views._thread_session_payload",
                return_value={},
            ),
        )

    @METER_ON
    def test_regeneration_is_its_own_logical_request(self):
        p1, p2, p3 = self._patches(cached=None)
        with p1, p2, p3:
            resp = self._post()
        self.assertEqual(resp.status_code, 200)

        event = AiSpendEvent.objects.get()
        self.assertEqual(event.surface, "thread_summary")
        row = AiRequestCost.objects.get(surface="thread_summary")
        self.assertEqual(row.result, AiRequestCost.RESULT_SUCCESS)
        self.assertEqual(row.user_id, str(self.user.id))
        _assert_nothing_unattributed(self)

    @METER_ON
    def test_a_cache_hit_posts_no_rollup(self):
        """This surface charges quota only on regeneration; the meter
        must follow the same line or every cache hit becomes a ¥0
        request diluting the per-request averages."""
        cached = SimpleNamespace(
            summary="s",
            last_updated=timezone.now(),
            message_count=1,
            fingerprint="fp",
        )
        p1, p2, p3 = self._patches(cached=cached)
        with p1, p2, p3:
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AiRequestCost.objects.count(), 0)
        self.assertEqual(AiSpendEvent.objects.count(), 0)

    @METER_ON
    def test_a_generation_failure_closes_as_provider_failure(self):
        def boom(**kw):
            _record_fake_llm_call()  # the stream died after spending
            raise ThreadSummaryError("provider fell over")

        p1, p2, p3 = self._patches(cached=None, regen=boom)
        with p1, p2, p3:
            resp = self._post()
        self.assertEqual(resp.status_code, 503)
        row = AiRequestCost.objects.get(surface="thread_summary")
        self.assertEqual(row.result, AiRequestCost.RESULT_PROVIDER_FAILURE)
        self.assertEqual(row.charged_jpy_milli, 0, "a failed request is never charged")


class NoteSummaryAttributionTests(_RestoresRecorder, BaseAPITestCase):
    URL = "/api/v2/agent/note-summary/"

    @METER_ON
    def test_regeneration_is_its_own_logical_request(self):
        record = SimpleNamespace(title="T", body_text="body")
        result = SimpleNamespace(
            summary="s",
            last_updated=timezone.now(),
            body_length=4,
            fingerprint="fp",
        )

        def fake_regen(**kw):
            _record_fake_llm_call()
            return result

        self.authenticate()
        with (
            patch(
                "origin.search_engine.agent_views.peek_cached_note_summary",
                return_value=(None, record, "fp"),
            ),
            patch(
                "origin.search_engine.agent_views.regenerate_note_summary",
                side_effect=fake_regen,
            ),
            patch(
                "origin.search_engine.agent_views._note_session_payload",
                return_value={},
            ),
        ):
            resp = self.client.post(
                self.URL,
                {"team_id": str(self.team.pk), "note_type": 1, "note_id": 7},
                format="json",
                HTTP_HOST="localhost",
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AiSpendEvent.objects.get().surface, "note_summary")
        row = AiRequestCost.objects.get(surface="note_summary")
        self.assertEqual(row.result, AiRequestCost.RESULT_SUCCESS)
        _assert_nothing_unattributed(self)


# --------------------------------------------------------------------------- #
# The judge cron                                                              #
# --------------------------------------------------------------------------- #


class JudgeCronAttributionTests(_RestoresRecorder, TestCase):
    @METER_ON
    def test_judge_spend_is_attributed_per_run(self):
        run = AgentRun.objects.create(
            team_id="t1",
            user_id="u1",
            query="q",
            status="done",
            final_answer_text="a",
        )

        def fake_judge(**kw):
            _record_fake_llm_call()
            return {"faithfulness": 1.0, "citation_precision": 1.0, "completeness": 1.0}

        with (
            patch(
                "origin.search_engine.management.commands.agent_judge_sample."
                "reconstruct_sources_for_run",
                return_value=[{"id": "s1"}],
            ),
            patch(
                "origin.search_engine.management.commands.agent_judge_sample.judge_answer",
                side_effect=fake_judge,
            ),
        ):
            call_command("agent_judge_sample", rate=1.0)

        event = AiSpendEvent.objects.get()
        self.assertEqual(event.surface, "judge")
        self.assertEqual(str(event.run_id), str(run.run_id))
        _assert_nothing_unattributed(self)
        self.assertIsNone(spend.current_context(), "the cron must unbind between runs")


# --------------------------------------------------------------------------- #
# Ingestion — the `index` surface                                             #
# --------------------------------------------------------------------------- #


class IngestionAttributionTests(_RestoresRecorder, TestCase):
    def _fake_ingest_entity(self, entity, stats, *, dry_run):
        spend.record_units(
            unit_kind=spend.UNIT_EMBED,
            units=1,
            provider="vertex",
            model="gemini-embedding-001",
            tokens=10,
        )

    @METER_ON
    def test_standalone_conversation_ingest_lands_on_the_index_surface(self):
        from origin.search_engine import ingestion

        with (
            patch.object(ingestion, "conversation_chunks_for_run", return_value=object()),
            patch.object(ingestion, "_ingest_entity", side_effect=self._fake_ingest_entity),
            override_settings(SEARCH_ENGINE=_se(AI_COST_METER=True, RAG_BULK_REFRESH=True)),
        ):
            ingestion.ingest_conversation_run(SimpleNamespace(run_id="r1"))

        event = AiSpendEvent.objects.get()
        self.assertEqual(event.surface, "index")
        self.assertEqual(event.purpose, "index")
        _assert_nothing_unattributed(self)

    @METER_ON
    def test_inside_an_ask_the_embed_joins_the_asks_request(self):
        from origin.search_engine import ingestion

        ask_id = str(uuid.uuid4())
        with (
            patch.object(ingestion, "conversation_chunks_for_run", return_value=object()),
            patch.object(ingestion, "_ingest_entity", side_effect=self._fake_ingest_entity),
            override_settings(SEARCH_ENGINE=_se(AI_COST_METER=True, RAG_BULK_REFRESH=True)),
            spend.spend_context(surface="ask", user_id="u1", request_id=ask_id),
        ):
            ingestion.ingest_conversation_run(SimpleNamespace(run_id="r1"))

        event = AiSpendEvent.objects.get()
        self.assertEqual(event.surface, "ask", "re-entrancy must keep the ask's attribution")
        self.assertEqual(str(event.request_id), ask_id)
        self.assertEqual(event.purpose, "index")

    @METER_ON
    def test_reindex_pass_lands_on_the_index_surface(self):
        from origin.search_engine import ingestion

        with (
            patch.object(ingestion, "iter_conversation_chunks", return_value=iter([object()])),
            patch.object(ingestion, "_ingest_entity", side_effect=self._fake_ingest_entity),
            override_settings(SEARCH_ENGINE=_se(AI_COST_METER=True, RAG_BULK_REFRESH=True)),
        ):
            ingestion.ingest_all(entity_types=["conversation"])

        self.assertEqual(AiSpendEvent.objects.get().surface, "index")
        _assert_nothing_unattributed(self)


# --------------------------------------------------------------------------- #
# The full ask flow — post-run embed + run linkage                            #
# --------------------------------------------------------------------------- #


class AskFlowAttributionTests(_RestoresRecorder, _StreamingAPIBase):
    URL = "/api/v2/agent/ask/"

    def _consume(self, resp):
        raw = b"".join(resp.streaming_content).decode("utf-8")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    @METER_ON
    def test_post_run_embed_joins_the_ask_and_the_rollup_carries_the_run(self):
        def fake_run_agent(query, ctx, emit, **kwargs):
            _record_fake_llm_call()  # the loop
            emit({"type": "answer_delta", "text": "hi"})
            emit({"type": "done"})
            # This runs on the real worker thread, whose DB connection
            # would otherwise outlive the test and block dropping
            # test_origin at teardown.
            from django.db import connections  # noqa: PLC0415

            connections.close_all()
            return None

        def fake_ingest(run):
            # The C1 conversation embed — the tripwire's first real
            # catch. It fires in the stream's `finally`, after the
            # worker's context exited.
            spend.record_units(
                unit_kind=spend.UNIT_EMBED,
                units=1,
                provider="vertex",
                model="gemini-embedding-001",
                tokens=10,
            )
            return True

        self.authenticate()
        with (
            patch("origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent),
            patch(
                "origin.search_engine.ingestion.ingest_conversation_run",
                side_effect=fake_ingest,
            ),
        ):
            resp = self.client.post(
                self.URL,
                {"query": "q", "team_id": str(self.team.pk)},
                format="json",
                HTTP_HOST="localhost",
            )
            self.assertEqual(resp.status_code, 200)
            self._consume(resp)

        run = AgentRun.objects.get()
        events = list(AiSpendEvent.objects.all())
        self.assertEqual(len(events), 2, "the loop call and the post-run embed")
        request_ids = {str(e.request_id) for e in events}
        self.assertEqual(len(request_ids), 1, "both must group under ONE logical request")
        for e in events:
            self.assertEqual(e.surface, "ask")

        row = AiRequestCost.objects.get()
        self.assertEqual(str(row.request_id), request_ids.pop())
        self.assertEqual(
            str(row.run_id),
            str(run.run_id),
            "the rollup must link to its run — the /decide/ resume leg finds "
            "the original request through exactly this column",
        )
        self.assertEqual(row.call_count, 2, "the rollup must include the post-run embed")
        _assert_nothing_unattributed(self)


# --------------------------------------------------------------------------- #
# /decide/ — the resume leg                                                   #
# --------------------------------------------------------------------------- #


class DecideResumeAttributionTests(_RestoresRecorder, _StreamingAPIBase):
    URL = "/api/v2/agent/decide/"

    def _make_paused_run(self):
        return AgentRun.objects.create(
            team_id=str(self.team.pk),
            user_id=str(self.user.id),
            query="q",
            status="awaiting_approval",
            pending_approval_token=uuid.uuid4(),
        )

    def _decide(self, run):
        self.authenticate()

        def fake_resume(run_, decision, ctx, emit, disabled_tools=None, cancel_event=None):
            _record_fake_llm_call()
            emit({"type": "answer_delta", "text": "resumed"})
            emit({"type": "done"})
            from django.db import connections  # noqa: PLC0415

            connections.close_all()  # worker-thread connection — see fake_run_agent
            return None

        with (
            patch("origin.search_engine.agent_views.resume_agent", side_effect=fake_resume),
            # The resumed leg reaches "done" and would run the REAL
            # post-run ingest (chunker + embed). Not this test's
            # subject — the ask-flow test covers that wiring.
            patch(
                "origin.search_engine.ingestion.ingest_conversation_run",
                return_value=False,
            ),
        ):
            resp = self.client.post(
                self.URL,
                {
                    "run_id": str(run.run_id),
                    "approval_token": str(run.pending_approval_token),
                    "decision": "approve",
                },
                format="json",
                HTTP_HOST="localhost",
            )
            self.assertEqual(resp.status_code, 200)
            b"".join(resp.streaming_content)

    @METER_ON
    def test_the_resumed_leg_rejoins_the_original_request(self):
        run = self._make_paused_run()
        original = AiRequestCost.objects.create(
            request_id=uuid.uuid4(),
            run_id=run.run_id,
            user_id=str(self.user.id),
            surface="ask",
            result=AiRequestCost.RESULT_SUCCESS,
            started_at=timezone.now(),
        )
        self._decide(run)

        event = AiSpendEvent.objects.get()
        self.assertEqual(
            str(event.request_id),
            str(original.request_id),
            "an approval round-trip is ONE logical request — a fresh id here "
            "would let a resumed ask double-count against the user",
        )
        original.refresh_from_db()
        self.assertEqual(original.call_count, 1, "the re-close must fold in the resumed leg")
        self.assertIsNotNone(original.finished_at)
        _assert_nothing_unattributed(self)

    @METER_ON
    def test_a_resume_with_no_prior_row_still_attributes(self):
        """Meter was off during the ask (or a pre-linkage run): the leg
        gets a fresh request rather than landing unattributed."""
        run = self._make_paused_run()
        self._decide(run)

        event = AiSpendEvent.objects.get()
        self.assertEqual(event.surface, "ask")
        row = AiRequestCost.objects.get()
        self.assertEqual(str(row.run_id), str(run.run_id))
        self.assertEqual(str(event.request_id), str(row.request_id))
        _assert_nothing_unattributed(self)
