"""MIP-1518: the two Japanese t1281 cases failed on their own checks, not on the model's answer.

`t1281-ja-who-waits`: the right answer is "nobody waits on anybody" and the model says so, using the
word 返信待ち negated ("返信待ち ... はありません"); the plain-substring `answer_does_not_contain`
forbade the word, so a correct answer failed. The case now forbids a CLAIM (`answer_does_not_match`).

`t1281-ja-full-timeline`: the `closed` group of `facts_in_order` held 完了 and 解決, which the
model's opening line ("発生から解決・完了までの全経緯") says before the first event, so the order
check reported "closed out of order". 完了 and 解決 are no longer in the group.

The tests replay the real checks (the runner's assertions plus the group layer, split the way
`case_runner` splits them) over inline answers: the model's correct answers shaped like the ones
a paid run recorded, and answers that are really wrong, which must still fail.

MIP-1532: the same `t1281-ja-full-timeline` check still failed a correct answer that opens with a
status header ("ステータス: Closed", the paid run's rep 2). A header is a summary, not an event:
`facts_ignore_lines` drops it before the order is read. The closing EVENT line must still come last.
"""

from __future__ import annotations

import re
from typing import Any

from django.test import SimpleTestCase

from origin.search_engine.agent.evals import runner
from origin.search_engine.agent.evals.group import harness


def case_expect(case_id: str) -> dict[str, Any]:
    case = next(c for c in harness.load_cases(include_legacy=False) if c.id == case_id)
    return dict(case.raw["expect"])


def run_checks(expect: dict[str, Any], answer: str, tools: list[str]) -> list[str]:
    """Failure reasons for `answer`, evaluated the way `DefaultCaseRunner._behaviour` does."""
    expect = dict(expect)
    own = {k: expect.pop(k) for k in list(expect) if k in harness.GROUP_EXPECT_KEYS}
    events: list[dict[str, Any]] = [{"type": "tool_call_start", "tool_name": t} for t in tools]
    events.append({"type": "answer_delta", "text": answer})
    reasons = list(runner._check_behavior_expectations(events, expect))
    for check in harness.group_checks(own, answer=answer, tools=tools, tool_text="", calls=1):
        if not check["ok"]:
            reasons.append(f"{check['name']}: {check['detail']}")
    return reasons


WAITS_TOOLS = ["list_my_inbox", "get_my_blockers", "list_tasks", "fetch_chat_thread"]

# The model's correct answers (the shapes a paid run recorded): nobody waits, said with the word
# 返信待ち or 待って... negated.
WAITS_CORRECT = [
    # recorded rep 1 and rep 3 used the word 返信待ち inside a negation
    "現在、ワークスペース内で**返答や対応を待っている人（保留中・ブロック中の返信待ち状態）はいません**。\n\n"
    "- **CLP-1**: Ben Manager から Chris Dev へ「CSVエクスポートも確認してください」との追加依頼がありましたが、"
    "Chris Dev が修正・リリースを行い、タスクは完了 (`Closed`) しています。\n\n"
    "現在オープンとなっているタスクやブロック中の依存関係、未解消の返信リクエストは存在しません。",
    "ワークスペース内の会話・タスク・ブロッカー情報を確認した結果、現在**誰かが誰かの返事を待っている状態"
    "（ペンディングや返信待ちの事項）は確認されませんでした**。\n\n"
    "- CLP-1: Chris Dev が修正とリリースを完了した旨を返答し、タスクは `Closed`（完了）となっています。\n\n"
    "現在、未回答の質問やブロック中のタスクはありません。",
    # recorded rep 2 (passed before the fix too)
    "現在、ワークスペース内で**返事や対応を待っている状態（未回答の質問や保留中のタスク）のユーザーはいません**。\n\n"
    "- CLP-1: Chris Dev が修正・リリースを行い、解決済み（`Closed`）として報告を完了しています。\n\n"
    "現在、保留（`Pending`）やブロック中（`Blocked`）のタスク・未返信のメッセージは確認されませんでした。",
    # other wordings a correct answer can use
    "誰も誰かの返事を待っていません。返信待ちのメッセージはありません。CLP-1 はクローズ済みです。",
    "今は誰も待たされていません。CSV の依頼にも Chris が回答済みで、返信待ちは残っていません。",
]

