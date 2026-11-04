"""MIP-977: a run resumed after an approval keeps the context its first leg had.

`/ask/` builds a `system_extra` for the model: the thread or note summary of
"Ask about this thread / note", the USER-PROVIDED REFERENCES block of @/#
mentions, and (at the read tier) the draft-and-offer instruction. `/decide/`
resumed the run with NONE of it: `resume_agent` called `_drive_loop` without
`system_extra`, so after any approve or reject the rest of that run answered
without the thread, the note or the references the person had asked about.

What these tests pin, and why they are shaped as they are:

  * The ask-to-decide round trip runs the REAL code on both legs (the view, the
    loop, the pause, `/decide/`, `resume_agent`). Only the model, the summary
    LLM call and the write tool are faked. A spy on `_drive_loop` records the
    `system_extra` each leg hands the loop, so "the resumed leg sees what the
    first leg saw" is a comparison of two real values, not of two stubs.
    `system_extra`, not the whole system instruction: the date line the loop
    appends moves at midnight and is rebuilt on every leg anyway.
  * Where the fix reaches the model is covered by the spy's value; where the
    context is kept (a column on the run, written at the pause and cleared at
    every terminal state) is covered by the lifecycle tests, because a copy of a
    private summary that outlives the decision it was kept for is the cost of
    this fix.
  * A normal (non-resumed) ask must send exactly what it sent before. The
    golden tests pin the five shapes (plain, thread, note, mention, thread +
    mention) against hashes recorded on `origin/main` BEFORE the change.
  * "Access lost" and "thread deleted" are only meaningful beside the test that
    shows the block IS carried while access is intact: on the unfixed code they
    pass vacuously (there was nothing to leak).

`TransactionTestCase`, as in `test_agent_decide_race`: both legs stream on real
worker threads whose writes must really commit.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
import uuid
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.unified_models import Channel, ChannelMember, Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.search_engine import agent_views, quota, stale_runs
from origin.search_engine.agent import controller
from origin.search_engine.agent.controller import PENDING_APPROVAL_MARKER
from origin.search_engine.agent.mentions import ResolvedMention
from origin.search_engine.agent.note_summary import NoteRecord
from origin.search_engine.agent.tool_tiers import READ_LEVEL_SYSTEM_EXTRA, disabled_tools_for_level
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.llm.types import FunctionCall
from origin.search_engine.models import AgentRun, AgentSession, AgentStep

ASK_URL = "/api/v2/agent/ask/"
DECIDE_URL = "/api/v2/agent/decide/"

THREAD_SUMMARY = "THREAD-SUMMARY: the team settled on Friday for the launch."
NOTE_SUMMARY = "NOTE-SUMMARY: the Q3 plan puts the migration before the redesign."
PROPOSAL = {"title": "Ship it", "project_id": 4, "priority": "Low", "content_text": "draft"}


def _blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class FakeWrite:
    """An approval-gated `create_task` that records what it ran with. It declares
    the REAL tool's schema and description so the loop can declare it to the model."""

    requires_approval = True

    def __init__(self):
        real = REGISTRY["create_task"]
        self.name = "create_task"
        self.description = real.description
        self.parameters_schema = real.parameters_schema
        self.calls = []

    def run(self, args, ctx):
        self.calls.append(dict(args))
        return {"__summary__": "created"}


class ScriptedModel:
    """The model: each call plays the next entry of `script`, "write" proposes the
    approval-gated tool and anything else is the final answer. Records the system
    instruction of every call, which is what the provider's cache keys on."""

    def __init__(self, script):
        self.script = list(script)
        self.system_instructions = []

    def generate_step(self, messages, tools, system_instruction, **kwargs):
        self.system_instructions.append(system_instruction)
        step = self.script.pop(0) if self.script else "answer"
        if step == "write":
            yield (None, FunctionCall(name="create_task", args=dict(PROPOSAL)))
        else:
            yield ("All done.", None)


