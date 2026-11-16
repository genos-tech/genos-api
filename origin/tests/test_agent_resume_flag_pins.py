"""Flag pins for what a RESUMED leg sends the model (MIP-977 and MIP-991).

`AGENT_RESUME_KEEPS_BLOCKS` (977) and `AGENT_RESUME_KEEPS_TURNS` (991) change what the
leg of a run that follows an approve or reject sends the model. Their contract is
"flag off = what `origin/main` sends, byte for byte". This module pins that contract
with sha256 goldens that were recorded on PRISTINE `origin/main` (346816f2, a clean
worktree with none of the change), and pins the flag-on shapes against them.

THE RECORDING. The hashes live in `origin/tests/golden/agent_pins.json` (MIP-1653); they were first
recorded on PRISTINE `origin/main` (346816f2). After a change that is MEANT to move what the model
sees, re-record them all (this module's, the others', the fingerprint golden) with one command, and
compare the diff rather than editing a hash to make a test pass:

    python manage.py record_agent_goldens        # the list of pin modules: origin/tests/agent_pins.py

What is hashed, per shape and decision: EVERY model call of the whole run (the ask leg
and the resumed leg), each as its system instruction (the date line is switched off, ids
are replaced by placeholders) plus its full message list (role, text, call, result).
That is what the provider receives, so a flag that leaks into the off state, in the
system prompt, the messages, or a call that should not exist, changes a hash.

A hash pin on the flag-off state alone is blind to a gate that is half removed (two
places read the flag: the write of `resume_context` at the pause and the read at
`/decide/`). So there are two off-state tests that each defeat one of the gates
(`FlagOffTests.test_a_row_kept_while_the_flag_was_on...` and `...writes_nothing...`).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import re
from unittest import mock

from django.conf import settings
from django.test import override_settings

from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.search_engine import agent_views
from origin.search_engine.agent.mentions import ResolvedMention
from origin.search_engine.models import AgentRun
from origin.tests import agent_pins
from origin.tests.test_agent_resume_prior_turns import ResumePriorBase, shape
from origin.tests.test_agent_resume_system_extra import (
    NOTE_SUMMARY,
    THREAD_SUMMARY,
    ResumeBase,
    _blocks,
)

OFF: dict = {}
ON_977 = {"AGENT_RESUME_KEEPS_BLOCKS": True}
ON_991 = {**ON_977, "AGENT_RESUME_KEEPS_TURNS": True}
NO_DATE = {"AGENT_INJECT_CURRENT_DATE": False}

# sha256 of every model call of one ask + decision, per shape/decision (origin/tests/golden/agent_pins.json,
# re-recorded with `python manage.py record_agent_goldens`).
PINS = agent_pins.Pins(__name__)
GOLDEN = PINS.golden

MENTION = ResolvedMention(
    kind="task", label="Fix login flow", task_id=123, display_id="WRD-5", project_id="77"
)
MENTION_ARGS = [{"type": "task", "task_id": 123, "label": "x"}]


class Recorder:
    """Mixin: a record of every model call (system instruction and messages) of a test,
    the date line off, and the digest of what the provider was sent."""

    def setUp(self):
        super().setUp()
        self.calls: list[tuple[str, list]] = []
        real = self.model.generate_step

        def spy(messages, tools, system_instruction, **kwargs):
            self.calls.append((system_instruction, copy.deepcopy(messages)))
            return real(messages, tools, system_instruction, **kwargs)

        self.model.generate_step = spy

    def make_note(self):
        # A fixed id: it is in the prompt, and a sequence-allocated one would make the
        # hash depend on which tests ran before this one.
        self.note = PersonalNoteMaster.objects.create(
            note_id=4242,
            team=self.team,
            owner=self.user,
            title="Q3 plan",
            body=_blocks("migration first"),
        )
        return {"note_type": 1, "note_id": self.note.note_id}

    @staticmethod
    def normalised(text: str | None) -> str | None:
        """Ids differ per run of the suite; the text around them is the subject."""
        if text is None:
            return None
        return re.sub(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<uuid>", text
        )

    def transcript(self, since: int = 0) -> list[dict]:
        out = []
        for system_instruction, messages in self.calls[since:]:
            out.append(
                {
                    "system": self.normalised(system_instruction),
                    "messages": [
                        json.loads(self.normalised(json.dumps(dataclasses.asdict(m), default=str)))
                        for m in messages
                    ],
                }
            )
        return out

    def digest(self, since: int = 0) -> str:
        blob = json.dumps(self.transcript(since), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def assert_golden(self, key: str, since: int = 0):
        PINS.check(self, key, self.digest(since))


class PinBase(Recorder, ResumeBase):
    """One ask that pauses, and its decision, with the flags chosen per class."""

    flags: dict = NO_DATE

    # -- one ask and its decision, the way a person does it ---------------------

    def ask_kwargs(self, shape: str) -> dict:
        kwargs: dict = {}
        if "thread" in shape:
            kwargs["thread_context"] = self.make_thread()
        if shape == "note":
            kwargs["note_context"] = self.make_note()
        if "mention" in shape:
            kwargs["mentions"] = MENTION_ARGS
        return kwargs

    def run_shape(self, shape: str, decision: str):
        with mock.patch.object(agent_views, "resolve_mentions", return_value=[MENTION]):
            run_id, token = self.ask(**self.ask_kwargs(shape))
            self.decide_to_the_end(run_id, token, decision)
        return run_id


class FlagOffTests(PinBase):
    """Every flag off: the whole run sends what `origin/main` sends."""

    flags = {**PinBase.flags, **OFF}

    def pin(self, shape, decision):
        self.run_shape(shape, decision)
        self.assertEqual(len(self.calls), 2, "the ask leg's call and the resumed leg's")
        self.assert_golden(f"{shape}/{decision}")

    def test_plain_approve(self):
        self.pin("plain", "approve")

    def test_plain_reject(self):
        self.pin("plain", "reject")

    def test_thread_approve(self):
        self.pin("thread", "approve")

    def test_thread_reject(self):
        self.pin("thread", "reject")

    def test_note_approve(self):
        self.pin("note", "approve")

    def test_note_reject(self):
        self.pin("note", "reject")

    def test_mention_approve(self):
        self.pin("mention", "approve")

    def test_mention_reject(self):
        self.pin("mention", "reject")

    def test_thread_and_mention_approve(self):
        self.pin("thread+mention", "approve")

    def test_thread_and_mention_reject(self):
        self.pin("thread+mention", "reject")

    # -- the two gates, each on its own -------------------------------------------

    def test_the_pause_keeps_nothing_with_the_flag_off(self):
        # Defeats a removed WRITE gate: the hash pins alone stay green when the row is
        # written and merely not read.
        with mock.patch.object(agent_views, "resolve_mentions", return_value=[MENTION]):
            run_id, _token = self.ask(**self.ask_kwargs("thread+mention"))
        run = AgentRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertIsNone(run.resume_context)

    def test_a_row_kept_while_the_flag_was_on_is_not_replayed_with_it_off(self):
        # Defeats a removed READ gate: a run paused while the flag was on (so its row
        # holds the blocks) and decided after it was switched off resumes as main does.
        with (
            override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **ON_977}),
            mock.patch.object(agent_views, "resolve_mentions", return_value=[MENTION]),
        ):
            run_id, token = self.ask(**self.ask_kwargs("thread+mention"))
        self.assertIsNotNone(AgentRun.objects.get(run_id=run_id).resume_context)
        self.decide_to_the_end(run_id, token, "approve")
        self.assertEqual(self.legs[1], None, "the resumed leg must carry no system_extra")
        self.assertNotIn(THREAD_SUMMARY, self.calls[1][0])


class FlagOnTests(PinBase):
    """977 on: the resumed leg differs from main, in the one intended way."""

    flags = {**PinBase.flags, **ON_977}

    def resumed_system(self, shape, decision="approve") -> str:
        self.run_shape(shape, decision)
        self.assertEqual(len(self.calls), 2)
        if not agent_pins.record_mode():
            self.assertNotEqual(self.digest(), GOLDEN[f"{shape}/{decision}"])
        return self.calls[1][0]

    def test_the_thread_block_reaches_the_resumed_leg(self):
        system = self.resumed_system("thread")
        self.assertIn(THREAD_SUMMARY, system)
        self.assertEqual(system, self.calls[0][0], "both legs send the same system instruction")

    def test_the_note_block_reaches_the_resumed_leg(self):
        self.assertIn(NOTE_SUMMARY, self.resumed_system("note", "reject"))

    def test_the_references_reach_the_resumed_leg(self):
        self.assertIn("Fix login flow", self.resumed_system("mention"))

    def test_a_plain_ask_is_unchanged_with_the_flag_on(self):
        self.run_shape("plain", "approve")
        if not agent_pins.record_mode():
            self.assertEqual(self.digest(), GOLDEN["plain/approve"])


# ---------------------------------------------------------------------------- #
# MIP-991: the earlier turns of a follow-up ask
# ---------------------------------------------------------------------------- #

FOLLOW_UPS = {"followup": 2, "followup+recap": 4, "followup+thread": 2}
AFTER_THE_PAUSE = [("assistant", "call:create_task"), ("tool_response", "result:create_task")]


class FollowUpBase(Recorder, ResumePriorBase):
    """A follow-up ask (earlier turns, optionally past the window so there is a recap,
    optionally from a thread) that pauses on the write, then its decision."""

    flags: dict = NO_DATE

    def pause_follow_up(self, kind: str):
        """The earlier turns and then the ask that pauses: (run id, approval token)."""
        self.ctx = {"thread_context": self.make_thread()} if kind.endswith("thread") else {}
        window = {
            "SESSION_MAX_PRIOR_TURNS": 2,
            "RAG_SESSION_ROLLING_SUMMARY": kind.endswith("recap"),
        }
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **window}):
            session_id = self.earlier_turns(FOLLOW_UPS[kind], **self.ctx)
            self.since = len(self.calls)  # the ask under test starts here
            return self.pause("the question", session_id, **self.ctx)

    def decide_follow_up(self, run_id, token, decision="approve"):
        self.decide_to_the_end(run_id, token, decision)
        self.assertEqual(len(self.calls) - self.since, 2, "the ask leg's call and the resumed one")

    def run_follow_up(self, kind: str, decision: str = "approve") -> str:
        run_id, token = self.pause_follow_up(kind)
        self.decide_follow_up(run_id, token, decision)
        return run_id

    def legs_messages(self):
        return self.calls[self.since][1], self.calls[self.since + 1][1]


class FollowUpFlagOffTests(FollowUpBase):
    """Both flags off: a follow-up's whole run sends what `origin/main` sends."""

    def pin(self, kind, decision):
        self.run_follow_up(kind, decision)
        self.assert_golden(f"{kind}/{decision}", since=self.since)

    def test_followup_approve(self):
        self.pin("followup", "approve")

    def test_followup_reject(self):
        self.pin("followup", "reject")

    def test_followup_with_a_recap_approve(self):
        self.pin("followup+recap", "approve")

    def test_followup_with_a_recap_reject(self):
        self.pin("followup+recap", "reject")

    def test_followup_from_a_thread_approve(self):
        self.pin("followup+thread", "approve")

    def test_followup_from_a_thread_reject(self):
        self.pin("followup+thread", "reject")

    def test_the_resumed_leg_starts_at_the_question_as_on_main(self):
        # Readable form of what the hashes pin.
        self.run_follow_up("followup+recap")
        _first, resumed = self.legs_messages()
        self.assertEqual(shape(resumed), [("user", "the question"), *AFTER_THE_PAUSE])

    # -- the two gates of 991, each on its own -------------------------------------

    def test_a_pause_keeps_no_earlier_turns_with_both_flags_off(self):
        run_id, _token = self.pause_follow_up("followup+recap")
        run = AgentRun.objects.get(run_id=run_id)
        self.assertEqual(
            run.status, "awaiting_approval"
        )  # checked at the pause: a finish clears it
        self.assertIsNone(run.resume_context)

    def test_a_pause_keeps_no_earlier_turns_with_only_the_977_flag_on(self):
        # Defeats a removed WRITE gate on the turns: the plain follow-up has no block, so
        # with 977 alone nothing at all is kept.
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **ON_977}):
            run_id, _token = self.pause_follow_up("followup+recap")
        run = AgentRun.objects.get(run_id=run_id)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertIsNone(run.resume_context)

    def test_a_row_kept_while_the_turns_flag_was_on_does_not_replay_them_with_it_off(self):
        # Defeats a removed READ gate on the turns: paused with both on, decided with
        # both off, the resumed leg starts at the question as main's does.
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **ON_991}):
            run_id, token = self.pause_follow_up("followup+recap")
        self.assertIn("prior", AgentRun.objects.get(run_id=run_id).resume_context)
        self.decide_follow_up(run_id, token)
        _first, resumed = self.legs_messages()
        self.assertEqual(shape(resumed), [("user", "the question"), *AFTER_THE_PAUSE])

    def test_a_row_kept_with_both_on_is_read_for_the_blocks_only_with_just_977_on(self):
        # Paused with both on, decided with only 977 on: the blocks come back, the
        # earlier turns do not (the turns flag is its own read gate).
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **ON_991}):
            run_id, token = self.pause_follow_up("followup+thread")
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **ON_977}):
            self.decide_follow_up(run_id, token)
        _first, resumed = self.legs_messages()
        self.assertEqual(shape(resumed), [("user", "the question"), *AFTER_THE_PAUSE])
        self.assertIn(THREAD_SUMMARY, self.calls[self.since + 1][0])


