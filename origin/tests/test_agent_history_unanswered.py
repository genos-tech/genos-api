"""MIP-2604: every ask the history list shows opens with a turn.

The history sidebar lists a session whatever its runs ended as, but the turns payload used to skip a run
with neither an answer nor an error, so a cancelled ask, or one paused on a write nobody answered,
opened as an empty conversation. Pinned here:

  * such a run is a turn, with its status;
  * a run awaiting approval carries `pending_approval`, the same card the live stream sent, and that
    card's token still works on /decide/;
  * a run still `running`, and a refused ask (`error` with no message), stay hidden;
  * the list's query count does not grow with the number of sessions.
"""

from __future__ import annotations

import uuid

from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.search_engine import agent_views
from origin.search_engine.models import AgentRun, AgentSession, AgentStep

from origin.search_engine.agent.controller import PENDING_APPROVAL_MARKER

from .test_agent_decide_edits import DecideEditBase

LIST_URL = "/api/v2/agent/sessions/"


class HistoryBase(DecideEditBase):
    def session(self) -> AgentSession:
        return AgentSession.objects.create(team_id=str(self.team.pk), user_id=str(self.user.id))

    def run_row(self, session, query="what is due?", status="done", **fields) -> AgentRun:
        return AgentRun.objects.create(
            session=session,
            team_id=str(self.team.pk),
            user_id=str(self.user.id),
            query=query,
            status=status,
            **fields,
        )

    def paused(self, session, tool_name="add_comment", arguments=None, **fields):
        token = uuid.uuid4()
        run = self.run_row(
            session,
            query="Add a comment",
            status="awaiting_approval",
            pending_approval_token=token,
            **fields,
        )
        AgentStep.objects.create(
            run=run,
            step_index=1,
            tool_name=tool_name,
            arguments_json=arguments or {"task_id": 1, "body_text": "Live check"},
            summary=PENDING_APPROVAL_MARKER,
        )
        return run, token

    def turns(self, session):
        resp = self.client.get(f"{LIST_URL}{session.session_id}/", {"team_id": str(self.team.pk)})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["turns"]


class UnansweredRunsAreTurnsTests(HistoryBase):
    def test_a_cancelled_ask_is_a_turn_with_its_status(self):
        session = self.session()
        self.run_row(session, query="mip 1813", status="cancelled", finished_at=timezone.now())
        (turn,) = self.turns(session)
        self.assertEqual(
            (turn["query"], turn["status"], turn["answer"], turn["error"]),
            ("mip 1813", "cancelled", "", None),
        )
        self.assertNotIn("pending_approval", turn)

    def test_an_ask_awaiting_approval_carries_its_card(self):
        session = self.session()
        run, token = self.paused(session)
        (turn,) = self.turns(session)
        self.assertEqual(turn["status"], "awaiting_approval")
        card = turn["pending_approval"]
        self.assertEqual(card["tool_name"], "add_comment")
        self.assertEqual(card["arguments"]["body_text"], "Live check")
        self.assertEqual(card["approval_token"], str(token))
        self.assertEqual(card["run_id"], str(run.run_id))
        self.assertEqual(card["step"], 1)
        self.assertIn("body_text", card["editable"])
        self.assertIn("undoable", card)

    def test_the_cards_token_still_decides_the_run(self):
        self.fake("add_comment")
        session = self.session()
        self.paused(session)
        card = self.turns(session)[0]["pending_approval"]
        resp = self.post(
            AgentRun.objects.get(run_id=card["run_id"]), card["approval_token"], "reject"
        )
        self.events(resp)
        (turn,) = self.turns(session)
        self.assertEqual(turn["answer"], "all done")
        self.assertNotIn("pending_approval", turn)

    def test_a_pause_after_some_text_still_carries_the_card(self):
        session = self.session()
        self.paused(session, final_answer_text="I will add it.")
        (turn,) = self.turns(session)
        self.assertEqual(turn["answer"], "I will add it.")
        self.assertEqual(turn["pending_approval"]["tool_name"], "add_comment")

    def test_a_pause_with_no_step_to_resume_has_no_card(self):
        session = self.session()
        self.run_row(session, status="awaiting_approval", pending_approval_token=uuid.uuid4())
        (turn,) = self.turns(session)
        self.assertIsNone(turn["pending_approval"])

    def test_a_running_run_and_a_refused_ask_stay_hidden(self):
        session = self.session()
        self.run_row(session, status="running")
        self.run_row(session, status="error")
        self.assertEqual(self.turns(session), [])

    def test_the_turns_stay_in_the_order_asked(self):
        session = self.session()
        self.run_row(session, query="one", final_answer_text="A.")
        self.run_row(session, query="two", status="cancelled")
        self.run_row(session, query="three", final_answer_text="C.")
        self.assertEqual([t["query"] for t in self.turns(session)], ["one", "two", "three"])

    @override_settings(GENOS_SKILLS=False)
    def test_the_payload_builder_does_the_same_for_the_thread_note_and_task_windows(self):
        session = self.session()
        self.run_row(session, status="cancelled")
        self.assertEqual(len(agent_views._build_turns_payload(session)), 1)


class SessionsListQueryTests(HistoryBase):
    def listing(self):
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(LIST_URL, {"team_id": str(self.team.pk)})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["sessions"], len(ctx.captured_queries)

    def add(self, first, *rest):
        session = self.session()
        self.run_row(session, query=first)
        for query in rest:
            self.run_row(session, query=query)
        return session

    def test_the_query_count_does_not_grow_with_the_sessions(self):
        self.add("a", "b")
        self.listing()  # the first request also pays one-time auth reads
        _, one = self.listing()
        for i in range(6):
            self.add(f"q{i}", "later")
        rows, seven = self.listing()
        self.assertEqual(len(rows), 7)
        self.assertEqual(seven, one)

    def test_each_row_keeps_its_first_question_and_turn_count(self):
        two = self.add("first of two", "second")
        empty = self.session()
        rows, _ = self.listing()
        by_id = {row["session_id"]: row for row in rows}
        self.assertEqual(
            (by_id[str(two.session_id)]["first_query"], by_id[str(two.session_id)]["turn_count"]),
            ("first of two", 2),
        )
        self.assertEqual(
            (
                by_id[str(empty.session_id)]["first_query"],
                by_id[str(empty.session_id)]["turn_count"],
            ),
            ("", 0),
        )
