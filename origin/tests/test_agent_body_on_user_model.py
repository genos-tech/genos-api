"""MIP-905: AGENT_BODY_ON_USER_MODEL, the member's model writes the body of a write.

On the Medium two-tier loop the cheap planning model chooses the tools, and a planning call that
proposes a write was executed as it came back, so the body the member reads and approves came
from the cheap model. With the flag ON, a planning call that proposes a write carrying a body is
discarded and that step runs again on the member's chosen model (as the final text answer
already is). Flag OFF: exactly what origin/main does, pinned by the `two-tier-write` golden of
`test_agent_content_length_pins` (also asserted here).
"""

from __future__ import annotations

from unittest import mock

from origin.search_engine.agent import controller
from origin.search_engine.llm.choice import LlmChoice
from origin.search_engine.llm.types import FunctionCall
from origin.tests.test_agent_content_length_pins import (
    GOLDEN,
    SCENARIOS,
    TWO_TIER,
    LoopBase,
    call,
    digest_of,
)

CHEAP_BODY = "### Summary\nDo the thing."
BETTER_BODY = "### Summary\nDo the thing, and say why.\n\n### Acceptance criteria\n- shipped\n"
MEDIUM = LlmChoice(provider="gemini", model="gemini-3.6-flash", effort="medium")
ON = {**TWO_TIER, "AGENT_BODY_ON_USER_MODEL": True}
OFF = dict(TWO_TIER)


def write(name="create_task", **args):
    return [(None, FunctionCall(name=name, args=dict(args)))]


class BodyOnUserModelTests(LoopBase):
    def go(self, script, *, flags, fake_writes=("create_task",), choice=MEDIUM):
        self.purposes = []
        real = controller._persist_llm_call

        def spy(run_id, team_id, **kwargs):
            self.purposes.append(kwargs.get("purpose"))
            return real(run_id, team_id, **kwargs)

        with mock.patch.object(controller, "_persist_llm_call", side_effect=spy):
            client = self.run_loop(script, flags=flags, choice=choice, fake_writes=fake_writes)
        return client

    def pending(self):
        return [e for e in self.events if e.get("type") == "tool_call_pending_approval"]

    # -- the flag off is main --------------------------------------------------------------

    def test_off_the_planning_models_proposal_goes_to_approval_as_it_came_back(self):
        client = self.go([write(title="T", project_id=1, content_text=CHEAP_BODY)], flags=OFF)
        self.assertEqual(len(client.calls), 1)
        self.assertIsNotNone(client.calls[0]["model_override"])
        self.assertEqual(self.pending()[0]["arguments"]["content_text"], CHEAP_BODY)
        self.assertEqual(self.purposes, ["planning"])

    def test_off_equals_the_golden_recorded_on_main(self):
        self.assertEqual(
            digest_of(SCENARIOS["two-tier-write"](self).calls), GOLDEN["two-tier-write"]
        )

    # -- the flag on ---------------------------------------------------------------------------

    def test_on_the_step_is_run_again_on_the_members_model_and_its_call_is_proposed(self):
        client = self.go(
            [
                write(title="T", project_id=1, content_text=CHEAP_BODY),
                write(title="T", project_id=1, content_text=BETTER_BODY),
            ],
            flags=ON,
        )
        self.assertEqual(len(client.calls), 2)
        planning, rerun = client.calls
        self.assertIsNotNone(planning["model_override"])
        self.assertIsNone(rerun["model_override"], "no override = the member's chosen model")
        # Same step, same history, same prompt and tools; only the model differs.
        self.assertEqual(planning["messages"], rerun["messages"])
        self.assertEqual(planning["system"], rerun["system"])
        self.assertEqual(planning["tools"], rerun["tools"])
        pending = self.pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["arguments"]["content_text"], BETTER_BODY)
        self.assertNotIn(CHEAP_BODY, repr(pending))
        self.assertEqual(self.purposes, ["planning", "write_rerun"])

    def test_on_the_planning_text_is_not_shown_and_the_rerun_text_is(self):
        client = self.go(
            [
                [
                    ("cheap musings. ", None),
                    *write(title="T", project_id=1, content_text=CHEAP_BODY),
                ],
                [
                    ("Drafting it properly. ", None),
                    *write(title="T", project_id=1, content_text=BETTER_BODY),
                ],
            ],
            flags=ON,
        )
        text = "".join(e.get("text", "") for e in self.events if e.get("type") == "answer_delta")
        self.assertNotIn("cheap musings", text)
        self.assertIn("Drafting it properly.", text)
        self.assertEqual(len(client.calls), 2)

    def test_on_a_rerun_that_answers_in_text_ends_the_run_with_that_answer(self):
        client = self.go(
            [
                write(title="T", project_id=1, content_text=CHEAP_BODY),
                [("Which project should this go in?", None)],
            ],
            flags=ON,
        )
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.pending(), [])
        text = "".join(e.get("text", "") for e in self.events if e.get("type") == "answer_delta")
        self.assertIn("Which project", text)

    def test_on_a_write_without_a_body_is_not_run_again(self):
        # Nothing to write well: a status change costs no second call.
        client = self.go(
            [write("update_task", task_id=5, status="Closed")],
            flags=ON,
            fake_writes=("update_task",),
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.purposes, ["planning"])
        self.assertEqual(len(self.pending()), 1)

    def test_on_each_body_carrying_write_is_run_again(self):
        cases = {
            "create_task": {"title": "T", "project_id": 1, "content_text": CHEAP_BODY},
            "update_task": {"task_id": 5, "body_append": CHEAP_BODY},
            "create_note": {"title": "N", "content_text": CHEAP_BODY},
            "update_note": {"note_id": 5, "content_text": CHEAP_BODY},
            "create_task_plan": {
                "project_id": 1,
                "tasks": [{"title": "a", "content_markdown": CHEAP_BODY}],
            },
            "manage_milestone": {"action": "update", "description_markdown": CHEAP_BODY},
        }
        for name, args in cases.items():
            with self.subTest(tool=name):
                client = self.go(
                    [write(name, **args), write(name, **args)], flags=ON, fake_writes=(name,)
                )
                self.assertEqual(len(client.calls), 2, name)
                self.assertEqual(self.purposes, ["planning", "write_rerun"])

    def test_on_a_read_call_is_not_run_again(self):
        client = self.go(
            [call("not_a_real_tool", {}), [("Done.", None)]],
            flags=ON,
        )
        # step 1 planning (tool, not a write), step 2 planning text -> the existing
        # discard-and-rerun of the FINAL answer: 3 calls, none of them a write_rerun.
        self.assertNotIn("write_rerun", self.purposes)

    def test_on_without_the_two_tier_loop_there_is_nothing_to_rerun(self):
        # No planning model (the effort flags are off): one model, one call, flag or not.
        client = self.go(
            [write(title="T", project_id=1, content_text=CHEAP_BODY)],
            flags={"AGENT_BODY_ON_USER_MODEL": True},
            choice=None,
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.purposes, ["loop"])
        self.assertEqual(self.pending()[0]["arguments"]["content_text"], CHEAP_BODY)

    def test_on_at_a_level_with_no_planning_model_is_a_single_call(self):
        high = LlmChoice(provider="gemini", model="gemini-3.6-flash", effort="high")
        client = self.go(
            [write(title="T", project_id=1, content_text=CHEAP_BODY)], flags=ON, choice=high
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.purposes, ["loop"])

    def test_the_flag_defaults_off(self):
        from django.conf import settings

        self.assertFalse(settings.SEARCH_ENGINE["AGENT_BODY_ON_USER_MODEL"])
