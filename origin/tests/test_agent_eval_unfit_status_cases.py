"""The two grouped-eval cases `t2494-cs.unfit-ask` and `.unfit-reply` (MIP-2546) with a STUB model.

No model is called. This shows what the paid run will be judging: the declaration the model is sent
says to send the name anyway, a proposal of that kind passes the case's own checks, and the approved
write hands the resumed model the notice and the names to offer. Whether the real model then does
so only a billed run can tell.
"""

from __future__ import annotations

import json
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.search_engine.agent import controller
from origin.search_engine.agent.evals.group import content_checks, harness
from origin.tests.test_eval_group_resume_runner import RunnerBase, arm_overrides, cases


def declarations(on: bool):
    with mock.patch.dict(settings.SEARCH_ENGINE, {"AGENT_CUSTOM_STATUS": on}):
        return {d.name: d for d in controller._build_tool_declarations()}


class UnfitAskTests(SimpleTestCase):
    def test_the_model_is_told_to_send_an_unlisted_name_only_while_the_flag_is_on(self):
        said = declarations(True)["update_task"].parameters_schema["properties"]["custom_status"]
        self.assertIn("no reason to stop or ask", said["description"])
        off = declarations(False)["update_task"].parameters_schema["properties"]
        self.assertNotIn("custom_status", off)

    def test_the_proposal_the_wording_asks_for_passes_the_cases_own_checks(self):
        found = {c.id: c for c in harness.load_cases(include_legacy=False)}
        for lang, name in (("en", "Ready for QA"), ("ja", "QA待ち")):
            expect = found[f"t2494-cs.unfit-ask.{lang}"].raw["expect"]["pending_write"]
            args = {"task_id": 1, "custom_status": name, "priority": "High"}
            pending = [{"tool_name": "update_task", "arguments": args}]
            checks = content_checks.check_pending(expect, pending, answer="")
            self.assertTrue(checks)
            self.assertTrue(all(c["ok"] for c in checks), checks)


class UnfitReplyTests(RunnerBase):
    def test_the_resumed_model_is_handed_the_notice_and_the_names(self):
        for case_id in ("t2494-cs.unfit-reply.en", "t2494-cs.unfit-reply.ja"):
            self.model.calls.clear()
            (case,) = [c for c in cases() if c["id"] == case_id]
            result = self.run_case(case, arm_overrides("t2494"))
            # The stub answers a fixed line, so only the checks on what the model was SENT can pass.
            checks = {c["name"]: c for c in result["checks"]}
            self.assertTrue(checks["resumed_prompt_contains"]["ok"], checks)
            _, messages = self.model.calls[-1]
            sent = json.dumps(
                [m.function_response for m in messages if m.role == "tool_response"],
                ensure_ascii=False,
                default=str,
            )
            self.assertIn("list the status names", sent)
            self.assertIn("Ready", sent)
            self.assertLess(sent.index("tell_the_member"), sent.index('"suggestions"'))
