"""MIP-991: a run resumed after an approval keeps the earlier turns its first leg had.

On a follow-up ask (turn 2 or later) `run_agent` puts the earlier turns of the
conversation in front of the question: the rolling recap (`prior_summary`, one
assistant message) and the verbatim `prior_turns` (a user message and an
assistant message each). When that ask pauses for a write and the person
approves or rejects, `/decide/` resumes the run through `resume_agent`, which
rebuilds the conversation from the run's own rows: its `query` and its steps.
So the resumed leg had NEITHER the recap nor the earlier turns, for the rest of
the run. The write itself is not affected (its arguments were fixed before the
pause); what is lost is the model's picture of the conversation it is finishing.

What these tests pin, and why they are shaped as they are:

  * The ask-to-decide round trip runs the REAL code on every leg (the view, the
    loop, the pause, `/decide/`, `resume_agent`). Only the model, the recap's
    LLM call and the write tool are faked. The model records the messages of
    every call it is sent, so "the resumed leg sees what the first leg saw" is a
    comparison of what the provider would have received, not of two stubs.
    A spy on `_drive_loop` splits those calls by leg.
  * The resumed leg's messages START WITH the first leg's, exactly (same order,
    same text), and continue with the replay of the pending call and its result.
    That is also what lets a provider's prompt cache carry over.
  * A normal (non-resumed) ask must send exactly what it sent before. The
    `NormalAskUnchangedTests` pins were recorded on the MIP-977 branch BEFORE
    this change was written, and pass before and after it.
  * The earlier TURNS are not copied onto the run: only the ids of the runs
    that made up the window, and the recap (which cannot be rebuilt: it is an
    LLM text that every later ask of the session overwrites). The tests that
    delete or change an earlier run after the pause say what the resumed leg
    sees then.

`TransactionTestCase`, as in `test_agent_resume_system_extra`: both legs stream
on real worker threads whose writes must really commit.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone

from origin.models.chat.unified_models import ChannelMember
from origin.search_engine import agent_views
from origin.search_engine.agent import controller, multi_turn
from origin.search_engine.agent.controller import PENDING_APPROVAL_MARKER
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.types import AgentMessage, FunctionCall
from origin.search_engine.models import AgentRun, AgentSession, AgentStep
from origin.tests.test_agent_resume_system_extra import (
    ASK_URL,
    PROPOSAL,
    THREAD_SUMMARY,
    ResumeBase,
    ScriptedModel,
)

RECAP_LINE = "[Context recap from earlier in this conversation: {}]"


def search_engine(**overrides):
    """`SEARCH_ENGINE` with the session window and the recap flag set explicitly,
    so nothing here depends on what the environment's defaults happen to be."""
    config = dict(settings.SEARCH_ENGINE)
    config.update(overrides)
    return override_settings(SEARCH_ENGINE=config)


# A window of two turns with the recap on: the third and later asks have turns
# older than the window, which is what makes a recap appear.
WINDOW_2 = search_engine(SESSION_MAX_PRIOR_TURNS=2, RAG_SESSION_ROLLING_SUMMARY=True)


def shape(messages):
    """The messages as the provider would be sent them, comparable and readable:
    `(role, text)` for text turns, `(role, tool name)` for a call and its result."""
    out = []
    for m in messages:
        if m.function_call is not None:
            out.append((m.role, f"call:{m.function_call.name}"))
        elif m.function_response_name is not None:
            out.append((m.role, f"result:{m.function_response_name}"))
        else:
            out.append((m.role, m.text))
    return out


AFTER_THE_PAUSE = [("assistant", "call:create_task"), ("tool_response", "result:create_task")]


def answered(*numbers, recap=None):
    """`conversation` of the earlier turns `q<n>` the tests ask."""
    return conversation(*[(f"q{n}", f"ANSWER to: q{n}") for n in numbers], recap=recap)


def conversation(*pairs, recap=None):
    """What a follow-up ask's messages look like before its own question: the
    recap (when there is one), then each earlier turn."""
    out = [("assistant", RECAP_LINE.format(recap))] if recap else []
    for question, answer in pairs:
        out += [("user", question), ("assistant", answer)]
    return out


class ConversationModel(ScriptedModel):
    """The model. Each call plays the next entry of `script`: "write" proposes the
    approval-gated tool, anything else answers `ANSWER to: <the question>`. Records
    the messages of every call, deep-copied because the loop appends to the list
    it handed over, grouped by leg (`ResumePriorBase.buckets`)."""

    def __init__(self, script, on_call):
        super().__init__(script)
        self.on_call = on_call

    def generate_step(self, messages, tools, system_instruction, **kwargs):
        self.on_call(copy.deepcopy(messages))
        question = [m.text for m in messages if m.role == "user"][-1]
        self.system_instructions.append(system_instruction)
        step = self.script.pop(0) if self.script else "answer"
        if step == "write":
            yield (None, FunctionCall(name="create_task", args=dict(PROPOSAL)))
        else:
            yield (f"ANSWER to: {question}", None)


