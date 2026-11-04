"""MIP-992: `/ask/` refuses a field of the wrong type before anything is spent.

`session_id`, `thread_context` and `note_context` were read AFTER the quota gates and the
cost meter's `open_request` (which writes the request's rollup row), and crashed there on a
value of the wrong type: `.strip()` on a number, `.get` on a list. `query` crashed before
them. All four are refused first now, in one block at the top of the view, so a refused
body spends and records nothing. Empty-ish values (null, `0`, `false`, `[]`, `{}`) still
mean "not sent".

A context that is not an object gets the answer an object with the wrong fields already
got (`thread_context must have an integer chat_type ...`): a client sees one message for
one mistake. `team_id`, `locale`, `mentions` and the booleans already answered cleanly,
or are read leniently on purpose, and are left alone.

`AskRefusedSpendsNothingTests` reads the rollups, the credit ledger, the spend events and
the daily counters before and after each refusal.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from django.core.cache import cache
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.usage_models import ModelUsageCounter
from origin.search_engine import credit_ledger
from origin.search_engine.models import (
    AgentRun,
    AgentSession,
    AiCreditEntry,
    AiRequestCost,
    AiSpendEvent,
)

from .test_agent_non_object_body import ASK, _AgentClient
from .test_credit_enforcement import AUTHORITATIVE, _StreamingBase


class AskFieldTypeTests(_AgentClient):
    """The fields `/ask/` reads as text or as an object, with a wrong type: a 400.
    The ones that already answered cleanly keep their answer."""

    def _ask(self, **fields):
        return self.client.post(
            ASK, {"query": "q", "team_id": str(self.team.pk), **fields}, format="json"
        )

    def test_a_text_field_that_is_not_text_is_a_400_not_a_500(self):
        for field in ("query", "session_id"):
            for bad in (5, 1.5, True, [1], {"a": 1}):
                resp = self._ask(**{field: bad})
                self.assertEqual(resp.status_code, 400, (field, bad))
                self.assertEqual(resp.json(), {"error": f"{field} must be a string."}, (field, bad))

    def test_a_context_that_is_not_an_object_is_the_answer_an_object_with_no_fields_gets(self):
        for field, message in (
            (
                "thread_context",
                "thread_context must have an integer chat_type and UUID-string chat_id and "
                "thread_id.",
            ),
            ("note_context", "note_context must have integer note_type and note_id."),
        ):
            reference = self._ask(**{field: {"a": 1}})
            self.assertEqual((reference.status_code, reference.json()), (400, {"error": message}))
            for bad in (5, 1.5, True, [1], "x"):
                resp = self._ask(**{field: bad})
                self.assertEqual(
                    (resp.status_code, resp.json()), (reference.status_code, reference.json()), bad
                )

    def test_an_empty_query_is_still_required(self):
        for empty in (None, "", "   ", 0, False, [], {}):
            resp = self.client.post(
                ASK, {"query": empty, "team_id": str(self.team.pk)}, format="json"
            )
            self.assertEqual(resp.status_code, 400, repr(empty))
            self.assertEqual(
                resp.json(), {"error": "query is required and must be non-empty."}, repr(empty)
            )

    def test_the_fields_that_already_answered_cleanly_still_do(self):
        for bad in (5, True, [1], {"a": 1}):
            resp = self._ask(team_id=bad)
            self.assertEqual((resp.status_code, resp.json()), (404, {"error": "Team not found."}))
        for bad in (5, True, {"a": 1}, "x"):
            resp = self._ask(mentions=bad)
            self.assertEqual(
                (resp.status_code, resp.json()), (400, {"error": "mentions must be a list."})
            )


@AUTHORITATIVE
class AskRefusedSpendsNothingTests(_StreamingBase):
    """A refused ask changes nothing that costs or counts.

    The meter is ON (`AI_COST_METER`, `AI_CREDITS_SHADOW`, `AI_CREDITS_AUTHORITATIVE`),
    because with it off `open_request` writes nothing and the comparison would pass for
    the wrong reason; `test_a_served_ask_does_move_them` is the control that says the
    snapshot can see a spend."""

    def setUp(self):
        super().setUp()
        self.client = APIClient(raise_request_exception=False)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.user).access_token}"
        )
        credit_ledger.ensure_monthly_grant(str(self.user.id), "free")
        cache.clear()

    def _state(self):
        return {
            "rollups": AiRequestCost.objects.count(),
            "credit entries": AiCreditEntry.objects.count(),
            "spend events": AiSpendEvent.objects.count(),
            "daily counters": sorted(
                ModelUsageCounter.objects.values_list(
                    "user_id", "model_name", "usage_date", "count"
                )
            ),
            "runs": AgentRun.objects.count(),
            "sessions": AgentSession.objects.count(),
        }

    def _post(self, body):
        """Post JSON text; a fake agent stands in for the run, as in `_StreamingBase._ask`."""

        def fake_run_agent(query, ctx, emit, **kwargs):
            emit({"type": "answer_delta", "text": "hi"})
            emit({"type": "done"})
            from django.db import connections  # noqa: PLC0415

            connections.close_all()
            return None

        with (
            patch("origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent),
            patch("origin.search_engine.ingestion.ingest_conversation_run", return_value=False),
        ):
            resp = self.client.post(ASK, body, content_type="application/json")
            if resp.status_code == 200:
                b"".join(resp.streaming_content)
            return resp

    def _body(self, **fields):
        return json.dumps({"query": "q", "team_id": str(self.team.pk), **fields})

    def test_a_served_ask_does_move_them(self):
        before = self._state()
        resp = self._post(self._body())
        after = self._state()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(after["rollups"], before["rollups"] + 1)
        self.assertNotEqual(after["daily counters"], before["daily counters"])
        self.assertEqual(after["runs"], before["runs"] + 1)

    def test_a_refused_body_spends_and_records_nothing(self):
        refused = {f"body {raw}": raw for raw in ("[1]", '"abc"', "5", "true")}
        for field, bads in (
            ("query", (5, [1], {"a": 1})),
            ("session_id", (5, 1.5, True, [1], {"a": 1})),
            ("thread_context", (5, [1], "x")),
            ("note_context", (5, [1], "x")),
        ):
            for bad in bads:
                refused[f"{field}={bad!r}"] = self._body(**{field: bad})

        for label, raw in refused.items():
            before = self._state()
            resp = self._post(raw)
            self.assertEqual(resp.status_code, 400, label)
            self.assertEqual(self._state(), before, label)

    def test_empty_ish_values_are_still_not_sent(self):
        for field in ("session_id", "thread_context", "note_context"):
            for empty in (None, "", 0, False, [], {}):
                resp = self._post(self._body(**{field: empty}))
                self.assertEqual(resp.status_code, 200, (field, empty))