class ResumeBase(TransactionTestCase):
    script = ("write", "answer")

    def setUp(self):
        super().setUp()
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="asker", email="asker@example.com", password="x"
        )
        # PRO, as in `test_agent_mentions`: the free tier appends the read-level
        # block to every ask, which is a different subject (see `ReadTierTests`).
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        quota.invalidate_effective_tier([self.user.id])
        self.addCleanup(quota.invalidate_effective_tier, [self.user.id])
        self.other = user_model.objects.create_user(
            username="other", email="other@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="T", team_email="t@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        TeamMembers.objects.create(team=self.team, attendee=self.other)
        self.client = APIClient()
        access = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

        self.tool = FakeWrite()
        self.model = ScriptedModel(self.script)
        # The `system_extra` each leg handed the loop, in order: [ask, resume, ...].
        self.legs: list = []

        real_drive = controller._drive_loop

        def spy_drive(**kwargs):
            self.legs.append(kwargs.get("system_extra"))
            return real_drive(**kwargs)

        real_run, real_resume = controller.run_agent, controller.resume_agent

        def closing(real):
            def call(*args, **kwargs):
                try:
                    return real(*args, **kwargs)
                finally:
                    connections.close_all()  # the stream's worker thread: leave nothing behind

            return call

        for patcher in (
            mock.patch.dict(REGISTRY, {"create_task": self.tool}),
            mock.patch.object(controller, "_drive_loop", side_effect=spy_drive),
            mock.patch.object(agent_views, "run_agent", side_effect=closing(real_run)),
            mock.patch.object(agent_views, "resume_agent", side_effect=closing(real_resume)),
            mock.patch.object(controller, "get_model_client", return_value=self.model),
            mock.patch.object(agent_views, "check_remaining", return_value=(True, 0, None)),
            mock.patch.object(agent_views, "disabled_tools_for_user", return_value=set()),
            mock.patch.object(agent_views, "_persisted_disabled_tools", return_value=set()),
            mock.patch.object(controller, "_operator_disabled_tools", return_value=set()),
            mock.patch(
                "origin.search_engine.agent.thread_summary.summarise_thread",
                return_value=(THREAD_SUMMARY, "test:model"),
            ),
            mock.patch(
                "origin.search_engine.agent.note_summary.summarise_note",
                return_value=(NOTE_SUMMARY, "test:model"),
            ),
            mock.patch(
                "origin.search_engine.ingestion.ingest_conversation_run", return_value=False
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # -- fixtures -----------------------------------------------------------

    def make_thread(self):
        self.channel = Channel.objects.create(team=self.team, kind=2, title="launch")
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        self.root = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body=_blocks("when do we launch?"),
            body_text="when do we launch?",
        )
        Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=2,
            body=_blocks("Friday"),
            body_text="Friday",
            parent=self.root,
            thread_root=self.root,
            is_thread_reply=True,
        )
        return {
            "chat_type": 2,
            "chat_id": str(self.channel.id),
            "thread_id": str(self.root.id),
        }

    def make_note(self):
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Q3 plan", body=_blocks("migration first")
        )
        return {"note_type": 1, "note_id": self.note.note_id}

    # -- requests -----------------------------------------------------------

    @staticmethod
    def read(resp):
        assert resp.status_code == 200, resp.content
        text = b"".join(resp.streaming_content).decode()
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def ask(self, **extra):
        payload = {"query": "do the thing", "team_id": str(self.team.team_id), **extra}
        events = self.read(self.client.post(ASK_URL, payload, format="json"))
        pending = [e for e in events if e["type"] == "tool_call_pending_approval"]
        self.assertEqual(len(pending), 1, events)
        return pending[0]["run_id"], pending[0]["approval_token"]

    def decide(self, run_id, token, decision="approve"):
        body = {"run_id": run_id, "approval_token": token, "decision": decision}
        return self.client.post(DECIDE_URL, body, format="json")

    def decide_to_the_end(self, run_id, token, decision="approve"):
        events = self.read(self.decide(run_id, token, decision))
        return events

    def row(self, run_id):
        return AgentRun.objects.get(run_id=run_id)


