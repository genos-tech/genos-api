"""MIP-992: a body that is not a JSON object is a 400 on every agent endpoint, not a 500.

`/agent/decide/`, `/agent/runs/<id>/feedback/`, `/agent/thread-summary/`,
`/agent/note-summary/`, `/agent/ask/` and `/search/` each read their body with
`data = request.data or {}` and then `data.get(...)`. `or {}` only covered the empty
bodies (null, `[]`, `0`, `""`, `false`). A body that parses as JSON but is not an object
(`[1]`, `"abc"`, `5`, `true`) went straight to `.get` and answered 500.

MIP-992 read such a body as an empty one (`body_object`), so it got the endpoint's own
"is required" 400. Since MIP-1013 the base view refuses it before the endpoint runs, with
400 "Request body must be a JSON object." (`object_body`), the same answer on every
route. The empty bodies still reach the endpoint as `{}` and keep exactly the answer
they always had, and `{}`'s answer is pinned so a refusal cannot pass for it.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.core.cache import cache
from rest_framework.test import APIClient

from origin.search_engine.models import AgentRun

from .test_base import BaseAPITestCase

# JSON text, posted as it is. None of these is an object.
NOT_OBJECTS = ("[1]", '"abc"', "5", "true", "1.5", '[{"query": "q"}]')
# Empty-ish: they read as "no body" and were already the missing-field answer.
EMPTY_ISH = ("null", "[]", "0", '""', "false", "")

NOT_AN_OBJECT = {"error": "Request body must be a JSON object."}

ASK = "/api/v2/agent/ask/"


def _unknown():
    return str(uuid.uuid4())


class _AgentClient(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # The agent throttles count in the cache, which persists across tests in LocMem.
        cache.clear()
        self.addCleanup(cache.clear)
        # The real 500 response if there is one, not the exception re-raised into the test.
        self.client = APIClient(raise_request_exception=False)
        self.authenticate()

    def post_raw(self, url, raw):
        return self.client.post(url, raw, content_type="application/json")


class _BodyShapeTests:
    """One endpoint's answers to a body that is not an object."""

    URL: str
    MISSING_FIELD: str  # the endpoint's answer to `{}`, in its own words

    def test_a_body_that_is_not_an_object_is_refused(self):
        empty = self.post_raw(self.URL, "{}")
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json(), {"error": self.MISSING_FIELD})

        for raw in NOT_OBJECTS:
            resp = self.post_raw(self.URL, raw)
            self.assertEqual((resp.status_code, resp.json()), (400, NOT_AN_OBJECT), raw)

    def test_an_empty_body_keeps_the_answer_it_always_had(self):
        for raw in EMPTY_ISH:
            resp = self.post_raw(self.URL, raw)
            self.assertEqual(
                (resp.status_code, resp.json()), (400, {"error": self.MISSING_FIELD}), repr(raw)
            )


class AskBodyShapeTests(_BodyShapeTests, _AgentClient):
    URL = ASK
    MISSING_FIELD = "query is required and must be non-empty."


class DecideBodyShapeTests(_BodyShapeTests, _AgentClient):
    URL = "/api/v2/agent/decide/"
    MISSING_FIELD = "run_id is required."

    def test_a_valid_body_still_answers_what_it_did(self):
        body = {"run_id": _unknown(), "approval_token": _unknown(), "decision": "approve"}
        resp = self.client.post(self.URL, body, format="json")
        self.assertEqual((resp.status_code, resp.json()), (404, {"error": "run not found."}))


class FeedbackBodyShapeTests(_BodyShapeTests, _AgentClient):
    # The body is read before the run is looked up, so any id will do for a refusal.
    URL = f"/api/v2/agent/runs/{uuid.uuid4()}/feedback/"
    MISSING_FIELD = "rating is required and must be an integer in {-1, 0, 1}."

    def _run(self):
        return AgentRun.objects.create(
            team_id=str(self.team.pk), user_id=str(self.user.id), query="q", status="done"
        )

    def test_a_valid_body_still_answers_what_it_did(self):
        run = self._run()
        url = f"/api/v2/agent/runs/{run.run_id}/feedback/"
        resp = self.client.post(url, {"rating": -1, "comment": "no"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"run_id": str(run.run_id), "rating": -1, "comment": "no"})

    def test_a_form_post_is_still_read(self):
        # A form body is a QueryDict, which is a dict: it must not read as empty.
        run = self._run()
        url = f"/api/v2/agent/runs/{run.run_id}/feedback/"
        resp = self.client.post(url, {"rating": "1", "comment": "yes"}, format="multipart")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"run_id": str(run.run_id), "rating": 1, "comment": "yes"})


class ThreadSummaryBodyShapeTests(_BodyShapeTests, _AgentClient):
    URL = "/api/v2/agent/thread-summary/"
    MISSING_FIELD = "team_id is required."

    def test_a_valid_body_still_answers_what_it_did(self):
        body = {
            "team_id": str(self.team.pk),
            "chat_type": 2,
            "chat_id": _unknown(),
            "thread_id": _unknown(),
        }
        resp = self.client.post(self.URL, body, format="json")
        self.assertEqual(
            (resp.status_code, resp.json()),
            (403, {"error": "Chat not found or has no members."}),
        )


class NoteSummaryBodyShapeTests(_BodyShapeTests, _AgentClient):
    URL = "/api/v2/agent/note-summary/"
    MISSING_FIELD = "team_id is required."

    def test_a_valid_body_still_answers_what_it_did(self):
        body = {"team_id": str(self.team.pk), "note_type": 1, "note_id": 999999}
        resp = self.client.post(self.URL, body, format="json")
        # a missing note, the one answer every kind of "no" on a note gets (MIP-1516)
        self.assertEqual((resp.status_code, resp.json()), (404, {"error": "Note not found."}))


class SearchBodyShapeTests(_BodyShapeTests, _AgentClient):
    URL = "/api/v2/search/"
    MISSING_FIELD = "query is required and must be non-empty."

    def test_a_valid_body_still_answers_what_it_did(self):
        found = {"results": [], "total": 0}
        with patch("origin.search_engine.views.search", return_value=found) as search:
            resp = self.client.post(
                self.URL, {"query": " q ", "team_id": str(self.team.pk)}, format="json"
            )
        self.assertEqual((resp.status_code, resp.json()), (200, found))
        self.assertEqual(search.call_args.kwargs["query"], "q")