class ResumePriorBase(ResumeBase):
    """`ResumeBase` with a model that answers per question and records messages,
    a deterministic recap, and a record of what each leg handed the loop."""

    script = ()

    def setUp(self):
        super().setUp()
        # Messages the loop was handed at entry, one list per leg (every ask and every
        # resume, in order), and the model's calls grouped the same way.
        self.entry: list[list[AgentMessage]] = []
        self.buckets: list[list[list[AgentMessage]]] = []
        self.recap_calls = 0
        self.model = ConversationModel(self.script, lambda m: self.buckets[-1].append(m))

        previous = controller._drive_loop  # ResumeBase's spy: records system_extra, runs the loop

        def spy(**kwargs):
            self.entry.append(copy.deepcopy(kwargs["messages"]))
            self.buckets.append([])
            return previous(**kwargs)

        def fake_recap(system, user_prompt, *, model_override):
            # The recap is an LLM text. A fresh, different one per call, so a test can tell
            # "the text the first leg saw" from "a regenerated one".
            self.recap_calls += 1
            return f"RECAP-{self.recap_calls}"

        for patcher in (
            mock.patch.object(controller, "get_model_client", return_value=self.model),
            mock.patch.object(controller, "_drive_loop", side_effect=spy),
            mock.patch.object(multi_turn, "_generate", side_effect=fake_recap),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- conversations ------------------------------------------------------

    def plain(self, query, session_id=None, **extra):
        """One ask that answers outright; returns the session it ran in."""
        payload = {"query": query, "team_id": str(self.team.team_id), **extra}
        if session_id:
            payload["session_id"] = session_id
        events = self.read(self.client.post(ASK_URL, payload, format="json"))
        done = [e for e in events if e["type"] == "done"]
        self.assertEqual(len(done), 1, events)
        return done[0]["session_id"]

    def earlier_turns(self, count, **extra):
        """`count` answered asks in one fresh session: `q1`..`q{count}`."""
        session_id = None
        for number in range(1, count + 1):
            session_id = self.plain(f"q{number}", session_id, **extra)
        return session_id

    def pause(self, query, session_id, *, script=("write", "answer"), **extra):
        """The ask that pauses on the write: returns its run id and approval token."""
        self.model.script[:] = list(script)
        return self.ask(query=query, session_id=session_id, **extra)

    def paused_legacy(self, session=None, **fields):
        """A run paused by code that kept nothing for the resume: a row and its
        pending step, as `/ask/` left them before `resume_context` existed."""
        token = uuid.uuid4()
        run = AgentRun.objects.create(
            team_id=str(self.team.pk),
            user_id=str(self.user.id),
            query="make me a task",
            status="awaiting_approval",
            pending_approval_token=token,
            session=session,
            **fields,
        )
        AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name="create_task",
            arguments_json=dict(PROPOSAL),
            summary=PENDING_APPROVAL_MARKER,
        )
        return str(run.run_id), str(token)

    def answered_session(self, count):
        """A session holding `count` finished turns, written straight to the table."""
        session = AgentSession.objects.create(team_id=str(self.team.pk), user_id=str(self.user.id))
        for number in range(1, count + 1):
            AgentRun.objects.create(
                team_id=str(self.team.pk),
                user_id=str(self.user.id),
                session=session,
                query=f"q{number}",
                final_answer_text=f"ANSWER to: q{number}",
                status="done",
            )
        return session

    # -- reading what a leg was sent -----------------------------------------

    def first_call(self, leg):
        """The messages of the first model call of the `leg`-th leg of the test."""
        return self.buckets[leg][0]

    def assert_resumes_from(self, first, resumed):
        """What the whole fix is for. The resumed leg's first call is the first
        leg's first call, message for message, followed by the pending call and what
        became of it (the same two messages whether it was approved or rejected)."""
        self.assertEqual(shape(resumed[: len(first)]), shape(first))  # a diff a person can read
        self.assertEqual(resumed[: len(first)], first)  # and the messages themselves
        self.assertEqual(shape(resumed[len(first) :]), AFTER_THE_PAUSE)


