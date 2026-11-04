"""MIP-986: an id in the request that is not an id is the SAME answer as an id that is not there.

`AgentRun.run_id` and `AgentSession.session_id` are UUID primary keys. Handing the ORM a
string that is not a UUID raises `django.core.exceptions.ValidationError` out of the lookup
(not `DoesNotExist`, and not `ValueError`: Django's `UUIDField.to_python` converts the
`ValueError` of `uuid.UUID(...)` into a `ValidationError`). A view that guards only
`DoesNotExist` answers such an id with a 500 where an unknown-but-valid one gets a 404.

`/agent/decide/` did exactly that, and `GET /agent/sessions/<id>/` guarded `ValueError` on
the comment "ValueError covers malformed UUIDs", which it does not. The feedback, undo,
pin, session-delete and shared-answer-withdraw endpoints already catch all three; the first
two are pinned here too (the others have their own tests), so the audit's answer cannot
drift.

Each probe compares STATUS AND BODY with the answer for an unknown but well-formed id: a
different body would tell a caller which ids are well-formed, and a guessed id confirms
nothing.

`/agent/decide/` also read its three text fields with `(data.get(k) or "").strip()`, which
is an `AttributeError` (a 500) for a truthy value that is not text. Those are a 400, like
every other malformed field of that body. Values that are empty-ish (`0`, `false`, `[]`,
`{}`, null) were already "is required" and still are.
"""

from __future__ import annotations

import uuid

from rest_framework.test import APIClient

from origin.search_engine.agent_views import _get_or_create_session
from origin.search_engine.models import AgentRun, AgentSession

from .test_agent_decide_race import URL as DECIDE_URL
from .test_agent_decide_race import DecideRaceBase
from .test_base import BaseAPITestCase

# Not UUIDs, each in a different way. None of them may reach the database as a lookup key.
NOT_UUIDS = (
    "not-a-uuid",
    "123",
    "6f0c5e0e-0000-4000-8000-00000000000",  # one digit short
    "6f0c5e0e-0000-4000-8000-0000000000000",  # one digit long
    "{not-a-uuid}",
    "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz",
    "../runs",
    "a" * 5000,
    "run\x00id",
)


def _unknown():
    return str(uuid.uuid4())


class DecideMalformedRunIdTests(DecideRaceBase):
    def setUp(self):
        super().setUp()
        # The real 500 response, not the exception re-raised into the test.
        self.client.raise_request_exception = False

    def _body(self, **overrides):
        body = {
            "run_id": _unknown(),
            "approval_token": str(uuid.uuid4()),
            "decision": "approve",
        }
        body.update(overrides)
        return body

    def _decide(self, **overrides):
        return self.client.post(DECIDE_URL, self._body(**overrides), format="json")

    def test_a_run_id_that_is_not_a_uuid_is_the_unknown_runs_404(self):
        run, token, _ = self._paused_run()
        unknown = self._decide()
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json(), {"error": "run not found."})

        for bad in NOT_UUIDS:
            resp = self._decide(run_id=bad)
            self.assertEqual((resp.status_code, resp.json()), (404, unknown.json()), repr(bad)[:40])

        # The pending run was not touched, and nothing ran.
        row = self._row(run)
        self.assertEqual((row.status, row.pending_approval_token), ("awaiting_approval", token))
        self.assertEqual(self.tool.calls, 0)

    def test_a_padded_run_id_is_trimmed_as_before(self):
        run, token, _ = self._paused_run()
        resp = self._decide(run_id=f"  {run.run_id}\n", approval_token=str(token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._run_streams((resp, "approve")), 1)
        self.assertEqual(self.tool.calls, 1)
        self.assertEqual(self._row(run).status, "done")

    def test_a_valid_run_still_answers_what_it_did(self):
        run, token, _ = self._paused_run()
        wrong_token = self._decide(run_id=str(run.run_id))
        self.assertEqual(wrong_token.status_code, 403)
        self.assertEqual(wrong_token.json(), {"error": "approval_token does not match."})

        theirs, their_token, _ = self._paused_run(user_id="999999")
        foreign = self._decide(run_id=str(theirs.run_id), approval_token=str(their_token))
        self.assertEqual(foreign.status_code, 403)
        self.assertEqual(foreign.json(), {"error": "Not authorized to resume this run."})

        finished, finished_token, _ = self._paused_run(status="done")
        replay = self._decide(run_id=str(finished.run_id), approval_token=str(finished_token))
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json(), {"error": "run is not awaiting approval (status=done)."})

        self.assertEqual(self._row(run).status, "awaiting_approval")
        self.assertEqual(self.tool.calls, 0)

    def test_a_run_id_that_is_not_text_is_a_400_not_a_500(self):
        run, token, _ = self._paused_run()
        for bad in (123, 1.5, True, [str(run.run_id)], {"run_id": str(run.run_id)}):
            resp = self._decide(run_id=bad)
            self.assertEqual(resp.status_code, 400, repr(bad))
            self.assertEqual(resp.json(), {"error": "run_id must be a string."}, repr(bad))
        self.assertEqual(self._row(run).status, "awaiting_approval")

    def test_an_empty_run_id_is_still_required(self):
        # Every one of these was a 400 "is required" before, and none was a crash.
        for empty in (None, "", "   ", 0, False, [], {}):
            resp = self._decide(run_id=empty)
            self.assertEqual(resp.status_code, 400, repr(empty))
            self.assertEqual(resp.json(), {"error": "run_id is required."}, repr(empty))
        body = self._body()
        del body["run_id"]
        resp = self.client.post(DECIDE_URL, body, format="json")
        self.assertEqual((resp.status_code, resp.json()), (400, {"error": "run_id is required."}))

    def test_the_other_text_fields_that_are_not_text_are_a_400_not_a_500(self):
        run, token, _ = self._paused_run()
        for field, bad in (
            ("approval_token", 123),
            ("approval_token", [str(token)]),
            ("approval_token", {"a": 1}),
            ("decision", 1),
            ("decision", ["approve"]),
            ("decision", True),
        ):
            resp = self._decide(run_id=str(run.run_id), **{field: bad})
            self.assertEqual(resp.status_code, 400, (field, bad))
            self.assertEqual(resp.json(), {"error": f"{field} must be a string."}, (field, bad))
        self.assertEqual(self._row(run).status, "awaiting_approval")
        self.assertEqual(self.tool.calls, 0)


