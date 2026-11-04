"""MIP-996: the feedback `comment` and the search `query` are refused when they are not text.

Left over from MIP-992, which gave the other agent endpoints "<field> must be a string.":

* `POST /agent/runs/<id>/feedback/` read `(data.get("comment") or "").strip()` after the run
  lookup and the ownership check, and crashed on a number, a boolean, a list or an object.
* `POST /search/` read `(data.get("query") or "").strip()` and crashed the same way.

Both are a 400 now, with the answer `/ask/` gives for its `query`. The refusal sits where the
field is read, so every answer that comes before it is unchanged, and for the comment it
comes before the row is written: a refused comment must not overwrite the verdict the person
gave last time. Empty-ish values (null, `0`, `false`, `[]`, `{}`, `""`) still mean "not sent".
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from origin.search_engine.models import AgentRun, AgentRunFeedback

from .test_agent_non_object_body import _AgentClient

NOT_TEXT = (5, 1.5, True, [1], {"a": 1})
EMPTY_ISH = (None, "", 0, False, [], {})


class FeedbackCommentTests(_AgentClient):
    def setUp(self):
        super().setUp()
        self.run_ = AgentRun.objects.create(
            team_id=str(self.team.pk), user_id=str(self.user.id), query="q", status="done"
        )
        self.url = f"/api/v2/agent/runs/{self.run_.run_id}/feedback/"

    def post(self, body, url=None):
        return self.client.post(url or self.url, body, format="json")

    def rows(self):
        return list(AgentRunFeedback.objects.order_by("pk").values())

    def test_a_comment_that_is_not_text_is_a_400_not_a_500(self):
        for bad in NOT_TEXT:
            resp = self.post({"rating": 1, "comment": bad})
            self.assertEqual(resp.status_code, 400, repr(bad))
            self.assertEqual(resp.json(), {"error": "comment must be a string."}, repr(bad))

    def test_a_refused_comment_writes_nothing(self):
        for bad in NOT_TEXT:
            self.post({"rating": 1, "comment": bad})
        self.assertEqual(self.rows(), [])

    def test_a_refused_comment_leaves_the_earlier_verdict_as_it_was(self):
        self.assertEqual(self.post({"rating": 1, "comment": "helpful"}).status_code, 200)
        before = self.rows()
        self.assertEqual(len(before), 1)
        for bad in NOT_TEXT:
            resp = self.post({"rating": -1, "comment": bad})
            self.assertEqual(resp.status_code, 400, repr(bad))
        self.assertEqual(self.rows(), before)

    def test_an_empty_comment_is_still_no_comment(self):
        for empty in EMPTY_ISH:
            resp = self.post({"rating": 1, "comment": empty})
            self.assertEqual(
                (resp.status_code, resp.json()),
                (200, {"run_id": str(self.run_.run_id), "rating": 1, "comment": ""}),
                repr(empty),
            )

    def test_a_text_comment_is_still_stored_trimmed(self):
        resp = self.post({"rating": -1, "comment": "  not what I asked  "})
        self.assertEqual(
            (resp.status_code, resp.json()),
            (200, {"run_id": str(self.run_.run_id), "rating": -1, "comment": "not what I asked"}),
        )
        self.assertEqual(AgentRunFeedback.objects.get().comment, "not what I asked")

    def test_every_answer_ahead_of_the_comment_is_unchanged(self):
        # The comment is read last, so a body that is wrong earlier gets the earlier answer.
        resp = self.post({"rating": "x", "comment": 5})
        self.assertEqual(
            (resp.status_code, resp.json()),
            (400, {"error": "rating is required and must be an integer in {-1, 0, 1}."}),
        )
        resp = self.post({"rating": 7, "comment": 5})
        self.assertEqual(
            (resp.status_code, resp.json()),
            (400, {"error": "rating must be one of -1 (👎), 0 (cleared), 1 (👍)."}),
        )
        resp = self.post(
            {"rating": 1, "comment": 5}, url=f"/api/v2/agent/runs/{uuid.uuid4()}/feedback/"
        )
        self.assertEqual((resp.status_code, resp.json()), (404, {"error": "No such agent run."}))

        # Someone else's run is a 403 whatever the comment says.
        self.authenticate(self.user2)
        resp = self.post({"rating": 1, "comment": 5})
        self.assertEqual(
            (resp.status_code, resp.json()),
            (403, {"error": "You can only give feedback on your own agent runs."}),
        )
        self.assertEqual(self.rows(), [])


class SearchQueryTests(_AgentClient):
    URL = "/api/v2/search/"

    def post(self, query):
        return self.client.post(
            self.URL, {"query": query, "team_id": str(self.team.pk)}, format="json"
        )

    def test_a_query_that_is_not_text_is_a_400_and_searches_nothing(self):
        with patch("origin.search_engine.views.search") as search:
            for bad in NOT_TEXT:
                resp = self.post(bad)
                self.assertEqual(resp.status_code, 400, repr(bad))
                self.assertEqual(resp.json(), {"error": "query must be a string."}, repr(bad))
        search.assert_not_called()

    def test_an_empty_query_is_still_required(self):
        with patch("origin.search_engine.views.search") as search:
            for empty in EMPTY_ISH + ("   ",):
                resp = self.post(empty)
                self.assertEqual(
                    (resp.status_code, resp.json()),
                    (400, {"error": "query is required and must be non-empty."}),
                    repr(empty),
                )
        search.assert_not_called()

    def test_a_text_query_still_searches_trimmed(self):
        found = {"results": [], "total": 0}
        with patch("origin.search_engine.views.search", return_value=found) as search:
            resp = self.post(" q ")
        self.assertEqual((resp.status_code, resp.json()), (200, found))
        self.assertEqual(search.call_args.kwargs["query"], "q")