class NormalAskUnchangedTests(ResumePriorBase):
    """What a normal (non-resumed) ask hands the model, message for message.

    Recorded on the MIP-977 branch (39c64314) BEFORE this change was written, over
    inputs fixed here (the question texts, a recap that says `RECAP-n`). The ask
    path is only read by the fix: its one edit is that the lines that put the
    earlier turns in front of the question became a function both legs call.
    These fail if the ask stops sending what it sent.
    """

    def check(self, expected, *, earlier, **extra):
        session_id = self.earlier_turns(earlier, **extra) if earlier else None
        self.plain("the question", session_id, **extra)
        self.assertEqual(shape(self.buckets[-1][0]), expected)

    def test_a_first_turn_ask_sends_only_its_question(self):
        self.check([("user", "the question")], earlier=0)

    def test_a_second_turn_ask_sends_the_first_turn_then_its_question(self):
        self.check(conversation(("q1", "ANSWER to: q1")) + [("user", "the question")], earlier=1)

    @WINDOW_2
    def test_a_ask_inside_the_window_has_no_recap(self):
        self.check(
            conversation(("q1", "ANSWER to: q1"), ("q2", "ANSWER to: q2"))
            + [("user", "the question")],
            earlier=2,
        )

    @WINDOW_2
    def test_an_ask_past_the_window_sends_the_recap_then_the_last_turns(self):
        # Four earlier turns, a window of two: the first two are the recap's,
        # which is built by a model call that the first earlier turn that aged out
        # triggered (`RECAP-1`) and the next one folded into (`RECAP-2`).
        self.check(
            conversation(("q3", "ANSWER to: q3"), ("q4", "ANSWER to: q4"), recap="RECAP-2")
            + [("user", "the question")],
            earlier=4,
        )

    @search_engine(SESSION_MAX_PRIOR_TURNS=2, RAG_SESSION_ROLLING_SUMMARY=False)
    def test_with_the_recap_off_only_the_window_is_sent(self):
        self.check(
            conversation(("q3", "ANSWER to: q3"), ("q4", "ANSWER to: q4"))
            + [("user", "the question")],
            earlier=4,
        )
        self.assertEqual(self.recap_calls, 0)

    @search_engine(SESSION_MAX_PRIOR_TURNS=2, SESSION_PRIOR_ANSWER_MAX_CHARS=5)
    def test_earlier_answers_are_cut_at_the_cap(self):
        self.check(
            conversation(("q1", "ANSWE"), ("q2", "ANSWE")) + [("user", "the question")],
            earlier=2,
        )

    def test_the_loop_is_handed_the_same_list_the_first_model_call_got(self):
        session_id = self.earlier_turns(1)
        self.plain("the question", session_id)
        self.assertEqual(shape(self.entry[-1]), shape(self.buckets[-1][0]))

    def test_run_agent_puts_the_recap_then_the_turns_then_the_question(self):
        seen = {}

        def fake_drive(**kwargs):
            seen["messages"] = copy.deepcopy(kwargs["messages"])

        with mock.patch.object(controller, "_drive_loop", side_effect=fake_drive):
            controller.run_agent(
                "the question",
                ToolContext(team_id="t", user_id="u"),
                lambda event: None,
                prior_turns=[("a", "A"), ("b", "B")],
                prior_summary="THE RECAP",
            )
        self.assertEqual(
            shape(seen["messages"]),
            [("assistant", RECAP_LINE.format("THE RECAP"))]
            + [("user", "a"), ("assistant", "A"), ("user", "b"), ("assistant", "B")]
            + [("user", "the question")],
        )
        seen.clear()
        with mock.patch.object(controller, "_drive_loop", side_effect=fake_drive):
            controller.run_agent("alone", ToolContext(team_id="t", user_id="u"), lambda e: None)
        self.assertEqual(shape(seen["messages"]), [("user", "alone")])


class ResumedLegSeesTheEarlierTurnsTests(ResumePriorBase):
    """The loss itself: what the resumed leg is sent, against what the first leg was."""

    def check(self, decision, earlier, **ask_context):
        session_id = self.earlier_turns(earlier, **ask_context)
        run_id, token = self.pause("the question", session_id, **ask_context)
        self.decide_to_the_end(run_id, token, decision)
        first, resumed = self.first_call(earlier), self.first_call(earlier + 1)
        self.assertEqual(
            shape(first), answered(*range(1, earlier + 1)) + [("user", "the question")]
        )
        self.assert_resumes_from(first, resumed)
        # What the loop was handed is what the provider was sent.
        self.assertEqual(self.entry[earlier + 1], resumed)
        return first, resumed

    def test_a_second_turn_ask_approved(self):
        self.check("approve", earlier=1)
        self.assertEqual(self.tool.calls, [PROPOSAL])

    def test_a_second_turn_ask_rejected(self):
        self.check("reject", earlier=1)
        self.assertEqual(self.tool.calls, [])

    def test_a_third_turn_ask_approved(self):
        self.check("approve", earlier=2)

    def test_a_third_turn_ask_rejected(self):
        self.check("reject", earlier=2)

    def test_a_deep_conversation_resumes_with_all_of_it(self):
        self.check("approve", earlier=6)

    def test_every_leg_of_a_run_that_pauses_twice_has_the_same_earlier_turns(self):
        session_id = self.earlier_turns(2)
        run_id, token = self.pause("the question", session_id, script=("write", "write", "answer"))
        events = self.decide_to_the_end(run_id, token)
        (again,) = [e for e in events if e["type"] == "tool_call_pending_approval"]
        self.decide_to_the_end(run_id, again["approval_token"])
        first, second, third = (self.first_call(n) for n in (2, 3, 4))
        self.assertEqual(second[: len(first)], first)
        # The third leg replays both pending calls after the same start.
        self.assertEqual(third[: len(first)], first)
        self.assertEqual(shape(third[len(first) :]), AFTER_THE_PAUSE * 2)

    def test_a_first_turn_ask_resumes_as_it_always_did(self):
        run_id, token = self.pause("the question", None)
        self.decide_to_the_end(run_id, token)
        self.assertEqual(shape(self.first_call(0)), [("user", "the question")])
        self.assertEqual(shape(self.first_call(1)), [("user", "the question")] + AFTER_THE_PAUSE)

    def test_both_legs_send_the_provider_the_same_system_instruction(self):
        # Unchanged by this fix (MIP-977 already pins it for a thread ask); here so the
        # claim "the first leg's request is a prefix of the resumed leg's" is the whole
        # request: same system string, same messages up to the pause.
        session_id = self.earlier_turns(2)
        run_id, token = self.pause("the question", session_id)
        self.decide_to_the_end(run_id, token)
        self.assertEqual(self.model.system_instructions[3], self.model.system_instructions[2])

    def test_the_approved_write_is_not_affected(self):
        session_id = self.earlier_turns(2)
        run_id, token = self.pause("the question", session_id)
        events = self.decide_to_the_end(run_id, token)
        self.assertEqual(self.tool.calls, [PROPOSAL])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(self.row(run_id).status, "done")