# Answers that REALLY claim someone is waiting: the fixed check must still reject every one.
WAITS_WRONG = [
    "Ben Manager が Chris Dev の返事を待っています。CSVエクスポートの確認依頼が未回答です。",
    "Ben さんは Chris さんの返信待ちです。",
    "CSVの件は返信待ちの状態です。",
    "現在、Chris Dev からの返信待ちのタスクがあります。",
    "Alice Client は Ben の回答を待ってます。",
    "Chris Dev は Ben Manager の確認を待機しています。",
    "Ben の返信待ち中です。",
    "Ben still waiting on Chris to reply.",
    "まだ待っている人がいます。",
]

TIMELINE_TOOLS = ["search_knowledge_base", "get_timeline", "fetch_task"]

TIMELINE_EVENTS = (
    "- **00:00** Alice Client がチャットで不具合を報告。\n"
    "- **00:20** Ben Manager がタスク CLP-1 を作成。\n"
    "- **00:30** Ben Manager が担当者を **Chris Dev** に設定。\n"
    "- **01:00** Chris Dev がステータスを `Open` から `WIP`（着手）に変更。\n"
    "- **02:30** Chris Dev がコメント: 「Yを再現しました。原因は権限の不足です」\n"
    "- **03:15** Ben Manager がコメント: 「CSVエクスポートも確認してください」\n"
    "- **07:00** Chris Dev がステータスを `WIP` から `Closed` に変更。\n"
)

# Correct answers, including the openings the model really wrote (recorded reps 1 and 2), which say
# 解決 / 完了 BEFORE the first event.
TIMELINE_CORRECT = [
    "タスク **[CLP-1](task:28)** の発生から完了までの経緯は以下の通りです。\n\n---\n\n### 時系列の経緯\n\n"
    + TIMELINE_EVENTS,
    "タスク CLP-1 の経緯です（チャットでの課題提起からタスク完了までの時系列順）。\n\n"
    + TIMELINE_EVENTS,
    "「Fix the export that fails」の発生から解決・完了までの全経緯を時系列でまとめました。\n\n"
    + TIMELINE_EVENTS,
    "タスク CLP-1 の経緯を時系列順にまとめました。\n\n" + TIMELINE_EVENTS,
]

# Answers that really put the close BEFORE the first event: the fixed check must still reject them.
TIMELINE_WRONG = [
    "このタスクは Closed 済みです。経緯:\n\n" + TIMELINE_EVENTS,
    "結論: クローズされました。\n\n" + TIMELINE_EVENTS,
    # the events themselves out of order (closed listed before the work)
    "- **00:00** Alice Client がチャットで不具合を報告。\n- **00:10** Chris Dev がタスクを `Closed` に変更。\n"
    "- **00:30** Ben Manager が担当者を Chris Dev に設定。\n- **01:00** WIP に変更。\n"
    "- **02:30** 原因は権限の不足。\n- **03:15** CSVエクスポートも確認。\n",
    # an event missing
    "- **00:30** Chris Dev に割り当て。\n- **01:00** WIP に変更。\n- **07:00** Closed に変更。\n",
]

# MIP-1532: the answer the paid run of 2026-11-14 recorded (rep 2): a summary block with a status
# header, then the events in the right order. Shapes of the header a model can write.
HEADERS = [
    "### 📋 タスク概要\n- **タスク名**: CLP-1 Fix the export\n- **ステータス**: `Closed`\n- **担当者**: Chris Dev\n\n",
    "ステータス: 完了\n\n",
    "**状態**: クローズ済み\n\n",
    "- Status: Closed\n\n",
    "**ステータス：** `Closed`\n\n",
    "`Closed`\n\n",
    "完了\n\n",
    "【完了】\n\n",
]
TIMELINE_WITH_HEADER = [h + "### 時系列の経緯\n\n" + TIMELINE_EVENTS for h in HEADERS]

