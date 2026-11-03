"""MIP-976: `POST /api/v2/agent/decide/` consumes the approval token atomically.

The endpoint used to read the run, check `status == "awaiting_approval"` and
the token, and only THEN clear the token with an unconditional
`run.save(update_fields=[...])`. Two requests for one pending approval (two
tabs, a client retry) that both read the row before either wrote it both
passed the checks, both "consumed" the token, and both ran the approved write
tool. The "concurrent decides get a 409" guarantee only held for requests that
arrived one after the other.

Why these tests are shaped the way they are:

  * The interleaving is forced, not hoped for. `agent_views.AgentRun` is
    swapped for a proxy whose `objects.get` lets the OUTER request read the
    row and then runs `between()` (a whole second request, or a direct row
    edit) before handing the now-STALE row back. Whatever the view does next
    it does with a snapshot that says "awaiting approval, token T" while the
    database may say otherwise. That is exactly what a request that lost a
    race holds, and it works the same before and after the fix.
  * Two executions need both resumed workers to get past
    `_rebuild_messages` before either saves the step result (that, not the
    view, is the controller's own single-shot guard), so the fake write tool
    parks inside `run()` until the test lets it go.
  * `TransactionTestCase`, as in `test_ai_attribution`: the streamed resume
    runs on a real worker thread whose writes must really commit.
"""

from __future__ import annotations

import contextlib
import threading
import time
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connections
from django.db.models.query import QuerySet
from django.test import TransactionTestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine import agent_views
from origin.search_engine.agent import controller
from origin.search_engine.agent.controller import PENDING_APPROVAL_MARKER
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.models import AgentRun, AgentStep

URL = "/api/v2/agent/decide/"