class WindowAndRecapTests(ResumePriorBase):
    """More turns than fit the window, and the recap that stands in for the rest."""

    def decide_after(self, earlier):
        session_id = self.earlier_turns(earlier)
        run_id, token = self.pause("the question", session_id)
        return session_id, run_id, token

    @WINDOW_2
    def test_the_resumed_leg_has_the_same_window_and_the_same_recap(self):
        session_id, run_id, token = self.decide_after(4)
        calls_before = self.recap_calls
        self.decide_to_the_end(run_id, token)
        first, resumed = self.first_call(4), self.first_call(5)
        self.assertEqual(shape(first), answered(3, 4, recap="RECAP-2") + [("user", "the question")])
        self.assert_resumes_from(first, resumed)
        self.assertEqual(self.recap_calls, calls_before, "the recap is not made again")

    @WINDOW_2
    def test_the_recap_is_the_first_legs_not_the_sessions_current_one(self):
        # The session's summary belongs to the session, and every later ask moves it on:
        # at the decision it may be a different text, or cover turns this run never saw.
        session_id, run_id, token = self.decide_after(4)
        AgentSession.objects.filter(session_id=session_id).update(
            rolling_summary_text="MOVED ON", rolling_summary_through=3
        )
        self.decide_to_the_end(run_id, token)
        first, resumed = self.first_call(4), self.first_call(5)
        self.assert_resumes_from(first, resumed)
        self.assertIn(("assistant", RECAP_LINE.format("RECAP-2")), shape(resumed))
        self.assertNotIn("MOVED ON", json.dumps(shape(resumed)))

    @WINDOW_2
    def test_the_recap_is_not_asked_for_again_when_the_summariser_is_down(self):
        session_id, run_id, token = self.decide_after(4)
        with mock.patch.object(multi_turn, "_generate", side_effect=AssertionError("called")):
            self.decide_to_the_end(run_id, token)
        self.assertIn(("assistant", RECAP_LINE.format("RECAP-2")), shape(self.first_call(5)))

    @WINDOW_2
    def test_inside_the_window_there_is_no_recap_on_either_leg(self):
        session_id, run_id, token = self.decide_after(2)
        self.decide_to_the_end(run_id, token)
        first, resumed = self.first_call(2), self.first_call(3)
        self.assertEqual(shape(first), answered(1, 2) + [("user", "the question")])
        self.assert_resumes_from(first, resumed)
        self.assertEqual(self.recap_calls, 0)

    @search_engine(SESSION_MAX_PRIOR_TURNS=2, RAG_SESSION_ROLLING_SUMMARY=False)
    def test_with_the_recap_off_neither_leg_has_one(self):
        session_id, run_id, token = self.decide_after(4)
        self.decide_to_the_end(run_id, token)
        first, resumed = self.first_call(4), self.first_call(5)
        self.assertEqual(shape(first), answered(3, 4) + [("user", "the question")])
        self.assert_resumes_from(first, resumed)
        self.assertEqual(self.recap_calls, 0)

    @search_engine(SESSION_MAX_PRIOR_TURNS=2, RAG_SESSION_ROLLING_SUMMARY=True)
    def test_the_window_is_the_first_legs_even_if_the_setting_moves(self):
        session_id, run_id, token = self.decide_after(4)
        with search_engine(SESSION_MAX_PRIOR_TURNS=1, RAG_SESSION_ROLLING_SUMMARY=False):
            self.decide_to_the_end(run_id, token)
        self.assert_resumes_from(self.first_call(4), self.first_call(5))

    @search_engine(SESSION_MAX_PRIOR_TURNS=2, SESSION_PRIOR_ANSWER_MAX_CHARS=5)
    def test_answers_are_cut_at_the_cap_on_both_legs(self):
        session_id, run_id, token = self.decide_after(2)
        self.decide_to_the_end(run_id, token)
        first, resumed = self.first_call(2), self.first_call(3)
        self.assertEqual(
            shape(first),
            conversation(("q1", "ANSWE"), ("q2", "ANSWE")) + [("user", "the question")],
        )
        self.assert_resumes_from(first, resumed)


