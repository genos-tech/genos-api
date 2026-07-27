"""Run lifecycle — client disconnect, cooperative cancellation, reaping.

The leak this closes: a client disconnect raises `GeneratorExit` at the
`yield` in `_stream_ndjson`, and `GeneratorExit` is a `BaseException`,
so the old `except Exception` around the close block never saw it. The
run row stayed `running` with `finished_at` NULL forever, nothing
reaped it, and the daemon worker thread kept calling the model to
completion on a stream nobody was reading.

What was NOT broken, and is asserted here so a future change doesn't
"fix" it into a regression: `_charge_once` runs BEFORE the yield, so
quota was already charged by the time a disconnect is observable.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from origin.search_engine import agent_views as views
from origin.search_engine.agent_views import _stream_ndjson
from origin.search_engine.models import AgentRun


class _FakeRun:
    """Stands in for AgentRun so the stream tests need no DB."""

    def __init__(self):
        self.run_id = "fake-run"
        self.status = "running"
        self.final_answer_text = ""
        self.error_message = ""
        # Recent, so `_push_run_complete` treats the run as foreground
        # and no-ops instead of logging an ERROR about a missing field.
        self.started_at = timezone.now()
        self.finished_at = None
        self.pending_approval_token = None
        self.session_id = None
        self.user_id = "u1"
        self.team_id = "t1"
        self.query = "q"
        self.saved_fields = []

    def save(self, update_fields=None):
        self.saved_fields.append(list(update_fields or []))


class StreamCancellationTests(SimpleTestCase):
    """No DB: these exercise the generator's control flow, not persistence.

    `_close_run`'s success path fires two side effects that DO need a DB
    — the completion web-push and C1 conversation indexing. Both are
    already best-effort (their failures are caught and logged), so
    leaving them live would still pass, but every run would spew a
    `DatabaseOperationForbidden` traceback into the log and make a real
    failure harder to spot. Stubbed here rather than made conditional in
    production code.
    """

    def setUp(self):
        # `ingest_conversation_run` is imported lazily INSIDE `_close_run`
        # (the module is heavy), so it has to be patched at its source —
        # patching the `views` namespace would silently do nothing.
        for target in (
            "origin.search_engine.agent_views._push_run_complete",
            "origin.search_engine.ingestion.ingest_conversation_run",
        ):
            patcher = mock.patch(target, autospec=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_disconnect_marks_the_run_cancelled_and_stops_the_worker(self):
        started = threading.Event()
        cancel_seen = threading.Event()
        release = threading.Event()

        def worker_target(emit, cancel_event):
            emit({"type": "answer_delta", "text": "partial"})
            started.set()
            # Stand in for the agent loop's between-steps check.
            release.wait(timeout=5)
            if cancel_event.is_set():
                cancel_seen.set()
            return None

        run = _FakeRun()
        stream = _stream_ndjson(worker_target, run=run)

        # Consume one chunk, then abandon the generator — exactly what
        # Django does when the client goes away.
        first = next(stream)
        self.assertIn(b"partial", first)
        self.assertTrue(started.wait(timeout=5))
        stream.close()
        release.set()

        self.assertTrue(
            cancel_seen.wait(timeout=5),
            "the worker was never told to stop — it would run to AGENT_MAX_STEPS "
            "billing tokens to a stream nobody is reading",
        )
        self.assertEqual(run.status, "cancelled")
        self.assertIsNotNone(run.finished_at, "a cancelled run must not stay open forever")
        self.assertEqual(run.final_answer_text, "partial", "keep what the user already saw")

    def test_clean_finish_is_unaffected(self):
        def worker_target(emit, cancel_event):
            emit({"type": "answer_delta", "text": "hello"})
            emit({"type": "done"})
            return None

        run = _FakeRun()
        chunks = list(_stream_ndjson(worker_target, run=run))
        self.assertEqual(run.status, "done")
        self.assertEqual(run.final_answer_text, "hello")
        self.assertIsNotNone(run.finished_at)
        # The adapter stamps total stream wall time onto `done` (the
        # client's "answered in Xs"), alongside run_id/session_id.
        done = json.loads(chunks[-1])
        self.assertEqual(done["type"], "done")
        self.assertIsInstance(done["elapsed_ms"], int)
        self.assertGreaterEqual(done["elapsed_ms"], 0)

    def test_error_event_still_closes_as_error(self):
        def worker_target(emit, cancel_event):
            emit({"type": "error", "message": "boom"})
            return None

        run = _FakeRun()
        list(_stream_ndjson(worker_target, run=run))
        self.assertEqual(run.status, "error")
        self.assertEqual(run.error_message, "boom")

    def test_pause_still_beats_a_clean_close(self):
        def worker_target(emit, cancel_event):
            emit({"type": "answer_delta", "text": "thinking"})
            return {"paused": True, "approval_token": "tok"}

        run = _FakeRun()
        list(_stream_ndjson(worker_target, run=run))
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(run.pending_approval_token, "tok")
        self.assertIsNone(run.finished_at, "a paused run is not finished")

    def test_quota_is_charged_before_the_disconnect_is_observable(self):
        """Deliberately pinned. The charge fires before the `yield`, so a
        disconnect can never produce a free ask — and a future refactor
        that moved the charge after the yield would open exactly that
        hole without failing any other test."""
        charged: list[tuple[str, str]] = []

        def worker_target(emit, cancel_event):
            emit({"type": "answer_delta", "text": "x"})
            return None

        stream = _stream_ndjson(
            worker_target,
            run=_FakeRun(),
            user_id_for_quota="u1",
            quota_keys=["__llm_ask__"],
        )
        with mock.patch.object(
            views, "increment_usage", side_effect=lambda uid, key: charged.append((uid, key))
        ):
            next(stream)
            stream.close()

        self.assertEqual(charged, [("u1", "__llm_ask__")])


class ReapStaleRunsTests(TestCase):
    def _run(self, *, status="running", age_hours=0):
        run = AgentRun.objects.create(team_id="t", user_id="u", query="q", status=status)
        AgentRun.objects.filter(pk=run.pk).update(
            started_at=timezone.now() - timedelta(hours=age_hours)
        )
        run.refresh_from_db()
        return run

    def test_closes_a_run_stuck_running_past_the_cutoff(self):
        stale = self._run(age_hours=5)
        call_command("reap_stale_agent_runs", "--hours", "2", stdout=StringIO())
        stale.refresh_from_db()
        self.assertEqual(stale.status, "error")
        self.assertIsNotNone(stale.finished_at)
        self.assertIn("never reached a terminal state", stale.error_message)

    def test_leaves_a_recent_run_alone(self):
        """The cost of reaping a LIVE run — a user watching their answer
        get marked failed underneath them — is far worse than a stale row
        living another hour."""
        fresh = self._run(age_hours=1)
        call_command("reap_stale_agent_runs", "--hours", "2", stdout=StringIO())
        fresh.refresh_from_db()
        self.assertEqual(fresh.status, "running")

    def test_never_touches_an_already_closed_run(self):
        done = self._run(status="done", age_hours=99)
        call_command("reap_stale_agent_runs", "--hours", "2", stdout=StringIO())
        done.refresh_from_db()
        self.assertEqual(done.status, "done")

    def test_does_not_reap_a_cancelled_run(self):
        # Cancelled is already terminal — the live handler proved it.
        cancelled = self._run(status="cancelled", age_hours=99)
        call_command("reap_stale_agent_runs", "--hours", "2", stdout=StringIO())
        cancelled.refresh_from_db()
        self.assertEqual(cancelled.status, "cancelled")

    def test_dry_run_writes_nothing(self):
        stale = self._run(age_hours=5)
        out = StringIO()
        call_command("reap_stale_agent_runs", "--hours", "2", "--dry-run", stdout=out)
        stale.refresh_from_db()
        self.assertEqual(stale.status, "running")
        self.assertIn("dry-run", out.getvalue())

    def test_is_idempotent(self):
        self._run(age_hours=5)
        call_command("reap_stale_agent_runs", "--hours", "2", stdout=StringIO())
        out = StringIO()
        call_command("reap_stale_agent_runs", "--hours", "2", stdout=out)
        self.assertIn("No runs stuck", out.getvalue())
