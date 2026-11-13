"""MIP-889: tell the model when the person undid an approved write, behind
`AGENT_TELL_MODEL_UNDO` (default off).

After an undo the next turn's replay still holds the answer that says the write was done. With the
flag on the model gets short factual data (`undone_by_user` in a replayed result; one bracketed
note at the end of the earlier answer in the next ask's conversation). Off, it is sent what
`origin/main` sends. An approval EDIT is NOT told to the model (an earlier version did; dropped:
the write tool's result already carries the applied values), and the edit pins below hold with the
flag ON too.

PINS (off state). The goldens were recorded on PRISTINE `origin/main` (346816f2) with the same
harness (the MIP-977 flag-pin one; the undo is simulated with `undone_at`, which main has):

    git -C <genos-api> worktree add --detach <wt> origin/main
    cp origin/tests/test_agent_tell_model_undo.py origin/tests/test_agent_resume_flag_pins.py \\
       origin/tests/test_agent_resume_system_extra.py origin/tests/test_agent_resume_prior_turns.py \\
       <wt>/origin/tests/
    RECORD_GOLDEN=1 python manage.py test origin.tests.test_agent_tell_model_undo.FlagOffTests --noinput
    # prints `GOLDEN <key> = "<sha256>"`; paste below

Each hash covers every model call of the whole scenario (system instruction and messages).
"""

from __future__ import annotations

import os

from django.conf import settings
from django.test import override_settings
from django.utils import timezone

from origin.search_engine.models import AgentRun, AgentStep
from origin.tests.test_agent_resume_flag_pins import NO_DATE, Recorder
from origin.tests.test_agent_resume_system_extra import ASK_URL, ResumeBase

EDIT = {"title": "Ship it v2", "priority": "High"}
ON = {"AGENT_TELL_MODEL_UNDO": True}

GOLDEN: dict[str, str] = {
    "edit/approve": "d207f4cbcea6328a577943e6772b7996fecf82b89a189482691154d1fcbd4661",
    "edit/twice": "39b0912c7405dd00d79fb4db161bf5882e1cafc1d8e02b65860232e2ded00600",
    "undo/next-turn": "18d9ba20edde859d29fdb86501580c043d1162de90049fb3a1851a478fdccc94",
}


class Base(Recorder, ResumeBase):
    flags: dict = NO_DATE

    def approve_with(self, run_id, token, edited_args=None):
        body = {"run_id": run_id, "approval_token": token, "decision": "approve"}
        if edited_args is not None:
            body["edited_args"] = edited_args
        return self.read(self.client.post("/api/v2/agent/decide/", body, format="json"))

    def responses(self):
        """The tool responses the model was sent, per model call."""
        return [
            [m.function_response for m in msgs if m.role == "tool_response"]
            for _system, msgs in self.calls
        ]

    def step_of(self, run_id):
        return AgentStep.objects.get(run__run_id=run_id, tool_name="create_task")

    def finished_run_then_next_turn(self, *, undone: dict | None):
        """An approved write, its run finished, the write undone (or not), then a plain
        follow-up in the same session. Returns the index of the follow-up's first call."""
        run_id, token = self.ask()
        self.approve_with(run_id, token)
        run = AgentRun.objects.get(run_id=run_id)
        if undone is not None:
            step = self.step_of(run_id)
            step.undone_at = timezone.now()
            fields = ["undone_at"]
            if undone:
                step.undo_outcome_json = undone
                fields.append("undo_outcome_json")
            step.save(update_fields=fields)
        start = len(self.calls)
        payload = {
            "query": "is it still there?",
            "team_id": str(self.team.team_id),
            "session_id": str(run.session_id),
        }
        self.model.script[:] = ["answer"]
        self.read(self.client.post(ASK_URL, payload, format="json"))
        self.assertEqual(len(self.calls) - start, 1)
        return start

    def pin(self, key, since=0):
        if os.environ.get("RECORD_GOLDEN"):
            print(f'GOLDEN {key} = "{self.digest(since)}"')
            return
        self.assertEqual(self.digest(since), GOLDEN[key], f"what the model is sent changed: {key}")


UNDO_OUTCOME = {
    "action": "restored",
    "restored": ["title", "status"],
    "skipped": [{"field": "priority", "reason": "changed_since"}],
}


class FlagOffTests(Base):
    def test_an_approval_with_edits_is_main_s_byte_for_byte(self):
        run_id, token = self.ask()
        self.approve_with(run_id, token, EDIT)
        self.assertEqual(len(self.calls), 2)
        self.pin("edit/approve")
        for response in self.responses()[1]:
            self.assertNotIn("user_edits_applied", response)

    def test_the_replay_of_an_edited_write_on_a_second_pause(self):
        self.model.script[:] = ["write", "write", "answer"]
        run_id, token = self.ask()
        events = self.approve_with(run_id, token, EDIT)
        (again,) = [e for e in events if e["type"] == "tool_call_pending_approval"]
        self.approve_with(run_id, again["approval_token"])
        self.assertEqual(len(self.calls), 3)
        self.pin("edit/twice")

    def test_the_next_turn_after_an_undo_is_main_s_byte_for_byte(self):
        start = self.finished_run_then_next_turn(undone={})
        self.pin("undo/next-turn", since=start)
        self.assertNotIn("undid", repr(self.calls[start]))

    def test_an_outcome_stored_with_the_undo_is_not_shown_either(self):
        start = self.finished_run_then_next_turn(undone=UNDO_OUTCOME)
        self.pin("undo/next-turn", since=start)  # the same bytes as without an outcome