class EarlierRunsChangeWhileTheCardWaitsTests(ResumePriorBase):
    """The earlier turns are other runs' rows, read again at the decision."""

    def pause_after(self, earlier, **ask_context):
        session_id = self.earlier_turns(earlier, **ask_context)
        run_id, token = self.pause("the question", session_id, **ask_context)
        return session_id, run_id, token

    def test_a_new_ask_in_the_same_session_while_the_card_waits_does_not_appear(self):
        session_id, run_id, token = self.pause_after(2)
        self.model.script[:] = ["answer", "answer"]
        self.plain("a later question", session_id)
        self.decide_to_the_end(run_id, token)
        first, resumed = self.first_call(2), self.first_call(4)
        self.assert_resumes_from(first, resumed)
        self.assertNotIn("a later question", json.dumps(shape(resumed)))

    def test_a_later_ask_does_not_change_the_recap_the_resumed_leg_has(self):
        with WINDOW_2:
            session_id, run_id, token = self.pause_after(4)
            # Two later asks: the second has a turn more than the recap covers, folds it in
            # and moves the session's recap on (`RECAP-3`), which is not the one this run saw.
            self.model.script[:] = ["answer", "answer", "answer"]
            self.plain("a later question", session_id)
            self.plain("another one", session_id)
            self.assertEqual(self.recap_calls, 3)
            self.decide_to_the_end(run_id, token)
        first, resumed = self.first_call(4), self.first_call(7)
        self.assert_resumes_from(first, resumed)
        self.assertIn(("assistant", RECAP_LINE.format("RECAP-2")), shape(resumed))
        self.assertNotIn("RECAP-3", json.dumps(shape(resumed)))

    def test_an_earlier_run_that_retention_deleted_is_left_out(self):
        # `agent_runs` retention DELETES a finished run; it never touches the paused one.
        from origin.services import retention  # noqa: PLC0415

        session_id, run_id, token = self.pause_after(3)
        old = timezone.now() - timedelta(days=90)
        AgentRun.objects.filter(session_id=session_id).update(started_at=old)
        with mock.patch.dict(os.environ, {"RETENTION_AGENT_RUNS_DAYS": "30"}):
            result = retention.run_policy(retention.BY_NAME["agent_runs"])
        self.assertEqual(result.changed, 3, "the three finished turns went, the paused run stayed")
        self.assertTrue(AgentRun.objects.filter(run_id=run_id).exists())
        self.decide_to_the_end(run_id, token)
        resumed = self.first_call(4)
        self.assertEqual(shape(resumed), [("user", "the question")] + AFTER_THE_PAUSE)

    def test_one_earlier_run_gone_leaves_the_others_in_order(self):
        session_id, run_id, token = self.pause_after(3)
        AgentRun.objects.filter(query="q2").delete()
        events = self.decide_to_the_end(run_id, token)  # 200, never a 4xx after the token went
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(
            shape(self.first_call(4)),
            answered(1, 3) + [("user", "the question")] + AFTER_THE_PAUSE,
        )
        self.assertEqual(self.tool.calls, [PROPOSAL], "the approved write still ran")

    def test_an_earlier_answer_blanked_meanwhile_is_left_out_like_an_unanswered_turn(self):
        session_id, run_id, token = self.pause_after(3)
        AgentRun.objects.filter(query="q2").update(final_answer_text="")
        self.decide_to_the_end(run_id, token)
        self.assertEqual(
            shape(self.first_call(4)),
            answered(1, 3) + [("user", "the question")] + AFTER_THE_PAUSE,
        )

    def test_every_earlier_run_gone_resumes_with_just_the_question(self):
        session_id, run_id, token = self.pause_after(2)
        AgentRun.objects.filter(session_id=session_id).exclude(run_id=run_id).delete()
        self.decide_to_the_end(run_id, token)
        self.assertEqual(shape(self.first_call(3)), [("user", "the question")] + AFTER_THE_PAUSE)

    @WINDOW_2
    def test_the_recap_survives_when_the_runs_it_stands_in_for_are_gone(self):
        session_id, run_id, token = self.pause_after(4)
        AgentRun.objects.filter(session_id=session_id).exclude(run_id=run_id).delete()
        self.decide_to_the_end(run_id, token)
        self.assertEqual(
            shape(self.first_call(5)),
            [("assistant", RECAP_LINE.format("RECAP-2")), ("user", "the question")]
            + AFTER_THE_PAUSE,
        )

    def test_an_earlier_answer_that_changed_meanwhile_is_read_as_it_is_now(self):
        # The ONE place the resumed leg's earlier turns can differ from the first
        # leg's, and the price of keeping their TEXT out of a second table: a turn is
        # kept as the id of its run, so a run whose answer was rewritten since (an
        # earlier paused card decided in between appends to its answer) is read as it
        # is now. The turn is still the same turn, in the same place.
        session_id, run_id, token = self.pause_after(2)
        AgentRun.objects.filter(query="q2").update(final_answer_text="ANSWER to: q2, and more")
        self.decide_to_the_end(run_id, token)
        self.assertEqual(
            shape(self.first_call(3)),
            conversation(("q1", "ANSWER to: q1"), ("q2", "ANSWER to: q2, and more"))
            + [("user", "the question")]
            + AFTER_THE_PAUSE,
        )

    def test_deleting_the_conversation_while_the_card_waits_deletes_the_card(self):
        session_id, run_id, token = self.pause_after(2)
        agent_views._delete_agent_conversations(user_id=str(self.user.id), session_id=session_id)
        self.assertEqual(self.decide(run_id, token).status_code, 404)
        self.assertEqual(len(self.buckets), 3, "nothing resumed")
        self.assertEqual(self.tool.calls, [])