class _WriteTool:
    """Stands in for a `requires_approval` tool and counts how often it ran.

    While `release` is clear, `run()` parks, so two resumed workers can be
    inside it at once (the window in which the controller's own guard, the
    pending-step marker, has not been spent yet)."""

    name = "create_task"
    requires_approval = True

    def __init__(self):
        self.calls = 0
        self._lock = threading.Lock()
        self.release = threading.Event()
        self.release.set()

    def run(self, args, ctx):
        with self._lock:
            self.calls += 1
        if not self.release.wait(timeout=15):  # a hung test must not hang the suite
            raise AssertionError("write tool was never released")
        return {"__summary__": "created"}

    def wait_for_calls(self, n, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.calls >= n:
                return True
            time.sleep(0.01)
        return self.calls >= n


class _HookedManager:
    """`AgentRun.objects`, except that the first `times` `.get()` calls (the
    view's read of the run) call `after_read` once the real row is fetched."""

    def __init__(self, after_read, times=1):
        self._after_read = after_read
        self._left = times
        self._lock = threading.Lock()

    def get(self, *args, **kwargs):
        row = AgentRun.objects.get(*args, **kwargs)
        with self._lock:
            fire = self._left > 0
            self._left -= 1
        if fire:
            self._after_read()
        return row

    def __getattr__(self, name):
        return getattr(AgentRun.objects, name)


class _HookedAgentRun:
    """Drop-in for the `AgentRun` name inside `agent_views`."""

    def __init__(self, after_read, times=1):
        self.objects = _HookedManager(after_read, times)

    def __getattr__(self, name):
        return getattr(AgentRun, name)


def _begin(resp):
    """Start a streamed 200: the worker thread only starts on first iteration."""
    chunks = iter(resp.streaming_content)
    next(chunks)
    return chunks


class DecideRaceBase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="decider", email="decider@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="T", team_email="t@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        self.client = self._client(self.user)
        self.tool = _WriteTool()
        # (decision, run.status, run.pending_approval_token) as `resume_agent`
        # received them: what the rest of the view hands the resumed leg.
        self.resumed_with = []

        real_resume = controller.resume_agent

        def resume_and_close(run, decision, *args, **kwargs):
            self.resumed_with.append((decision, run.status, run.pending_approval_token))
            try:
                return real_resume(run, decision, *args, **kwargs)
            finally:
                # This is the stream's worker thread: leave no connection behind.
                connections.close_all()

        def fake_drive_loop(*, emit, **kwargs):
            emit({"type": "answer_delta", "text": "all done"})
            emit({"type": "done"})
            return None

        for patcher in (
            mock.patch.dict(REGISTRY, {"create_task": self.tool}),
            mock.patch.object(agent_views, "resume_agent", side_effect=resume_and_close),
            mock.patch.object(controller, "_drive_loop", side_effect=fake_drive_loop),
            # The test user's tier must not veto the pending tool: a vetoed
            # tool would never run and every "ran once" check would be vacuous.
            mock.patch.object(agent_views, "disabled_tools_for_user", return_value=set()),
            mock.patch.object(agent_views, "_persisted_disabled_tools", return_value=set()),
            mock.patch.object(controller, "_operator_disabled_tools", return_value=set()),
            # A run that reaches "done" would run the real post-run ingest.
            mock.patch(
                "origin.search_engine.ingestion.ingest_conversation_run", return_value=False
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- fixtures ---------------------------------------------------------

    @staticmethod
    def _client(user):
        client = APIClient()
        access = RefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        return client

    def _paused_run(self, **overrides):
        token = uuid.uuid4()
        fields = dict(
            team_id=str(self.team.pk),
            user_id=str(self.user.id),
            query="make me a task",
            status="awaiting_approval",
            pending_approval_token=token,
        )
        fields.update(overrides)
        run = AgentRun.objects.create(**fields)
        step = AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name="create_task",
            arguments_json={"title": "x"},
            summary=PENDING_APPROVAL_MARKER,
        )
        return run, token, step

    # -- requests ---------------------------------------------------------

    def _post(self, run_id, token, decision, client=None):
        return (client or self.client).post(
            URL,
            {"run_id": str(run_id), "approval_token": str(token), "decision": decision},
            format="json",
        )

    def _post_after_stale_read(self, run, token, decision, between):
        """POST a decision whose read of the run goes stale: `between()` runs
        after the view has fetched the row and before it does anything else."""
        with mock.patch.object(agent_views, "AgentRun", _HookedAgentRun(between)):
            return self._post(run.run_id, token, decision)

    def _concurrent_decide(self, run, token, decision):
        """`between()` that is a complete second decide request, finished
        before the outer one resumes. The response lands in the returned box."""
        box = {}

        def request():
            try:
                box["resp"] = self._post(run.run_id, token, decision, self._client(self.user))
            finally:
                connections.close_all()

        def between():
            thread = threading.Thread(target=request)
            thread.start()
            thread.join(timeout=30)
            assert not thread.is_alive(), "the concurrent request never finished"

        return box, between

    def _run_streams(self, *responses_and_decisions):
        """Start every 200 stream, in order, then let the write tool go and
        drain them. After each approve, wait until its worker is inside the
        tool, so the overlap is real and not a matter of thread timing."""
        started = []
        approvals = 0
        for resp, decision in responses_and_decisions:
            if resp.status_code != 200:
                continue
            started.append(_begin(resp))
            if decision == "approve":
                approvals += 1
                self.assertTrue(
                    self.tool.wait_for_calls(approvals),
                    f"expected {approvals} worker(s) inside the write tool, saw {self.tool.calls}",
                )
        self.tool.release.set()
        for chunks in started:
            for _ in chunks:
                pass
        return len(started)

    def _row(self, run):
        return AgentRun.objects.get(run_id=run.run_id)


class DecideUnchangedPathTests(DecideRaceBase):
    """Every status the endpoint answered before the fix, still answered the
    same way, and the happy paths still do what they did."""

    def test_unknown_run_is_404(self):
        resp = self._post(uuid.uuid4(), uuid.uuid4(), "approve")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json(), {"error": "run not found."})

    def test_a_run_that_belongs_to_someone_else_is_403_and_stays_pending(self):
        run, token, _ = self._paused_run(user_id="999999")
        resp = self._post(run.run_id, token, "approve")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"error": "Not authorized to resume this run."})
        row = self._row(run)
        self.assertEqual((row.status, row.pending_approval_token), ("awaiting_approval", token))
        self.assertEqual(self.tool.calls, 0)

    def test_a_run_that_is_not_awaiting_approval_is_409(self):
        run, token, _ = self._paused_run(status="done")
        resp = self._post(run.run_id, token, "approve")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json(), {"error": "run is not awaiting approval (status=done)."})
        self.assertEqual(self.tool.calls, 0)

    def test_the_status_is_checked_before_the_token(self):
        run, _, _ = self._paused_run(status="done")
        resp = self._post(run.run_id, uuid.uuid4(), "approve")
        self.assertEqual(resp.status_code, 409)

    def test_the_owner_is_checked_before_the_status(self):
        run, token, _ = self._paused_run(user_id="999999", status="done")
        self.assertEqual(self._post(run.run_id, token, "approve").status_code, 403)

    def test_a_wrong_token_is_403_and_the_run_stays_pending(self):
        run, token, _ = self._paused_run()
        resp = self._post(run.run_id, uuid.uuid4(), "approve")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"error": "approval_token does not match."})
        row = self._row(run)
        self.assertEqual((row.status, row.pending_approval_token), ("awaiting_approval", token))
        self.assertEqual(self.tool.calls, 0)

    def test_malformed_requests_are_400(self):
        run, token, _ = self._paused_run()
        for body in (
            {"approval_token": str(token), "decision": "approve"},
            {"run_id": str(run.run_id), "decision": "approve"},
            {"run_id": str(run.run_id), "approval_token": str(token), "decision": "maybe"},
        ):
            self.assertEqual(self.client.post(URL, body, format="json").status_code, 400, body)
        self.assertEqual(self._row(run).status, "awaiting_approval")

    def test_approve_consumes_the_token_and_runs_the_tool_once(self):
        run, token, step = self._paused_run()
        resp = self._post(run.run_id, token, "approve")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._run_streams((resp, "approve")), 1)

        self.assertEqual(self.tool.calls, 1)
        # The resumed leg is handed a run that already says "running", no token:
        # the row the rest of the view (and the stream's close) goes on to use.
        self.assertEqual(self.resumed_with, [("approve", "running", None)])
        row = self._row(run)
        self.assertEqual(row.status, "done")
        self.assertIsNone(row.pending_approval_token)
        self.assertIsNotNone(row.finished_at)
        self.assertEqual(row.final_answer_text, "all done")
        step.refresh_from_db()
        self.assertEqual(step.summary, "created")
        self.assertFalse(step.error)

    def test_reject_consumes_the_token_and_does_not_run_the_tool(self):
        run, token, step = self._paused_run()
        resp = self._post(run.run_id, token, "reject")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._run_streams((resp, "reject")), 1)

        self.assertEqual(self.tool.calls, 0)
        self.assertEqual(self.resumed_with, [("reject", "running", None)])
        row = self._row(run)
        self.assertEqual(row.status, "done")
        self.assertIsNone(row.pending_approval_token)
        step.refresh_from_db()
        self.assertEqual(step.error, "user_rejected")

    def test_a_replay_after_the_resume_finished_is_409(self):
        run, token, _ = self._paused_run()
        resp = self._post(run.run_id, token, "approve")
        self._run_streams((resp, "approve"))
        again = self._post(run.run_id, token, "approve")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json(), {"error": "run is not awaiting approval (status=done)."})
        self.assertEqual(self.tool.calls, 1)

    def test_a_replay_while_the_resume_is_running_is_409(self):
        run, token, _ = self._paused_run()
        first = self._post(run.run_id, token, "approve")
        again = self._post(run.run_id, token, "approve")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json(), {"error": "run is not awaiting approval (status=running)."})
        self._run_streams((first, "approve"))
        self.assertEqual(self.tool.calls, 1)