class FlagOnEditsAreMainTests(Base):
    """The edit note was dropped: with the flag ON an edit changes nothing the model is sent."""

    flags = {**NO_DATE, **ON}

    def test_an_approval_with_edits_is_main_s_byte_for_byte_with_the_flag_on(self):
        run_id, token = self.ask()
        self.approve_with(run_id, token, EDIT)
        self.pin("edit/approve")
        for response in self.responses()[1]:
            self.assertNotIn("user_edits_applied", response)

    def test_the_replay_of_an_edited_write_is_main_s_with_the_flag_on(self):
        self.model.script[:] = ["write", "write", "answer"]
        run_id, token = self.ask()
        events = self.approve_with(run_id, token, EDIT)
        (again,) = [e for e in events if e["type"] == "tool_call_pending_approval"]
        self.approve_with(run_id, again["approval_token"])
        self.pin("edit/twice")


class FlagOnUndoTests(Base):
    flags = {**NO_DATE, **ON}

    def test_the_earlier_answer_in_the_next_turn_says_the_write_was_undone(self):
        start = self.finished_run_then_next_turn(undone=UNDO_OUTCOME)
        earlier = [m.text for m in self.calls[start][1] if m.role == "assistant"][0]
        self.assertTrue(earlier.startswith("All done."))
        self.assertIn(
            "[Note: after this answer the person undid create_task (step 1): put back "
            "title, status; not put back because it changed since: priority.]",
            earlier,
        )

    def test_a_write_that_was_not_undone_adds_nothing(self):
        start = self.finished_run_then_next_turn(undone=None)
        earlier = [m.text for m in self.calls[start][1] if m.role == "assistant"][0]
        self.assertEqual(earlier, "All done.")

    def test_an_undo_with_no_stored_outcome_still_says_undone(self):
        start = self.finished_run_then_next_turn(undone={})
        earlier = [m.text for m in self.calls[start][1] if m.role == "assistant"][0]
        self.assertIn("undid create_task (step 1): undone", earlier)

    def test_what_a_create_undo_deleted_is_said_plainly(self):
        start = self.finished_run_then_next_turn(
            undone={"action": "deleted", "restored": [], "skipped": []}
        )
        earlier = [m.text for m in self.calls[start][1] if m.role == "assistant"][0]
        self.assertIn("what it created was deleted", earlier)

    def test_the_replay_of_an_undone_write_inside_its_own_run_carries_the_marker(self):
        from origin.search_engine.agent import controller

        run_id, token = self.ask()
        self.approve_with(run_id, token)
        step = self.step_of(run_id)
        step.undone_at = timezone.now()
        step.undo_outcome_json = UNDO_OUTCOME
        step.save(update_fields=["undone_at", "undo_outcome_json"])
        messages, _pending = controller._rebuild_messages(AgentRun.objects.get(run_id=run_id))
        (response,) = [m.function_response for m in messages if m.role == "tool_response"]
        self.assertEqual(
            response["undone_by_user"],
            {
                "restored": ["title", "status"],
                "not_restored": [{"field": "priority", "reason": "changed_since"}],
            },
        )

    def test_off_means_the_same_rows_say_nothing(self):
        from origin.search_engine.agent import controller

        run_id, token = self.ask()
        self.approve_with(run_id, token)
        step = self.step_of(run_id)
        step.undone_at = timezone.now()
        step.undo_outcome_json = UNDO_OUTCOME
        step.save(update_fields=["undone_at", "undo_outcome_json"])
        with override_settings(
            SEARCH_ENGINE={**settings.SEARCH_ENGINE, **{"AGENT_TELL_MODEL_UNDO": False}}
        ):
            messages, _ = controller._rebuild_messages(AgentRun.objects.get(run_id=run_id))
        (response,) = [m.function_response for m in messages if m.role == "tool_response"]
        self.assertNotIn("undone_by_user", response)


class ApprovalNotesGateTests(Base):
    """Every public function of `approval_notes` is a no-op with the flag off, on its own:
    the callers gate too, so these are the tests that would catch a gate quietly removed."""

    def undone_step(self):
        run_id, token = self.ask()
        self.approve_with(run_id, token)
        step = self.step_of(run_id)
        step.undone_at = timezone.now()
        step.undo_outcome_json = UNDO_OUTCOME
        step.save(update_fields=["undone_at", "undo_outcome_json"])
        step.refresh_from_db()
        return step

    def test_off_every_function_returns_its_input_or_nothing(self):
        from origin.search_engine.agent import approval_notes

        step = self.undone_step()
        result = {"task_id": 1}
        self.assertIs(approval_notes.with_step_notes(result, step), result)
        self.assertIsNone(approval_notes.undone_marker(step))
        self.assertEqual(approval_notes.undo_note_text([step]), "")

    def test_on_each_says_its_part(self):
        from origin.search_engine.agent import approval_notes

        step = self.undone_step()
        with override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, **ON}):
            result = {"task_id": 1}
            self.assertIn("undone_by_user", approval_notes.with_step_notes(result, step))
            self.assertIsNotNone(approval_notes.undone_marker(step))
            self.assertIn("undid create_task", approval_notes.undo_note_text([step]))
            # An edit leaves nothing of itself in the notes.
            self.assertNotIn("user_edits_applied", approval_notes.with_step_notes(result, step))