class ScopedSessionTests(ResumePriorBase):
    """A thread or note session: long-lived, so the one with the most earlier turns."""

    def test_a_thread_session_resumes_with_its_earlier_turns_and_its_summary(self):
        thread = self.make_thread()
        session_id = self.earlier_turns(2, thread_context=thread)
        run_id, token = self.pause("the question", session_id, thread_context=thread)
        self.decide_to_the_end(run_id, token)
        first, resumed = self.first_call(2), self.first_call(3)
        self.assertEqual(shape(first), answered(1, 2) + [("user", "the question")])
        self.assert_resumes_from(first, resumed)
        # MIP-977's context rides beside it, unchanged.
        self.assertIn(THREAD_SUMMARY, self.legs[2])
        self.assertEqual(self.legs[3], self.legs[2])

    def test_a_note_session_resumes_with_its_earlier_turns_and_its_summary(self):
        note = self.make_note()
        session_id = self.earlier_turns(2, note_context=note)
        run_id, token = self.pause("the question", session_id, note_context=note)
        self.decide_to_the_end(run_id, token, "reject")
        first, resumed = self.first_call(2), self.first_call(3)
        self.assert_resumes_from(first, resumed)
        self.assertEqual(self.legs[3], self.legs[2])

    def test_a_thread_the_person_left_loses_the_summary_but_keeps_the_earlier_turns(self):
        # Decided, not left open. MIP-977 drops the thread SUMMARY at a decision when the
        # person can no longer read the thread, because it is hidden context that a fresh
        # call could surface. The earlier turns are answers the person was already shown,
        # in a conversation `/ask/` itself carries on from with no access check at all
        # (it only requires the session to be theirs), so there is nothing new to guard.
        thread = self.make_thread()
        session_id = self.earlier_turns(2, thread_context=thread)
        run_id, token = self.pause("the question", session_id, thread_context=thread)
        ChannelMember.objects.filter(channel=self.channel, user=self.user).delete()
        self.decide_to_the_end(run_id, token)
        first, resumed = self.first_call(2), self.first_call(3)
        self.assert_resumes_from(first, resumed)
        self.assertIsNone(self.legs[3], "the summary was dropped (MIP-977)")
        self.assertNotIn(THREAD_SUMMARY, self.model.system_instructions[3])

    def test_the_scope_of_a_session_is_not_a_filter_on_its_turns(self):
        # `shared_answers.scoped_session_q` (MIP-978) keeps scoped runs out of the team lane.
        # It has no part in which earlier turns a follow-up carries: every turn of the
        # session, scoped or not, is in the window.
        thread = self.make_thread()
        session_id = self.earlier_turns(3, thread_context=thread)
        self.assertEqual(AgentRun.objects.filter(session_id=session_id).count(), 3)
        run_id, token = self.pause("the question", session_id, thread_context=thread)
        self.assertEqual(
            self.row(run_id).resume_context["prior"]["run_ids"],
            [
                str(r)
                for r in AgentRun.objects.filter(session_id=session_id)
                .exclude(run_id=run_id)
                .order_by("started_at")
                .values_list("run_id", flat=True)
            ],
        )