class DecideRaceTests(DecideRaceBase):
    """Two requests for one pending approval."""

    def test_two_approves_run_the_write_tool_once(self):
        run, token, step = self._paused_run()
        self.tool.release.clear()

        box, between = self._concurrent_decide(run, token, "approve")
        outer = self._post_after_stale_read(run, token, "approve", between)
        inner = box["resp"]

        started = self._run_streams((inner, "approve"), (outer, "approve"))

        self.assertEqual(self.tool.calls, 1, "the approved write ran more than once")
        # Whichever request lost is refused; the other one resumed the run.
        self.assertEqual(inner.status_code, 200, "the request that consumed the token resumes")
        self.assertEqual(
            outer.status_code,
            409,
            "the request that read the run before the token was consumed must be refused",
        )
        self.assertEqual(outer.json(), {"error": "run is not awaiting approval (status=running)."})
        self.assertEqual(started, 1)
        self.assertEqual(len(self.resumed_with), 1, "the loser must not reach resume_agent")
        row = self._row(run)
        self.assertEqual(row.status, "done")
        self.assertIsNone(row.pending_approval_token)
        step.refresh_from_db()
        self.assertEqual(step.summary, "created")
        self.assertFalse(step.error)

    def test_two_requests_that_really_overlap_run_the_write_tool_once(self):
        """No scripted loser here: both requests read the run, wait for each
        other at a barrier, and then race to consume it."""
        run, token, step = self._paused_run()
        self.tool.release.clear()
        barrier = threading.Barrier(2, timeout=20)
        responses = []

        def request():
            try:
                responses.append(self._post(run.run_id, token, "approve", self._client(self.user)))
            finally:
                connections.close_all()

        with mock.patch.object(agent_views, "AgentRun", _HookedAgentRun(barrier.wait, times=2)):
            threads = [threading.Thread(target=request) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(len(responses), 2)

        self._run_streams(*[(resp, "approve") for resp in responses])

        self.assertEqual(self.tool.calls, 1, "the approved write ran more than once")
        self.assertEqual(sorted(resp.status_code for resp in responses), [200, 409])
        self.assertEqual(len(self.resumed_with), 1)
        self.assertEqual(self._row(run).status, "done")
        step.refresh_from_db()
        self.assertEqual(step.summary, "created")

    def test_a_late_approve_after_a_reject_does_not_run_the_tool(self):
        run, token, step = self._paused_run()
        self.tool.release.clear()

        box, between = self._concurrent_decide(run, token, "reject")
        outer = self._post_after_stale_read(run, token, "approve", between)
        inner = box["resp"]

        # The stale approve goes first: were it to resume, its worker would be
        # inside the tool before the reject (which does not park) lands.
        self._run_streams((outer, "approve"), (inner, "reject"))

        self.assertEqual(self.tool.calls, 0, "the user rejected this action")
        self.assertEqual(inner.status_code, 200)
        self.assertEqual(outer.status_code, 409)
        step.refresh_from_db()
        self.assertEqual(step.error, "user_rejected")
        self.assertEqual(len(self.resumed_with), 1)

    def test_a_late_reject_after_an_approve_does_not_undo_it(self):
        run, token, step = self._paused_run()
        self.tool.release.clear()

        box, between = self._concurrent_decide(run, token, "approve")
        outer = self._post_after_stale_read(run, token, "reject", between)
        inner = box["resp"]

        self._run_streams((inner, "approve"), (outer, "reject"))

        self.assertEqual(self.tool.calls, 1)
        step.refresh_from_db()
        self.assertEqual(step.summary, "created")
        self.assertFalse(step.error, "the write ran, so the step must not also say it was rejected")
        self.assertEqual(inner.status_code, 200)
        self.assertEqual(outer.status_code, 409)
        self.assertEqual(len(self.resumed_with), 1)

    def test_a_stale_token_does_not_consume_a_newer_pause(self):
        """The winner's resume paused again on a second write tool (a new
        token, status back to awaiting_approval) before the loser's consume."""
        run, token, _ = self._paused_run()
        newer = uuid.uuid4()

        def between():
            AgentRun.objects.filter(run_id=run.run_id).update(
                status="awaiting_approval", pending_approval_token=newer
            )

        resp = self._post_after_stale_read(run, token, "approve", between)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json(), {"error": "approval_token does not match."})
        row = self._row(run)
        self.assertEqual((row.status, row.pending_approval_token), ("awaiting_approval", newer))
        self.assertEqual((self.tool.calls, self.resumed_with), (0, []))

    def test_a_run_that_stopped_awaiting_approval_is_not_resumed(self):
        """Synthetic: the token is still T but the status moved on. (In real
        flows a non-null token implies awaiting_approval; this pins that the
        consume checks the status itself and does not lean on that.)"""
        run, token, _ = self._paused_run()

        def between():
            AgentRun.objects.filter(run_id=run.run_id).update(status="cancelled")

        resp = self._post_after_stale_read(run, token, "approve", between)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json(), {"error": "run is not awaiting approval (status=cancelled)."})
        self.assertEqual(self._row(run).status, "cancelled")
        self.assertEqual((self.tool.calls, self.resumed_with), (0, []))

    def test_a_run_deleted_mid_request_is_404_and_nothing_resumes(self):
        """A conversation delete (which removes a paused run) landed between
        the read and the consume. A later decide would be a 404; so is this."""
        run, token, _ = self._paused_run()

        def between():
            AgentRun.objects.filter(run_id=run.run_id).delete()

        resp = self._post_after_stale_read(run, token, "approve", between)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json(), {"error": "run not found."})
        self.assertEqual((self.tool.calls, self.resumed_with), (0, []))


