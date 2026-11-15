"""The eval cases the ONE grouped paid run of the MIP-578 parity batch (2026-11-15) showed to be wrong.

No model runs. Each fix is held two ways: the logged answer (or tool sequence) the run produced now
passes, and an answer that really is wrong still fails, so a check loosened too far is caught here.
The logs are in `genos-wt/logs/parity/` (`s1-new`, `s2-reg`, `s2b-reg-allon`, `f1-reps3`):

  * `t1057.plain-ask.en`, `t977.plain-ask.approve.en`, `t991.first-turn.approve.en`: the answer
    "Created [KEY](task:N)." is right, but the check wanted the title in it (6 of 6 failed on it);
  * `t1271-{en,ja}-full-timeline`: `get_timeline` is on by default now, so a correct timeline comes
    from that tool and not from `fetch_task` / `get_task_history` (the check wanted those);
  * `t1271-ja-full-timeline`: the closing fact was read from an introduction that says "完了まで"
    (out of order in 4 of 8 runs): `t1281` already ignores such summary lines;
  * `t1271-{en,ja}-find-task`: a correct find takes 5 to 6 tool calls, the check allowed 4.
"""

from __future__ import annotations

import copy

from django.test import SimpleTestCase

from origin.search_engine.agent.evals import runner
from origin.search_engine.agent.evals.group import harness, resume_runner


def cases():
    return {c.id: c for c in harness.load_cases(include_legacy=False)}


def events(tools, answer=""):
    out = [{"type": "tool_call_start", "tool_name": t, "step": i} for i, t in enumerate(tools)]
    out.append({"type": "answer_delta", "text": answer})
    return out


def failed(case_id, tools, answer):
    """The names of the checks that fail for a behaviour case: the runner's, then the group's."""
    expect = cases()[case_id].raw["expect"]
    reasons = [
        r.split(":")[0] for r in runner._check_behavior_expectations(events(tools, answer), expect)
    ]
    group = harness.group_checks(
        expect, answer=answer, tools=tools, tool_text="", calls=len(tools) + 1
    )
    return reasons + [c["name"] for c in group if not c["ok"]]


def resume_failed(case_id, answer):
    expect = cases()[case_id].raw["expect"]
    checks = resume_runner.evaluate(
        expect,
        answer=answer,
        tools=[],
        resumed_prompt="",
        follow_up_prompt="",
        final_prompt="",
        pending=None,
        calls=2,
    )
    return [c["name"] for c in checks if not c["ok"] and c["name"] == "answer_contains_any"]


class CreatedTaskAnswerTests(SimpleTestCase):
    CASES = (
        "t1057.plain-ask.en",
        "t977.plain-ask.approve.en",
        "t991.first-turn.approve.en",
    )

    def test_the_answers_the_run_produced_pass(self):
        # Verbatim from the logs: the title is not in them, the task chip is.
        for case_id, answer in (
            ("t1057.plain-ask.en", "Created [EAAE6-1](task:319)."),
            ("t977.plain-ask.approve.en", "Created [#EB031-1](task:308) in project 382."),
            ("t991.first-turn.approve.en", "Created [EF304-1](task:298)."),
        ):
            with self.subTest(case=case_id):
                self.assertEqual(resume_failed(case_id, answer), [])

    def test_an_answer_that_confirms_nothing_still_fails(self):
        for case_id in self.CASES:
            with self.subTest(case=case_id):
                self.assertEqual(
                    resume_failed(case_id, "I could not do that."), ["answer_contains_any"]
                )

    def test_the_titles_still_pass_and_the_japanese_twins_are_untouched(self):
        self.assertEqual(
            resume_failed("t991.first-turn.approve.en", "The Billing migration kickoff is made."),
            [],
        )
        for case_id in (
            "t1057.plain-ask.ja",
            "t977.plain-ask.approve.ja",
            "t991.first-turn.approve.ja",
        ):
            self.assertNotIn("(task:", cases()[case_id].raw["expect"]["answer_contains_any"])