class LegacyRunsTests(ResumePriorBase):
    """A run paused before this shipped has no earlier turns kept, and resumes as before."""

    def test_nothing_kept_resumes_with_just_the_question_even_in_a_session_with_turns(self):
        session = self.answered_session(2)
        run_id, token = self.paused_legacy(session=session)
        events = self.decide_to_the_end(run_id, token)
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(shape(self.first_call(0)), [("user", "make me a task")] + AFTER_THE_PAUSE)

    def test_a_context_kept_by_mip_977_alone_resumes_as_977_did(self):
        # Paused between the two changes shipping: `resume_context` has the system_extra
        # parts and no earlier turns. It resumes with the first, and not the second.
        session = self.answered_session(2)
        kept = {"scope": None, "scoped": None, "rest": "REFERENCES-BLOCK"}
        run_id, token = self.paused_legacy(session=session, resume_context=kept)
        self.decide_to_the_end(run_id, token)
        self.assertEqual(self.legs, ["REFERENCES-BLOCK"])
        self.assertEqual(shape(self.first_call(0)), [("user", "make me a task")] + AFTER_THE_PAUSE)

    def test_a_run_with_no_session_resumes_as_before(self):
        run_id, token = self.paused_legacy(session=None)
        self.decide_to_the_end(run_id, token)
        self.assertEqual(shape(self.first_call(0)), [("user", "make me a task")] + AFTER_THE_PAUSE)

    def test_a_malformed_kept_value_never_crashes_and_degrades_to_what_is_readable(self):
        session = self.answered_session(1)
        other_user = self.other
        foreign = AgentRun.objects.create(
            team_id=str(self.team.pk),
            user_id=str(other_user.id),
            query="foreign question",
            final_answer_text="FOREIGN SECRET",
            status="done",
        )
        elsewhere = AgentRun.objects.create(
            team_id=str(self.team.pk),
            user_id=str(self.user.id),
            session=AgentSession.objects.create(
                team_id=str(self.team.pk), user_id=str(self.user.id)
            ),
            query="other conversation",
            final_answer_text="OTHER SESSION ANSWER",
            status="done",
        )
        mine = str(AgentRun.objects.get(session=session).run_id)
        junk = [
            {"prior": "a string"},
            {"prior": ["a", "list"]},
            {"prior": {"summary": 5, "run_ids": "abc"}},
            {"prior": {"summary": ["x"], "run_ids": [5, None, "not-a-uuid", {"a": 1}]}},
            {"prior": {"summary": "", "run_ids": [str(uuid.uuid4())]}},
            {"prior": {"run_ids": [str(foreign.run_id)]}},
            {"prior": {"run_ids": [str(elsewhere.run_id)]}},
            {"prior": {"run_ids": [str(foreign.run_id), mine, str(elsewhere.run_id)]}},
        ]
        expected = [[]] * 7 + [answered(1)]
        for kept, wanted in zip(junk, expected, strict=True):
            with self.subTest(kept=kept):
                run_id, token = self.paused_legacy(session=session, resume_context=kept)
                events = self.decide_to_the_end(run_id, token)
                self.assertEqual(events[-1]["type"], "done")
                sent = shape(self.buckets[-1][0])
                self.assertEqual(sent, wanted + [("user", "make me a task")] + AFTER_THE_PAUSE)
                self.assertNotIn("FOREIGN SECRET", json.dumps(sent))
                self.assertNotIn("OTHER SESSION ANSWER", json.dumps(sent))

    def test_a_recap_alone_is_replayed(self):
        session = self.answered_session(1)
        kept = {"prior": {"summary": "JUST A RECAP", "run_ids": []}}
        run_id, token = self.paused_legacy(session=session, resume_context=kept)
        self.decide_to_the_end(run_id, token)
        self.assertEqual(
            shape(self.first_call(0)),
            [("assistant", RECAP_LINE.format("JUST A RECAP")), ("user", "make me a task")]
            + AFTER_THE_PAUSE,
        )


class KeptPriorLifecycleTests(ResumePriorBase):
    """What is kept on the run: the ids of the turns and the recap, never their text."""

    @WINDOW_2
    def test_the_pause_keeps_the_ids_of_the_window_and_the_recap(self):
        session_id = self.earlier_turns(4)
        run_id, _token = self.pause("the question", session_id)
        by_query = {r.query: str(r.run_id) for r in AgentRun.objects.filter(session_id=session_id)}
        self.assertEqual(
            self.row(run_id).resume_context["prior"],
            {"summary": "RECAP-2", "run_ids": [by_query["q3"], by_query["q4"]]},
        )

    @WINDOW_2
    def test_no_earlier_answer_or_question_is_copied_onto_the_run(self):
        session_id = self.earlier_turns(4)
        run_id, _token = self.pause("the question", session_id)
        kept = json.dumps(self.row(run_id).resume_context)
        for text in ("ANSWER to", "q3", "q4"):
            self.assertNotIn(text, kept)

    def test_a_first_turn_pause_keeps_nothing(self):
        run_id, _token = self.pause("the question", None)
        self.assertIsNone(self.row(run_id).resume_context)

    def test_a_second_turn_plain_pause_keeps_only_the_prior(self):
        session_id = self.earlier_turns(1)
        run_id, _token = self.pause("the question", session_id)
        kept = self.row(run_id).resume_context
        self.assertEqual(kept["prior"]["run_ids"], [str(AgentRun.objects.get(query="q1").run_id)])
        self.assertIsNone(kept["prior"]["summary"])
        self.assertEqual((kept["scope"], kept["scoped"], kept["rest"]), (None, None, None))

    def test_a_turn_that_never_pauses_keeps_nothing(self):
        session_id = self.earlier_turns(2)
        self.plain("the question", session_id)
        self.assertFalse(AgentRun.objects.exclude(resume_context=None).exists())

    def test_cleared_with_the_rest_when_the_run_ends(self):
        session_id = self.earlier_turns(2)
        for decision in ("approve", "reject"):
            run_id, token = self.pause("the question", session_id)
            self.assertIsNotNone(self.row(run_id).resume_context)
            self.decide_to_the_end(run_id, token, decision)
            self.assertIsNone(self.row(run_id).resume_context)

    @WINDOW_2
    def test_never_in_an_events_stream_to_the_browser(self):
        session_id = self.earlier_turns(4)
        payload = {
            "query": "the question",
            "team_id": str(self.team.team_id),
            "session_id": session_id,
        }
        self.model.script[:] = ["write", "answer"]
        raw = b"".join(self.client.post(ASK_URL, payload, format="json").streaming_content).decode()
        run_id = self.last_paused_run_id()
        token = str(self.row(run_id).pending_approval_token)
        resumed = b"".join(self.decide(run_id, token).streaming_content).decode()
        for stream in (raw, resumed):
            self.assertNotIn("RECAP-", stream)
            self.assertNotIn("ANSWER to: q3", stream)

    def last_paused_run_id(self):
        return str(AgentRun.objects.filter(status="awaiting_approval").latest("started_at").run_id)


