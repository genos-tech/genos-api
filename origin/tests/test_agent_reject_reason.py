"""MIP-889: an optional reason on reject, shown to the model only behind AGENT_REJECT_REASON.

`POST /decide/` accepts `reason` (a string, at most 500 characters, only with `reject`) and
stores it on the step. With `SEARCH_ENGINE["AGENT_REJECT_REASON"]` on, the model's result for
the rejected call is `{"error": "user_rejected", "reason": "..."}` and system-prompt rule 7
gains one line. Off (the default) the model is sent exactly what `origin/main` sends.

PINS. The off-state goldens were recorded on PRISTINE `origin/main` (346816f2), where a
`reason` in the body is simply ignored, with the same harness (the flag-pin harness of
MIP-977, which touches nothing this change adds):

    git -C <genos-api> worktree add --detach <wt> origin/main
    cp origin/tests/test_agent_reject_reason.py origin/tests/test_agent_resume_flag_pins.py \\
       origin/tests/test_agent_resume_system_extra.py origin/tests/test_agent_resume_prior_turns.py \\
       <wt>/origin/tests/
    RECORD_GOLDEN=1 python manage.py test origin.tests.test_agent_reject_reason.FlagOffTests --noinput
    # prints `GOLDEN <key> = "<sha256>"`; paste below

`prompt/*` hash `agent_system_prompt()` (both cheat-sheet variants); `reject/*` hash every
model call of a whole run (system instruction and messages) that is rejected, with and
without a reason in the body, and with a second pause (so the replay of the first rejection
is in the hash).
"""

from __future__ import annotations

import hashlib
import os

from django.conf import settings
from django.test import TestCase, override_settings

from origin.search_engine.agent.prompts import agent_system_prompt
from origin.search_engine.models import AgentStep
from origin.tests.test_agent_resume_flag_pins import NO_DATE, Recorder
from origin.tests.test_agent_resume_system_extra import DECIDE_URL, ResumeBase

REASON = "Wrong project: put it in Website Redesign, and make it High priority."

GOLDEN: dict[str, str] = {
    "prompt/legacy": "5aa0aeaa405f8b1b0759c04a4c99422b2778731d470ad9bfff8a2e30e936c6df",
    "prompt/registry": "f5a822b4a2ff2aba264e11ab94840f240bd94433449ebb8f2dff5acc1dc0477c",
    "reject/plain": "9b7aa41657fb037374a3c850064089fbee6b88f94da161e3ccb59fb839852df4",
    "reject/with-reason": "9b7aa41657fb037374a3c850064089fbee6b88f94da161e3ccb59fb839852df4",
    "reject/twice": "22126c9d1c23a04fc7b9506f5388ac844688f23cdd403abe918efa97c7701dc1",
}

ON = {"AGENT_REJECT_REASON": True}


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class PromptPins(TestCase):
    """The system prompt, flag off, for both cheat-sheet variants."""

    def prompt(self, registry: bool) -> str:
        cfg = {**settings.SEARCH_ENGINE, "AGENT_CHEATSHEET_FROM_REGISTRY": registry}
        with override_settings(SEARCH_ENGINE=cfg):
            return agent_system_prompt()

    def pin(self, key: str, registry: bool):
        digest = digest_text(self.prompt(registry))
        if os.environ.get("RECORD_GOLDEN"):
            print(f'GOLDEN {key} = "{digest}"')
            return
        self.assertEqual(digest, GOLDEN[key])

    def test_the_legacy_prompt_is_unchanged_with_the_flag_off(self):
        self.pin("prompt/legacy", registry=False)

    def test_the_registry_prompt_is_unchanged_with_the_flag_off(self):
        self.pin("prompt/registry", registry=True)

    def test_with_the_flag_on_exactly_one_line_is_added_after_rule_7(self):
        for registry in (False, True):
            off = self.prompt(registry)
            cfg = {**settings.SEARCH_ENGINE, "AGENT_CHEATSHEET_FROM_REGISTRY": registry, **ON}
            with override_settings(SEARCH_ENGINE=cfg):
                on = agent_system_prompt()
            self.assertNotEqual(on, off)
            self.assertIn('carries a "reason"', on)
            self.assertNotIn("reason", off.split("  7. ")[1].split("  8. ")[0])
            # Rule 7's own sentence is untouched and still first.
            rule_7 = on.split("  7. ")[1].split("  8. ")[0]
            self.assertTrue(
                rule_7.startswith('If a tool returns {"error": "user_rejected"} or\n')
            )
            # Everything before rule 7 and from rule 8 on is the flag-off prompt, byte for byte.
            head, tail = off.split("  7. ")[0], "  8. " + off.split("  8. ", 1)[1]
            self.assertTrue(on.startswith(head))
            self.assertTrue(on.endswith(tail))