# Answers where the header is NOT what saves them: they must still fail.
TIMELINE_WRONG_WITH_HEADER = [
    # the close really is listed first, a header or not
    "ステータス: Closed\n\n- **00:00** Chris Dev がタスクを `Closed` に変更。\n" + TIMELINE_EVENTS,
    # the header is the ONLY mention of the close: no closing event
    "- **ステータス**: `Closed`\n\n"
    + "".join(TIMELINE_EVENTS.splitlines(keepends=True)[:-1]),
    # the close is announced in a sentence, not a header
    "このタスクは Closed 済みです。\n\n" + TIMELINE_EVENTS,
    # a header, then the events out of order
    "ステータス: Closed\n\n- **00:00** 報告。\n- **00:10** Chris Dev が `Closed` に変更。\n"
    "- **00:30** 担当者を Chris Dev に。\n- **01:00** WIP に変更。\n- **02:30** 権限の不足。\n"
    "- **03:15** CSVエクスポートも確認。\n",
]


class WhoWaitsCheckTests(SimpleTestCase):
    def setUp(self):
        self.expect = case_expect("t1281-ja-who-waits")

    def test_the_word_is_no_longer_forbidden_only_the_claim(self):
        self.assertNotIn("返信待ち", self.expect["answer_does_not_contain"])
        self.assertIn("まだ待", self.expect["answer_does_not_contain"])
        self.assertTrue(self.expect["answer_does_not_match"])

    def test_correct_answers_pass_including_the_negated_word(self):
        for answer in WAITS_CORRECT:
            self.assertEqual(run_checks(self.expect, answer, WAITS_TOOLS), [], answer[:40])

    def test_the_old_definition_failed_the_negated_answers(self):
        old = {**self.expect}
        old.pop("answer_does_not_match")
        old["answer_does_not_contain"] = [*old["answer_does_not_contain"], "返信待ち"]
        failed = [a for a in WAITS_CORRECT if run_checks(old, a, WAITS_TOOLS)]
        self.assertEqual(len(failed), 4)  # all but the one that never wrote 返信待ち

    def test_answers_that_really_claim_a_wait_still_fail(self):
        for answer in WAITS_WRONG:
            self.assertTrue(run_checks(self.expect, answer, WAITS_TOOLS), answer)

    def test_every_pattern_is_a_valid_regex(self):
        for pattern in self.expect["answer_does_not_match"]:
            re.compile(pattern)