class ResumeKeepsContextTests(ResumeBase):
    """The loss itself: what the resumed leg is handed, against the first leg's."""

    def check(self, decision, **ask_context):
        run_id, token = self.ask(**ask_context)
        self.decide_to_the_end(run_id, token, decision)
        self.assertEqual(len(self.legs), 2, "the ask leg and the resumed leg")
        first, resumed = self.legs
        self.assertIsNotNone(first, "the ask leg must have carried a context to keep")
        self.assertEqual(resumed, first)
        return first

    def test_thread_ask_approve(self):
        extra = self.check("approve", thread_context=self.make_thread())
        self.assertIn("<thread_summary>", extra)
        self.assertIn(THREAD_SUMMARY, extra)
        self.assertEqual(self.tool.calls, [PROPOSAL])

    def test_thread_ask_reject(self):
        extra = self.check("reject", thread_context=self.make_thread())
        self.assertIn(THREAD_SUMMARY, extra)
        self.assertEqual(self.tool.calls, [])

    def test_note_ask_approve(self):
        extra = self.check("approve", note_context=self.make_note())
        self.assertIn("<note_summary>", extra)
        self.assertIn(NOTE_SUMMARY, extra)
        self.assertIn('titled "Q3 plan"', extra)

    def test_note_ask_reject(self):
        extra = self.check("reject", note_context=self.make_note())
        self.assertIn(NOTE_SUMMARY, extra)

    def test_mentions_block_is_kept_too(self):
        # The task text names the thread and note summaries; the loss was wider.
        # The references block went the same way (only `ctx.resolved_mentions`,
        # the search boost, was rehydrated).
        mention = ResolvedMention(
            kind="task", label="Fix login flow", task_id=123, display_id="WRD-5", project_id="77"
        )
        with mock.patch.object(agent_views, "resolve_mentions", return_value=[mention]):
            extra = self.check(
                "approve", mentions=[{"type": "task", "task_id": 123, "label": "x"}]
            )
        self.assertIn("USER-PROVIDED REFERENCES", extra)
        self.assertIn("Fix login flow", extra)

    def test_thread_ask_with_mentions_keeps_both_in_the_same_order(self):
        mention = ResolvedMention(
            kind="task", label="Fix login flow", task_id=123, display_id="WRD-5", project_id="77"
        )
        with mock.patch.object(agent_views, "resolve_mentions", return_value=[mention]):
            extra = self.check(
                "approve",
                thread_context=self.make_thread(),
                mentions=[{"type": "task", "task_id": 123}],
            )
        self.assertLess(extra.index("<thread_summary>"), extra.index("USER-PROVIDED REFERENCES"))

    def test_a_plain_ask_resumes_exactly_as_it_always_did(self):
        run_id, token = self.ask()
        self.decide_to_the_end(run_id, token)
        self.assertEqual(self.legs, [None, None])

    def test_the_tier_block_is_the_one_the_first_leg_had(self):
        # A run that pauses is never at the read tier (every approval-gated tool
        # is hidden there: `ReadTierTests`), so in practice the block is absent
        # on both legs. Forced here to show WHICH rule a present block follows:
        # as at the original leg, not re-read at the decision.
        with mock.patch.object(agent_views, "tier_system_extra", return_value="TIER-BLOCK"):
            run_id, token = self.ask(thread_context=self.make_thread())
        with mock.patch.object(agent_views, "tier_system_extra", return_value=None):
            self.decide_to_the_end(run_id, token)
        first, resumed = self.legs
        self.assertTrue(first.endswith("TIER-BLOCK"))
        self.assertEqual(resumed, first)

    def test_a_second_pause_keeps_the_context_for_the_next_leg(self):
        self.model.script[:] = ["write", "write", "answer"]
        run_id, token = self.ask(thread_context=self.make_thread())
        events = self.decide_to_the_end(run_id, token)
        (again,) = [e for e in events if e["type"] == "tool_call_pending_approval"]
        # Paused a second time: a decision is still owed, so the context is too.
        self.assertIn(THREAD_SUMMARY, self.row(run_id).resume_context["scoped"])
        self.decide_to_the_end(run_id, again["approval_token"])
        self.assertEqual(len(self.legs), 3)
        self.assertEqual(self.legs[1], self.legs[0])
        self.assertEqual(self.legs[2], self.legs[0])

    def test_both_legs_send_the_provider_the_same_system_instruction(self):
        # What the provider's prompt cache keys on (Gemini digests the whole
        # string; OpenAI/Gemini implicit caching share the longest common prefix).
        # Today the resumed leg's string is the plain ask's; after the fix it is
        # the first leg's, byte for byte (same day, same locale).
        self.ask_and_approve(thread_context=self.make_thread())
        first, resumed = self.model.system_instructions
        self.assertEqual(resumed, first)
        self.assertIn(THREAD_SUMMARY, resumed)

    def ask_and_approve(self, **ask_context):
        run_id, token = self.ask(**ask_context)
        self.decide_to_the_end(run_id, token)