class RejectBase(Recorder, ResumeBase):
    flags: dict = NO_DATE
    script = ("write", "answer")

    def reject(self, run_id, token, reason=None):
        body = {"run_id": run_id, "approval_token": token, "decision": "reject"}
        if reason is not None:
            body["reason"] = reason
        return self.client.post(DECIDE_URL, body, format="json")

    def reject_to_the_end(self, run_id, token, reason=None):
        return self.read(self.reject(run_id, token, reason))

    def rejection_responses(self):
        """The function responses of the tool the model was sent, per model call."""
        out = []
        for _system, messages in self.calls:
            out.append([m.function_response for m in messages if m.role == "tool_response"])
        return out


class FlagOffTests(RejectBase):
    def pin(self, key):
        if os.environ.get("RECORD_GOLDEN"):
            print(f'GOLDEN {key} = "{self.digest()}"')
            return
        self.assertEqual(self.digest(), GOLDEN[key], f"what the model is sent changed: {key}")

    def test_a_rejection_without_a_reason(self):
        run_id, token = self.ask()
        self.reject_to_the_end(run_id, token)
        self.pin("reject/plain")

    def test_a_rejection_with_a_reason_is_main_s_byte_for_byte(self):
        run_id, token = self.ask()
        self.reject_to_the_end(run_id, token, REASON)
        self.pin("reject/with-reason")
        self.assertEqual(self.rejection_responses()[1], [{"error": "user_rejected"}])

    def test_a_reason_is_stored_but_never_shown_with_the_flag_off(self):
        run_id, token = self.ask()
        self.reject_to_the_end(run_id, token, REASON)
        step = AgentStep.objects.get(run__run_id=run_id, tool_name="create_task")
        self.assertEqual(step.decision, "reject")
        self.assertEqual(step.decision_reason, REASON)
        self.assertNotIn("reason", repr(self.calls[1][1]))
        self.assertNotIn("Wrong project", self.calls[1][0])

    def test_the_replay_of_an_earlier_rejection_on_a_second_pause(self):
        self.model.script[:] = ["write", "write", "answer"]
        run_id, token = self.ask()
        events = self.reject_to_the_end(run_id, token, REASON)
        (again,) = [e for e in events if e["type"] == "tool_call_pending_approval"]
        self.reject_to_the_end(run_id, again["approval_token"], "Still wrong")
        self.assertEqual(len(self.calls), 3)
        self.pin("reject/twice")
        self.assertEqual(
            self.rejection_responses()[2], [{"error": "user_rejected"}, {"error": "user_rejected"}]
        )