class FollowUpFlagOnTests(FollowUpBase):
    """977 alone, and 977 with 991."""

    flags = {**NO_DATE, **ON_977}

    def test_977_alone_leaves_the_conversation_to_main(self):
        # The blocks ride, the turns do not: with only the 977 flag the resumed leg's
        # messages are exactly main's.
        self.run_follow_up("followup+thread")
        first, resumed = self.legs_messages()
        self.assertEqual(shape(resumed), [("user", "the question"), *AFTER_THE_PAUSE])
        self.assertIn(THREAD_SUMMARY, self.calls[self.since + 1][0])
        self.assertNotEqual(shape(first), shape(resumed[:1]))

    def test_both_flags_resume_from_the_first_legs_messages(self):
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **ON_991}):
            self.run_follow_up("followup+recap")
        first, resumed = self.legs_messages()
        self.assertEqual(shape(resumed[: len(first)]), shape(first))
        self.assertEqual(shape(resumed[len(first) :]), AFTER_THE_PAUSE)
        self.assertTrue(shape(first)[0][1].startswith("[Context recap"))

    def test_the_turns_flag_is_inert_without_the_blocks_flag(self):
        with override_settings(
            SEARCH_ENGINE={
                **settings.SEARCH_ENGINE,
                "AGENT_RESUME_KEEPS_BLOCKS": False,
                "AGENT_RESUME_KEEPS_TURNS": True,
            }
        ):
            run_id = self.run_follow_up("followup+recap")
        self.assertIsNone(AgentRun.objects.get(run_id=run_id).resume_context)
        _first, resumed = self.legs_messages()
        self.assertEqual(shape(resumed), [("user", "the question"), *AFTER_THE_PAUSE])