class ResumeAgentForwardsTests(TestCase):
    def run_resume(self, decision, **kwargs):
        user = self.user
        seen = {}

        def fake_drive(**kw):
            seen["messages"] = copy.deepcopy(kw["messages"])

        AgentStep.objects.filter(run=self.run_row).update(summary=PENDING_APPROVAL_MARKER, error="")
        with mock.patch.object(controller, "_drive_loop", side_effect=fake_drive):
            controller.resume_agent(
                self.run_row,
                decision,
                ToolContext(team_id="t", user_id=str(user.id)),
                lambda event: None,
                **kwargs,
            )
        return shape(seen["messages"])

    def setUp(self):
        from django.contrib.auth import get_user_model  # noqa: PLC0415

        self.user = get_user_model().objects.create_user(
            username="fwd", email="fwd@example.com", password="x"
        )
        self.run_row = AgentRun.objects.create(
            team_id="t", user_id=str(self.user.id), query="the question", status="running"
        )
        AgentStep.objects.create(
            run=self.run_row,
            step_index=0,
            tool_name="create_task",
            arguments_json=dict(PROPOSAL),
            summary=PENDING_APPROVAL_MARKER,
        )

    def test_the_recap_and_turns_go_in_front_of_the_question_in_the_asks_order(self):
        sent = self.run_resume(
            "reject", prior_summary="THE RECAP", prior_turns=[("a", "A"), ("b", "B")]
        )
        self.assertEqual(
            sent,
            [("assistant", RECAP_LINE.format("THE RECAP"))]
            + [("user", "a"), ("assistant", "A"), ("user", "b"), ("assistant", "B")]
            + [("user", "the question")]
            + AFTER_THE_PAUSE,
        )

    def test_absent_it_is_what_it_always_was(self):
        self.assertEqual(self.run_resume("reject"), [("user", "the question")] + AFTER_THE_PAUSE)
        self.assertEqual(
            self.run_resume("reject", prior_summary=None, prior_turns=[]),
            [("user", "the question")] + AFTER_THE_PAUSE,
        )

    def test_it_is_the_same_prefix_run_agent_builds(self):
        seen = {}

        def fake_drive(**kw):
            seen["messages"] = copy.deepcopy(kw["messages"])

        prior = {"prior_turns": [("a", "A")], "prior_summary": "S"}
        with mock.patch.object(controller, "_drive_loop", side_effect=fake_drive):
            controller.run_agent(
                "the question", ToolContext(team_id="t", user_id="u"), lambda e: None, **prior
            )
        asked = shape(seen["messages"])
        self.assertEqual(self.run_resume("approve", **prior)[: len(asked)], asked)


class ResumeContextShapeTests(TestCase):
    def test_a_first_turn_with_no_blocks_keeps_nothing(self):
        self.assertIsNone(
            agent_views._resume_context_for(
                scope=None,
                scoped_extra=None,
                mention_extra=None,
                tier_extra=None,
                prior_summary=None,
                prior_run_ids=[],
            )
        )

    def test_the_prior_is_kept_beside_the_blocks(self):
        kept = agent_views._resume_context_for(
            scope={"kind": "note", "note_type": 1, "note_id": 4},
            scoped_extra="SCOPED",
            mention_extra="MENTIONS",
            tier_extra=None,
            prior_summary="RECAP",
            prior_run_ids=[uuid.UUID(int=1), uuid.UUID(int=2)],
        )
        self.assertEqual(kept["scoped"], "SCOPED")
        self.assertEqual(kept["rest"], "MENTIONS")
        self.assertEqual(
            kept["prior"],
            {"summary": "RECAP", "run_ids": [str(uuid.UUID(int=1)), str(uuid.UUID(int=2))]},
        )
        json.dumps(kept)  # a JSON column

    def test_an_empty_recap_is_not_kept_as_one(self):
        kept = agent_views._resume_context_for(
            scope=None,
            scoped_extra=None,
            mention_extra=None,
            tier_extra=None,
            prior_summary="",
            prior_run_ids=[uuid.UUID(int=1)],
        )
        self.assertIsNone(kept["prior"]["summary"])

    def test_the_old_call_without_the_prior_still_means_what_it_meant(self):
        self.assertEqual(
            agent_views._resume_context_for(
                scope=None, scoped_extra=None, mention_extra="M", tier_extra=None
            ),
            {"scope": None, "scoped": None, "rest": "M"},
        )