class FullTimelineTests(SimpleTestCase):
    ENGLISH = (
        "Here is the timeline for task [CLP-1](task:248):\n"
        "- Ben Manager assigned it to Chris Dev\n"
        "- Chris Dev changed the status from Open to WIP\n"
        "- the cause is a missing permission\n"
        "- Ben asked to check the CSV export too\n"
        "- Chris Dev marked it Closed\n"
    )
    #: the shape of the 5 failing runs: the introduction says "完了まで" before anything happened
    JAPANESE = (
        "タスク [CLP-1](task:250) の発生から完了までの時系列の全経緯です。\n\n"
        "- **ステータス**: `Closed`\n"
        "- 00:30 Ben Manager が担当者に Chris Dev を割り当て\n"
        "- 01:00 Chris Dev がステータスを `Open` から `WIP` に変更\n"
        "- 02:30 原因は権限の不足です\n"
        "- 03:15 CSVエクスポートも確認してください\n"
        "- 06:00 Chris Dev がクローズ\n"
    )
    TOOLS = ["search_knowledge_base", "list_tasks", "get_timeline"]

    def test_a_timeline_read_with_get_timeline_passes_in_both_languages(self):
        self.assertEqual(failed("t1271-en-full-timeline", self.TOOLS, self.ENGLISH), [])
        self.assertEqual(failed("t1271-ja-full-timeline", self.TOOLS, self.JAPANESE), [])

    def test_the_older_routes_still_pass(self):
        for case_id, answer in (
            ("t1271-en-full-timeline", self.ENGLISH),
            ("t1271-ja-full-timeline", self.JAPANESE),
        ):
            for tools in (["fetch_task"], ["get_task_history"]):
                with self.subTest(case=case_id, tools=tools):
                    self.assertEqual(failed(case_id, tools, answer), [])

    def test_no_task_read_at_all_still_fails(self):
        for case_id, answer in (
            ("t1271-en-full-timeline", self.ENGLISH),
            ("t1271-ja-full-timeline", self.JAPANESE),
        ):
            with self.subTest(case=case_id):
                self.assertIn("tools_used_contains_any", failed(case_id, ["list_tasks"], answer))

    def test_the_introduction_and_the_status_line_are_not_the_closing_event(self):
        # The old list counted 完了 and 解決 anywhere: the introduction above was "closed" before "assign".
        expect = cases()["t1271-ja-full-timeline"].raw["expect"]
        closed = next(f for f in expect["facts_in_order"] if "クローズ" in f)
        self.assertNotIn("完了", closed)
        self.assertNotIn("解決", closed)
        self.assertEqual(
            expect["facts_ignore_lines"],
            cases()["t1281-ja-full-timeline"].raw["expect"]["facts_ignore_lines"],
        )
        # And the logged failure is reproduced by the OLD wording: out of order on this very answer.
        old = copy.deepcopy(expect)
        old.pop("facts_ignore_lines")
        old["facts_in_order"][-1] = [*closed[:-1], "完了", "解決", closed[-1]]
        before = harness.group_checks(
            old, answer=self.JAPANESE, tools=self.TOOLS, tool_text="", calls=5
        )
        (order,) = [c for c in before if c["name"] == "facts_in_order"]
        self.assertFalse(order["ok"])
        self.assertIn("out of order", order["detail"])

    def test_a_timeline_that_closes_before_it_starts_still_fails_the_order(self):
        backwards = (
            "- 00:00 Chris Dev がクローズ\n"
            "- 00:30 Chris Dev に割り当て\n"
            "- 01:00 WIP に変更\n"
            "- 02:30 原因は権限の不足\n"
            "- 03:15 CSV を確認\n"
        )
        self.assertIn("facts_in_order", failed("t1271-ja-full-timeline", self.TOOLS, backwards))

    def test_an_invented_step_still_fails(self):
        for case_id, answer in (
            ("t1271-en-full-timeline", self.ENGLISH + "- the client confirmed the fix\n"),
            ("t1271-ja-full-timeline", self.JAPANESE + "- 顧客が確認\n"),
        ):
            with self.subTest(case=case_id):
                self.assertIn("answer_does_not_contain", failed(case_id, self.TOOLS, answer))


class FindTaskTests(SimpleTestCase):
    FIND = [
        "search_knowledge_base",
        "search_knowledge_base",
        "list_tasks",
        "fetch_task",
        "get_team_members",
    ]

    def test_five_and_six_tool_calls_pass_and_seven_fails(self):
        for case_id in ("t1271-en-find-task", "t1271-ja-find-task"):
            answer = "The task is CLP-1."
            with self.subTest(case=case_id):
                self.assertEqual(failed(case_id, self.FIND, answer), [])
                self.assertEqual(failed(case_id, [*self.FIND, "list_projects"], answer), [])
                over = failed(case_id, [*self.FIND, "list_projects", "get_my_focus_tasks"], answer)
                self.assertTrue(any(r.startswith("tool_calls_at_most") for r in over), over)