class FlagOnTests(RejectBase):
    flags = {**NO_DATE, **ON}

    def test_the_model_is_sent_the_reason_beside_the_exact_error_key(self):
        run_id, token = self.ask()
        self.reject_to_the_end(run_id, token, REASON)
        self.assertEqual(
            self.rejection_responses()[1], [{"error": "user_rejected", "reason": REASON}]
        )
        self.assertIn('carries a "reason"', self.calls[1][0])

    def test_a_rejection_without_a_reason_is_the_bare_result(self):
        run_id, token = self.ask()
        self.reject_to_the_end(run_id, token)
        self.assertEqual(self.rejection_responses()[1], [{"error": "user_rejected"}])

    def test_a_blank_reason_is_the_bare_result(self):
        run_id, token = self.ask()
        self.reject_to_the_end(run_id, token, "   \n\t ")
        self.assertEqual(self.rejection_responses()[1], [{"error": "user_rejected"}])

    def test_a_second_pause_replays_the_first_rejection_with_its_reason(self):
        self.model.script[:] = ["write", "write", "answer"]
        run_id, token = self.ask()
        events = self.reject_to_the_end(run_id, token, REASON)
        (again,) = [e for e in events if e["type"] == "tool_call_pending_approval"]
        self.reject_to_the_end(run_id, again["approval_token"])
        replayed, latest = self.rejection_responses()[2]
        self.assertEqual(replayed, {"error": "user_rejected", "reason": REASON})
        self.assertEqual(latest, {"error": "user_rejected"})

    def test_control_characters_are_gone_and_lines_become_one(self):
        run_id, token = self.ask()
        self.reject_to_the_end(run_id, token, "no\x00t th\x1bis\n\nproject‮!")
        (response,) = self.rejection_responses()[1]
        self.assertEqual(response["reason"], "not this project!")

    def test_a_row_written_by_another_path_is_capped_and_cleaned_on_the_way_out(self):
        from origin.search_engine.agent import approval_reason  # absent on main (the recording)

        run_id, token = self.ask()
        self.reject_to_the_end(run_id, token, "x")
        step = AgentStep.objects.get(run__run_id=run_id, tool_name="create_task")
        step.decision_reason = "a" * 900 + "\x07"
        step.save(update_fields=["decision_reason"])
        self.assertEqual(
            approval_reason.rejection_result(step.decision_reason)["reason"], "a" * 500
        )

    def test_an_approval_is_unaffected(self):
        run_id, token = self.ask()
        self.decide_to_the_end(run_id, token, "approve")
        for response in self.rejection_responses()[1]:
            self.assertNotIn("reason", response)


class DecideContractTests(RejectBase):
    """The endpoint, whatever the flag says."""

    def test_a_reason_with_an_approval_is_refused_and_nothing_is_spent(self):
        run_id, token = self.ask()
        body = {"run_id": run_id, "approval_token": token, "decision": "approve", "reason": "x"}
        resp = self.client.post(DECIDE_URL, body, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.row(run_id).status, "awaiting_approval")  # the card still works

    def test_a_reason_over_the_cap_is_refused(self):
        run_id, token = self.ask()
        resp = self.reject(run_id, token, "x" * 501)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.row(run_id).status, "awaiting_approval")
        self.assertEqual(self.reject(run_id, token, "x" * 500).status_code, 200)

    def test_a_reason_that_is_not_text_is_refused(self):
        run_id, token = self.ask()
        for bad in (5, ["a"], {"a": 1}):
            resp = self.reject(run_id, token, bad)
            self.assertEqual(resp.status_code, 400, bad)
        self.assertEqual(self.row(run_id).status, "awaiting_approval")

    def test_a_rejection_with_no_reason_stores_none(self):
        run_id, token = self.ask()
        self.reject_to_the_end(run_id, token)
        step = AgentStep.objects.get(run__run_id=run_id, tool_name="create_task")
        self.assertEqual(step.decision_reason, "")

    def test_the_stored_reason_is_cleaned(self):
        run_id, token = self.ask()
        self.reject_to_the_end(run_id, token, "  a\x00b\nc  ")
        step = AgentStep.objects.get(run__run_id=run_id, tool_name="create_task")
        self.assertEqual(step.decision_reason, "ab c")

    def test_the_reason_is_not_in_the_events_the_browser_gets(self):
        run_id, token = self.ask()
        events = self.reject_to_the_end(run_id, token, REASON)
        self.assertNotIn("Wrong project", repr(events))
