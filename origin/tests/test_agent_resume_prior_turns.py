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
from unittest import mock

from django.conf import settings
from django.test import override_settings

from origin.search_engine.agent import controller, multi_turn
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.types import AgentMessage, FunctionCall
from origin.tests.test_agent_resume_system_extra import (
    ASK_URL,
    PROPOSAL,
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

    def pause(self, query, session_id, **extra):
        """The ask that pauses on the write (the script's next entry must be "write")."""
        return self.ask(query=query, session_id=session_id, **extra)

    # -- reading what a leg was sent -----------------------------------------

    def first_call(self, leg):
        """The messages of the first model call of the `leg`-th leg of the test."""
        return self.buckets[leg][0]


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