class _AsAnAgentUser(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # A real 500 response if there is one, not the exception re-raised into the test.
        self.client = APIClient(raise_request_exception=False)
        self.authenticate()

    def make_run(self, **fields):
        return AgentRun.objects.create(
            **{
                "team_id": str(self.team.pk),
                "user_id": str(self.user.id),
                "query": "q",
                "status": "done",
                **fields,
            }
        )


class FeedbackMalformedRunIdTests(_AsAnAgentUser):
    """Already right; pinned so it stays the same answer as the others."""

    @staticmethod
    def url(run_id):
        return f"/api/v2/agent/runs/{run_id}/feedback/"

    def test_a_run_id_that_is_not_a_uuid_is_the_unknown_runs_404(self):
        unknown = self.client.post(self.url(_unknown()), {"rating": 1}, format="json")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json(), {"error": "No such agent run."})
        for bad in ("not-a-uuid", "123", "{not-a-uuid}", "a" * 5000):
            resp = self.client.post(self.url(bad), {"rating": 1}, format="json")
            self.assertEqual((resp.status_code, resp.json()), (404, unknown.json()), bad[:40])

    def test_a_valid_run_still_takes_its_feedback(self):
        run = self.make_run()
        resp = self.client.post(
            self.url(run.run_id), {"rating": -1, "comment": "no"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"run_id": str(run.run_id), "rating": -1, "comment": "no"})


class SessionDetailMalformedIdTests(_AsAnAgentUser):
    def get(self, session_id):
        return self.client.get(
            f"/api/v2/agent/sessions/{session_id}/", {"team_id": str(self.team.pk)}
        )

    def test_a_session_id_that_is_not_a_uuid_is_the_unknown_sessions_404(self):
        unknown = self.get(_unknown())
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json(), {"error": "Session not found."})
        for bad in ("not-a-uuid", "123", "{not-a-uuid}", "a" * 5000):
            resp = self.get(bad)
            self.assertEqual((resp.status_code, resp.json()), (404, unknown.json()), bad[:40])

    def test_a_valid_session_is_still_read(self):
        session = AgentSession.objects.create(team_id=str(self.team.pk), user_id=str(self.user.id))
        self.make_run(session=session, final_answer_text="the answer")
        resp = self.get(session.session_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["session_id"], str(session.session_id))
        self.assertEqual([t["answer"] for t in resp.json()["turns"]], ["the answer"])

    def test_another_users_session_is_the_same_404(self):
        theirs = AgentSession.objects.create(team_id=str(self.team.pk), user_id=str(self.user2.id))
        self.assertEqual(self.get(theirs.session_id).json(), {"error": "Session not found."})


class SessionResolutionMalformedIdTests(BaseAPITestCase):
    """`/ask/` resolves the `session_id` it is sent through `_get_or_create_session`, and
    wraps the call in a broad `except` that carries on WITHOUT a session. An id that is not
    a UUID used to escape the helper's own catch (a ValidationError, not a ValueError) and
    land there: no session, no memory, an exception logged. It is a fresh session, as for an
    id that is not there."""

    def resolve(self, session_id, **kwargs):
        return _get_or_create_session(session_id, str(self.team.pk), str(self.user.id), **kwargs)

    def test_a_session_id_that_is_not_a_uuid_gets_a_fresh_session_like_an_unknown_one(self):
        for bad in (_unknown(), *NOT_UUIDS):
            before = AgentSession.objects.count()
            session = self.resolve(bad)
            self.assertEqual(AgentSession.objects.count(), before + 1, repr(bad)[:40])
            self.assertEqual(
                (session.team_id, session.user_id), (str(self.team.pk), str(self.user.id))
            )
            self.assertNotEqual(str(session.session_id), bad)

    def test_a_live_session_is_still_resumed(self):
        live = AgentSession.objects.create(team_id=str(self.team.pk), user_id=str(self.user.id))
        before = AgentSession.objects.count()
        self.assertEqual(self.resolve(str(live.session_id)).session_id, live.session_id)
        self.assertEqual(AgentSession.objects.count(), before)