class FullTimelineCheckTests(SimpleTestCase):
    def setUp(self):
        self.expect = case_expect("t1281-ja-full-timeline")

    def closed_group(self) -> list[str]:
        return self.expect["facts_in_order"][-1]

    def test_the_closed_group_no_longer_holds_the_words_of_the_opening_line(self):
        self.assertNotIn("完了", self.closed_group())
        self.assertNotIn("解決", self.closed_group())
        self.assertIn("closed", self.closed_group())
        self.assertIn("クローズ", self.closed_group())

    def test_correct_answers_pass_even_with_the_closing_words_in_the_opening_line(self):
        for answer in TIMELINE_CORRECT:
            self.assertEqual(run_checks(self.expect, answer, TIMELINE_TOOLS), [], answer[:40])

    def test_the_old_group_failed_the_openings(self):
        old = {**self.expect}
        old["facts_in_order"] = [
            *old["facts_in_order"][:-1],
            [*self.closed_group(), "完了", "解決"],
        ]
        failed = [a for a in TIMELINE_CORRECT if run_checks(old, a, TIMELINE_TOOLS)]
        self.assertEqual(len(failed), 3)

    def test_answers_that_really_close_first_or_miss_an_event_still_fail(self):
        for answer in TIMELINE_WRONG:
            reasons = run_checks(self.expect, answer, TIMELINE_TOOLS)
            self.assertTrue(any(r.startswith("facts_in_order") for r in reasons), answer)

    def test_the_tool_requirement_is_unchanged(self):
        self.assertEqual(self.expect["tools_used_contains"], ["get_timeline"])
        self.assertTrue(run_checks(self.expect, TIMELINE_CORRECT[0], ["fetch_task"]))

    def test_a_status_header_is_not_the_closing_event(self):
        for answer in TIMELINE_WITH_HEADER:
            self.assertEqual(run_checks(self.expect, answer, TIMELINE_TOOLS), [], answer[:60])

    def test_without_the_ignore_rule_the_headers_fail(self):
        bare = {k: v for k, v in self.expect.items() if k != "facts_ignore_lines"}
        words = [w.lower() for w in self.closed_group()]
        for answer in TIMELINE_WITH_HEADER:
            head = answer.split("### 時系列")[0].lower()
            failed = bool(run_checks(bare, answer, TIMELINE_TOOLS))
            # a header with a word of the closed group put it before the first event (the 完了 /
            # 解決 headers never did: MIP-1518 took those words out of the group)
            self.assertEqual(failed, any(w in head for w in words), head)

    def test_a_header_does_not_rescue_a_wrong_answer(self):
        for answer in TIMELINE_WRONG_WITH_HEADER:
            reasons = run_checks(self.expect, answer, TIMELINE_TOOLS)
            self.assertTrue(any(r.startswith("facts_in_order") for r in reasons), answer[:60])

    def test_the_event_line_about_the_status_is_not_dropped(self):
        # "ステータスを `Closed` に変更" is an event: the ignore rule must not eat it
        for pattern in self.expect["facts_ignore_lines"]:
            self.assertIsNone(
                re.search(pattern, "- **07:00** Chris Dev がステータスを `WIP` から `Closed` に変更。")
            )

    def test_every_ignore_pattern_is_a_valid_regex(self):
        for pattern in self.expect["facts_ignore_lines"]:
            re.compile(pattern)


class IgnoreLinesTests(SimpleTestCase):
    def check(self, answer, ignore):
        return harness.group_checks(
            {"facts_in_order": [["a"], ["b"]], "facts_ignore_lines": ignore},
            answer=answer,
            tools=[],
            tool_text="",
            calls=1,
        )[0]

    def test_an_ignored_line_neither_counts_nor_satisfies(self):
        self.assertTrue(self.check("note: b\na\nb", ["^note"])["ok"])
        self.assertFalse(self.check("note: b\nb\na", ["^note"])["ok"])
        self.assertIn("missing", self.check("a\nnote: b", ["^note"])["detail"])

    def test_without_the_key_nothing_is_dropped(self):
        got = harness.group_checks(
            {"facts_in_order": [["a"], ["b"]]}, answer="b\na\nb", tools=[], tool_text="", calls=1
        )[0]
        self.assertFalse(got["ok"])


class AnswerDoesNotMatchTests(SimpleTestCase):
    def check(self, answer: str, *patterns: str) -> dict[str, Any]:
        return harness.group_checks(
            {"answer_does_not_match": list(patterns)},
            answer=answer,
            tools=[],
            tool_text="",
            calls=1,
        )[0]

    def test_a_match_fails_and_names_the_pattern(self):
        got = self.check("He is STILL waiting", r"still\s+waiting")
        self.assertFalse(got["ok"])
        self.assertIn("still", got["detail"])

    def test_no_match_passes(self):
        self.assertTrue(self.check("Nobody is waiting", r"still\s+waiting")["ok"])

    def test_an_empty_answer_passes(self):
        self.assertTrue(self.check("", r"x")["ok"])