class AccessChangesBetweenPauseAndDecisionTests(ResumeBase):
    """The resume re-checks the access the ask checked, and only ever degrades."""

    def test_a_thread_the_person_left_is_not_put_back_in_the_prompt(self):
        run_id, token = self.ask(thread_context=self.make_thread())
        ChannelMember.objects.filter(channel=self.channel, user=self.user).delete()
        self.decide_to_the_end(run_id, token)  # 200, never a 4xx after the token went
        first, resumed = self.legs
        self.assertIn(THREAD_SUMMARY, first)
        self.assertIsNone(resumed)
        self.assertEqual(self.tool.calls, [PROPOSAL], "the approved write still ran")
        # Not in the model's prompt on the resumed call either.
        self.assertNotIn(THREAD_SUMMARY, self.model.system_instructions[1])

    def test_a_deleted_thread_is_not_put_back_in_the_prompt(self):
        run_id, token = self.ask(thread_context=self.make_thread())
        self.root.delete()
        self.decide_to_the_end(run_id, token)
        self.assertIsNone(self.legs[1])

    def test_a_deleted_channel_is_not_put_back_in_the_prompt(self):
        run_id, token = self.ask(thread_context=self.make_thread())
        Channel.objects.filter(id=self.channel.id).update(is_deleted=True)
        self.decide_to_the_end(run_id, token)
        self.assertIsNone(self.legs[1])

    def test_a_note_the_person_can_no_longer_read_is_not_put_back(self):
        run_id, token = self.ask(note_context=self.make_note())
        PersonalNoteMaster.objects.filter(note_id=self.note.note_id).update(owner=self.other)
        self.decide_to_the_end(run_id, token, "reject")
        first, resumed = self.legs
        self.assertIn(NOTE_SUMMARY, first)
        self.assertIsNone(resumed)

    def test_a_deleted_note_is_not_put_back(self):
        run_id, token = self.ask(note_context=self.make_note())
        self.note.delete()
        self.decide_to_the_end(run_id, token)
        self.assertIsNone(self.legs[1])

    def test_a_note_emptied_meanwhile_is_not_put_back(self):
        # The ask refuses an empty note ("nothing to summarise yet"); so does the resume.
        run_id, token = self.ask(note_context=self.make_note())
        PersonalNoteMaster.objects.filter(note_id=self.note.note_id).update(title="", body=[])
        self.decide_to_the_end(run_id, token)
        self.assertIsNone(self.legs[1])

    def test_the_summary_is_what_the_first_leg_saw_not_a_fresh_one(self):
        # The approved write may be an edit of the very note asked about, and a
        # thread gets new messages while a card waits: a regenerated summary would
        # differ (and cost an LLM call inside /decide/). The kept text does not.
        run_id, token = self.ask(note_context=self.make_note())
        PersonalNoteMaster.objects.filter(note_id=self.note.note_id).update(
            body=_blocks("completely different now")
        )
        with mock.patch(
            "origin.search_engine.agent.note_summary.summarise_note",
            side_effect=AssertionError("the resume must not summarise"),
        ):
            self.decide_to_the_end(run_id, token)
        self.assertEqual(self.legs[1], self.legs[0])

    def test_mentions_are_not_rechecked_only_the_scope_is(self):
        # Decision, pinned: the references block holds only titles and ids the
        # run row already persists (`mentions`), and every read of the item goes
        # through a tool that checks access itself. The scoped summary is the
        # private CONTENT, and the only part re-checked.
        mention = ResolvedMention(
            kind="task", label="Fix login flow", task_id=123, display_id="WRD-5", project_id="77"
        )
        with mock.patch.object(agent_views, "resolve_mentions", return_value=[mention]):
            run_id, token = self.ask(
                thread_context=self.make_thread(), mentions=[{"type": "task", "task_id": 123}]
            )
        ChannelMember.objects.filter(channel=self.channel, user=self.user).delete()
        self.decide_to_the_end(run_id, token)
        resumed = self.legs[1]
        self.assertIn("USER-PROVIDED REFERENCES", resumed)
        self.assertNotIn(THREAD_SUMMARY, resumed)
        self.assertNotIn("<thread_summary>", resumed)

    def test_an_error_in_the_recheck_degrades_to_resuming_as_today(self):
        run_id, token = self.ask(thread_context=self.make_thread())
        with mock.patch.object(
            agent_views, "fetch_thread_messages_for_agent", side_effect=RuntimeError("db down")
        ):
            events = self.decide_to_the_end(run_id, token)
        self.assertIn("done", [e["type"] for e in events])
        self.assertIsNone(self.legs[1])