class DecideConsumeFailureTests(DecideRaceBase):
    """If the token cannot be consumed, nothing is resumed."""

    @contextlib.contextmanager
    def _consume_broken(self):
        """Fail every write to an AgentRun, whichever way the view makes it:
        a model `.save()` or a queryset `.update()`."""
        real_update = QuerySet.update

        def update(queryset, **kwargs):
            if queryset.model is AgentRun:
                raise DatabaseError("connection lost")
            return real_update(queryset, **kwargs)

        with (
            mock.patch.object(AgentRun, "save", side_effect=DatabaseError("connection lost")),
            mock.patch.object(QuerySet, "update", update),
        ):
            yield

    def test_a_failed_consume_answers_503_and_runs_nothing(self):
        run, token, step = self._paused_run()

        with self._consume_broken():
            resp = self._post(run.run_id, token, "approve")

        self.assertEqual(resp.status_code, 503)
        self.assertIn("error", resp.json())
        self.assertIn("reference", resp.json(), "the failure must be findable in the logs")
        self.assertEqual(self.tool.calls, 0, "the write ran although the token was never consumed")
        self.assertEqual(self.resumed_with, [])
        row = self._row(run)
        self.assertEqual((row.status, row.pending_approval_token), ("awaiting_approval", token))
        step.refresh_from_db()
        self.assertEqual(step.summary, PENDING_APPROVAL_MARKER)

    def test_the_run_is_left_retryable_after_a_failed_consume(self):
        run, token, _ = self._paused_run()
        with self._consume_broken():
            self.assertEqual(self._post(run.run_id, token, "approve").status_code, 503)

        resp = self._post(run.run_id, token, "approve")
        self.assertEqual(resp.status_code, 200)
        self._run_streams((resp, "approve"))
        self.assertEqual(self.tool.calls, 1)