class LegacyRunsTests(ResumeBase):
    """A run paused before this shipped has nothing kept, and resumes as before."""

    script = ("answer",)  # no ask leg ran: the first model call IS the resumed leg

    def paused_without_context(self, **fields):
        token = uuid.uuid4()
        run = AgentRun.objects.create(
            team_id=str(self.team.pk),
            user_id=str(self.user.id),
            query="make me a task",
            status="awaiting_approval",
            pending_approval_token=token,
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

    def test_nothing_kept_resumes_with_no_extra(self):
        run_id, token = self.paused_without_context()
        events = self.decide_to_the_end(run_id, token)
        self.assertEqual(self.legs, [None])
        self.assertIn("done", [e["type"] for e in events])

    def test_a_legacy_thread_run_is_not_given_a_block_it_never_had(self):
        # Even when its session says it was a thread ask: regenerating "from the
        # scope" would hand a legacy run a block its first leg did not have.
        thread = self.make_thread()
        session = AgentSession.objects.create(
            team_id=str(self.team.pk),
            user_id=str(self.user.id),
            chat_type=thread["chat_type"],
            chat_id=thread["chat_id"],
            thread_id=thread["thread_id"],
        )
        run_id, token = self.paused_without_context(session=session)
        self.decide_to_the_end(run_id, token)
        self.assertEqual(self.legs, [None])

    def test_a_malformed_kept_value_resumes_as_today_and_never_crashes(self):
        for junk in ("a string", ["a", "list"], {"scoped": 5, "rest": ["x"]}, {"scope": "x"}):
            with self.subTest(junk=junk):
                self.legs.clear()
                run_id, token = self.paused_without_context(resume_context=junk)
                events = self.decide_to_the_end(run_id, token)
                self.assertIn("done", [e["type"] for e in events])
                self.assertIsNone(self.legs[0])


class KeptContextLifecycleTests(ResumeBase):
    """The kept copy lives while a decision is owed, and goes with the run."""

    def test_kept_at_the_pause_and_not_before(self):
        run_id, token = self.ask(thread_context=self.make_thread())
        kept = self.row(run_id).resume_context
        self.assertEqual(kept["scope"]["kind"], "thread")
        self.assertEqual(kept["scope"]["chat_id"], str(self.channel.id))
        self.assertIn(THREAD_SUMMARY, kept["scoped"])

    def test_an_ask_that_never_pauses_keeps_nothing(self):
        self.model.script[:] = ["answer"]
        payload = {
            "query": "just answer",
            "team_id": str(self.team.team_id),
            "thread_context": self.make_thread(),
        }
        events = self.read(self.client.post(ASK_URL, payload, format="json"))
        self.assertIn("done", [e["type"] for e in events])
        (run,) = AgentRun.objects.filter(user_id=str(self.user.id))
        self.assertEqual(run.status, "done")
        self.assertIsNone(run.resume_context)

    def test_a_plain_pause_keeps_nothing(self):
        run_id, token = self.ask()
        self.assertIsNone(self.row(run_id).resume_context)

    def test_cleared_when_an_approved_run_finishes(self):
        run_id, token = self.ask(thread_context=self.make_thread())
        self.decide_to_the_end(run_id, token, "approve")
        row = self.row(run_id)
        self.assertEqual(row.status, "done")
        self.assertIsNone(row.resume_context)

    def test_cleared_when_a_rejected_run_finishes(self):
        run_id, token = self.ask(note_context=self.make_note())
        self.decide_to_the_end(run_id, token, "reject")
        row = self.row(run_id)
        self.assertIn(row.status, ("rejected", "done"))
        self.assertIsNone(row.resume_context)

    def test_cleared_when_the_resumed_leg_errors(self):
        run_id, token = self.ask(thread_context=self.make_thread())
        self.model.script[:] = []
        with mock.patch.object(
            self.model, "generate_step", side_effect=RuntimeError("provider down")
        ):
            self.decide_to_the_end(run_id, token)
        row = self.row(run_id)
        self.assertEqual(row.status, "error")
        self.assertIsNone(row.resume_context)

    def test_the_reaper_clears_what_a_dead_resume_left_behind(self):
        # A resumed run whose worker died stays `running`; the reaper closes it
        # with a queryset UPDATE, which no stream close-out ever sees.
        run = AgentRun.objects.create(
            team_id=str(self.team.pk),
            user_id=str(self.user.id),
            query="q",
            status="running",
            resume_context={"scope": None, "scoped": THREAD_SUMMARY, "rest": None},
        )
        AgentRun.objects.filter(run_id=run.run_id).update(
            started_at=timezone.now() - timedelta(hours=stale_runs.STALE_RUN_HOURS + 1)
        )
        call_command("reap_stale_agent_runs", stdout=StringIO())
        run.refresh_from_db()
        self.assertEqual(run.status, "error")
        self.assertIsNone(run.resume_context)

    def test_deleting_the_conversation_erases_it(self):
        run_id, token = self.ask(thread_context=self.make_thread())
        run = self.row(run_id)
        self.assertIsNotNone(run.resume_context)
        agent_views._delete_agent_conversations(
            user_id=str(self.user.id), session_id=run.session_id
        )
        self.assertFalse(AgentRun.objects.filter(run_id=run_id).exists())
        # ...and a late /decide/ for it is a 404, with nothing resumed.
        self.assertEqual(self.decide(run_id, token).status_code, 404)

    def test_deleting_all_conversations_erases_it(self):
        run_id, _token = self.ask(note_context=self.make_note())
        agent_views._delete_agent_conversations(
            user_id=str(self.user.id), team_id=str(self.team.pk)
        )
        self.assertFalse(AgentRun.objects.filter(run_id=run_id).exists())

    def test_deleting_the_account_erases_it(self):
        from origin.services.account_deletion import _purge_search_engine_rows  # noqa: PLC0415

        run_id, _token = self.ask(thread_context=self.make_thread())
        self.assertIsNotNone(self.row(run_id).resume_context)
        _purge_search_engine_rows(str(self.user.id))
        self.assertFalse(AgentRun.objects.filter(run_id=run_id).exists())

    def test_retention_leaves_a_paused_run_alone_like_its_question_and_answer(self):
        # Stated, not hidden: `agent_runs` retention never takes an
        # `awaiting_approval` run (the person is about to decide), so an
        # abandoned card keeps its copy exactly as long as it keeps `query`.
        from origin.services import retention  # noqa: PLC0415

        run_id, _token = self.ask(thread_context=self.make_thread())
        policy = retention.BY_NAME["agent_runs"]
        eligible = policy.queryset().filter(policy.eligible())
        self.assertFalse(eligible.filter(run_id=run_id).exists())

    def test_never_in_an_events_stream_to_the_browser(self):
        # The kept text goes nowhere but the model's prompt.
        payload = {
            "query": "do the thing",
            "team_id": str(self.team.team_id),
            "thread_context": self.make_thread(),
        }
        resp = self.client.post(ASK_URL, payload, format="json")
        raw = b"".join(resp.streaming_content).decode()
        self.assertNotIn(THREAD_SUMMARY, raw)


class _Run:
    """The few things `_stream_ndjson` reads and writes on a run, no DB."""

    def __init__(self, resume_context=None):
        self.run_id = "r"
        self.status = "running"
        self.final_answer_text = ""
        self.error_message = ""
        self.started_at = timezone.now()
        self.finished_at = None
        self.pending_approval_token = None
        self.session_id = None
        self.user_id = "u"
        self.team_id = "t"
        self.query = "q"
        self.resume_context = resume_context
        self.saves = []

    def save(self, update_fields=None):
        self.saves.append(list(update_fields or []))


class StreamCloseOutTests(SimpleTestCase):
    """`_close_run`'s three exits, without a DB: what each does to the kept copy."""

    KEPT = {"scope": None, "scoped": "S", "rest": None}

    def setUp(self):
        for target in (
            "origin.search_engine.agent_views._push_run_complete",
            "origin.search_engine.ingestion.ingest_conversation_run",
        ):
            patcher = mock.patch(target, autospec=True)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_pause_writes_the_context_it_was_given(self):
        def worker(emit, cancel_event):
            return {"paused": True, "approval_token": uuid.uuid4()}

        run = _Run()
        list(agent_views._stream_ndjson(worker, run=run, resume_context=self.KEPT))
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(run.resume_context, self.KEPT)
        self.assertIn("resume_context", run.saves[-1])

    def test_a_pause_with_nothing_given_leaves_what_is_kept(self):
        # The resumed leg pausing again: /decide/ passes no context, and the
        # copy the first pause wrote must survive for the next leg.
        def worker(emit, cancel_event):
            return {"paused": True, "approval_token": uuid.uuid4()}

        run = _Run(resume_context=self.KEPT)
        list(agent_views._stream_ndjson(worker, run=run))
        self.assertEqual(run.resume_context, self.KEPT)
        self.assertNotIn("resume_context", run.saves[-1])

    def test_a_finish_clears_it(self):
        def worker(emit, cancel_event):
            emit({"type": "answer_delta", "text": "hi"})
            emit({"type": "done"})

        run = _Run(resume_context=self.KEPT)
        list(agent_views._stream_ndjson(worker, run=run))
        self.assertIsNone(run.resume_context)
        self.assertIn("resume_context", run.saves[-1])

    def test_an_error_clears_it(self):
        def worker(emit, cancel_event):
            emit({"type": "error", "message": "boom"})

        run = _Run(resume_context=self.KEPT)
        list(agent_views._stream_ndjson(worker, run=run))
        self.assertEqual(run.status, "error")
        self.assertIsNone(run.resume_context)

    def test_a_disconnect_clears_it(self):
        started, release = threading.Event(), threading.Event()

        def worker(emit, cancel_event):
            emit({"type": "answer_delta", "text": "partial"})
            started.set()
            release.wait(timeout=5)

        run = _Run(resume_context=self.KEPT)
        stream = agent_views._stream_ndjson(worker, run=run)
        next(stream)
        self.assertTrue(started.wait(timeout=5))
        stream.close()
        release.set()
        self.assertEqual(run.status, "cancelled")
        self.assertIsNone(run.resume_context)
        self.assertIn("resume_context", run.saves[-1])


class ResumeContextShapeTests(TestCase):
    """The round trip of what is kept: pieces in, the first leg's string out."""

    SCOPED, MENTIONS, TIER = "SCOPED-BLOCK", "MENTION-BLOCK", "TIER-BLOCK"

    def original(self, scoped, mentions, tier):
        """What `/ask/` has always sent: the present blocks, in this order, blank-line joined."""
        return "\n\n".join(b for b in (scoped, mentions, tier) if b) or None

    def test_every_combination_of_blocks_comes_back_byte_for_byte(self):
        scope = {"kind": "note", "note_type": 1, "note_id": 9}
        for has in itertools.product((False, True), repeat=3):
            scoped, mentions, tier = (
                b if present else None
                for b, present in zip((self.SCOPED, self.MENTIONS, self.TIER), has)
            )
            with self.subTest(scoped=bool(scoped), mentions=bool(mentions), tier=bool(tier)):
                kept = agent_views._resume_context_for(
                    scope=scope if scoped else None,
                    scoped_extra=scoped,
                    mention_extra=mentions,
                    tier_extra=tier,
                )
                run = AgentRun(resume_context=kept)
                with mock.patch.object(agent_views, "_scope_still_readable", return_value=True):
                    self.assertEqual(
                        agent_views._resumed_system_extra(run), self.original(scoped, mentions, tier)
                    )

    def test_nothing_to_keep_is_nothing_stored(self):
        self.assertIsNone(
            agent_views._resume_context_for(
                scope=None, scoped_extra=None, mention_extra=None, tier_extra=None
            )
        )

    def test_the_scope_is_stored_only_beside_a_scoped_block(self):
        kept = agent_views._resume_context_for(
            scope={"kind": "thread"},
            scoped_extra=None,
            mention_extra=self.MENTIONS,
            tier_extra=None,
        )
        self.assertIsNone(kept["scope"])
        self.assertIsNone(kept["scoped"])

    def test_no_scope_stored_with_a_scoped_block_drops_it(self):
        # Cannot be re-checked, so it is not trusted: the rest still resumes.
        run = AgentRun(resume_context={"scope": None, "scoped": self.SCOPED, "rest": "REST"})
        self.assertEqual(agent_views._resumed_system_extra(run), "REST")

    def test_an_unknown_scope_kind_drops_the_scoped_block(self):
        run = AgentRun(
            resume_context={"scope": {"kind": "project"}, "scoped": self.SCOPED, "rest": None}
        )
        self.assertIsNone(agent_views._resumed_system_extra(run))


class ReadTierTests(TestCase):
    def test_every_approval_gated_tool_is_hidden_at_the_read_tier(self):
        # The proof behind "the tier block is as at the first leg": a run that
        # paused on a write cannot have been asked at the read tier, so its first
        # leg never carried READ_LEVEL_SYSTEM_EXTRA, and "as then" and "as now"
        # only differ for a person downgraded to read between pause and decision
        # (whose pending write is vetoed by the recomputed tool gate).
        gated = {t.name for t in REGISTRY.values() if t.requires_approval}
        self.assertTrue(gated)
        self.assertEqual(gated - disabled_tools_for_level("read"), set())


class NormalAskUnchangedTests(TestCase):
    """What a normal (non-resumed) ask hands the model, byte for byte.

    The hashes were recorded on `origin/main` (a89531c1) BEFORE this change, over
    inputs this test fixes (ids, summaries, mention). The ask path is only read,
    never rewritten, by the fix; these fail if that stops being true.
    """

    CHAT = "00000000-0000-0000-0000-00000000c0de"
    THREAD = "00000000-0000-0000-0000-0000000070ad"
    GOLDEN = {
        "thread": "32acfcf83b77cac4f3c31dad72b175b866810ab0cb3c3e7b6d8738604373f21a",
        "note": "bea024d6c0c06bac8dc3ebff6bb83f062b4ce597ffdc17cff5add22256da6ebb",
        "mention": "85d377367310a2917d0be64ef91a617323a8ea033fe5279e9e8f030dcb4d0b7f",
        "thread+mention": "35a4e6a3995cbb3e5ba314b3fbf04da3060dd9f25d844587cf6b35a552bb7ccf",
    }

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="golden", email="golden@example.com", password="x"
        )
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        quota.invalidate_effective_tier([self.user.id])
        self.addCleanup(quota.invalidate_effective_tier, [self.user.id])
        self.team = TeamMaster.objects.create(
            team_name="T", team_email="t@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        self.client = APIClient()
        access = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def post(self, **extra):
        captured = {}

        def fake_stream(worker, **kwargs):
            captured["worker"] = worker
            return iter([b""])

        def fake_run_agent(query, ctx, emit, **kwargs):
            captured["kwargs"] = kwargs
            emit({"type": "done"})

        record = NoteRecord(
            note_type=1,
            note_id=4242,
            team_id="t",
            title="Q3 plan",
            body_text="b",
            ts_updated=None,
            owner_id="o",
        )
        mention = ResolvedMention(
            kind="task", label="Fix login flow", task_id=123, display_id="WRD-5", project_id="77"
        )
        with (
            mock.patch.object(agent_views, "_stream_ndjson", side_effect=fake_stream),
            mock.patch.object(agent_views, "run_agent", side_effect=fake_run_agent),
            mock.patch.object(agent_views, "check_remaining", return_value=(True, 0, None)),
            mock.patch.object(agent_views, "load_or_generate_for_ask", return_value="THREAD SUMMARY"),
            mock.patch.object(
                agent_views, "load_or_generate_note_for_ask", return_value=("NOTE SUMMARY", record)
            ),
            mock.patch.object(agent_views, "resolve_mentions", return_value=[mention]),
        ):
            payload = {"query": "q", "team_id": str(self.team.team_id), **extra}
            resp = self.client.post(ASK_URL, payload, format="json")
            self.assertEqual(resp.status_code, 200)
            captured["worker"](lambda event: None, threading.Event())
        return captured["kwargs"]["system_extra"]

    @staticmethod
    def digest(text):
        return hashlib.sha256(text.encode()).hexdigest()

    def thread_context(self):
        return {"chat_type": 2, "chat_id": self.CHAT, "thread_id": self.THREAD}

    def test_a_plain_ask_sends_no_extra(self):
        self.assertIsNone(self.post())

    def test_a_thread_ask(self):
        self.assertEqual(
            self.digest(self.post(thread_context=self.thread_context())), self.GOLDEN["thread"]
        )

    def test_a_note_ask(self):
        extra = self.post(note_context={"note_type": 1, "note_id": 4242})
        self.assertEqual(self.digest(extra), self.GOLDEN["note"])

    def test_a_mention_ask(self):
        extra = self.post(mentions=[{"type": "task", "task_id": 123}])
        self.assertEqual(self.digest(extra), self.GOLDEN["mention"])

    def test_a_thread_ask_with_a_mention(self):
        extra = self.post(
            thread_context=self.thread_context(), mentions=[{"type": "task", "task_id": 123}]
        )
        self.assertEqual(self.digest(extra), self.GOLDEN["thread+mention"])

    def test_a_free_tier_ask_ends_in_the_read_block(self):
        self.user.tier = "free"
        self.user.save(update_fields=["tier"])
        quota.invalidate_effective_tier([self.user.id])
        self.assertEqual(self.post(), READ_LEVEL_SYSTEM_EXTRA)
        extra = self.post(thread_context=self.thread_context())
        self.assertTrue(extra.endswith("\n\n" + READ_LEVEL_SYSTEM_EXTRA))
        self.assertEqual(
            self.digest(extra[: -len("\n\n" + READ_LEVEL_SYSTEM_EXTRA)]), self.GOLDEN["thread"]
        )


class ResumeAgentForwardsTests(TestCase):
    def test_resume_agent_hands_system_extra_to_the_loop(self):
        user = get_user_model().objects.create_user(
            username="fwd", email="fwd@example.com", password="x"
        )
        run = AgentRun.objects.create(
            team_id="t", user_id=str(user.id), query="q", status="running"
        )
        AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name="create_task",
            arguments_json=dict(PROPOSAL),
            summary=PENDING_APPROVAL_MARKER,
        )
        seen = {}

        def fake_drive(**kwargs):
            seen.update(kwargs)

        with mock.patch.object(controller, "_drive_loop", side_effect=fake_drive):
            controller.resume_agent(
                run,
                "reject",
                ToolContext(team_id="t", user_id=str(user.id)),
                lambda event: None,
                system_extra="EXTRA",
            )
        self.assertEqual(seen["system_extra"], "EXTRA")
        # And absent, as every caller before this: None, as it always was.
        seen.clear()
        AgentStep.objects.filter(run=run).update(summary=PENDING_APPROVAL_MARKER, error="")
        with mock.patch.object(controller, "_drive_loop", side_effect=fake_drive):
            controller.resume_agent(
                run,
                "reject",
                ToolContext(team_id="t", user_id=str(user.id)),
                lambda event: None,
            )
        self.assertIsNone(seen["system_extra"])
