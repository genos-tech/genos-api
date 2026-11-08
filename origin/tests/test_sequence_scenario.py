"""Ken's 12-step example, read back as a sequence (MIP-1269, part of MIP-1268).

    1. A (client) asked X in the GM chat      7. C commented progress on the task
    2. B (PM) responded to A about X          8. B replied to that comment
    3. A asked B to handle issue Y due to X   9. C summarized it in a task note
    4. B created task-A for Y                10. C closed task-A with a comment
    5. C got assigned to task-A              11. B noticed that task-A was closed
    6. C started handling it                 12. A noticed that issue Y was solved

The story is built by `fixtures/sequence_scenario.py` through the real funnels (REST for the
chat, the task create form and the task comment; MCP tools for assign, update, comment and the
task note), with no AI and a frozen clock, in two variants:

  * V1: A is a member of the project;
  * V2: A is a client who is only in the GM chat and on no project.

Then every surface that should tell the story is read and pinned:

  * `get_item_line` (the task's line) as B, as C, as A in V1, as A in V2;
  * `get_my_briefing` for A, B and C, as of the moment each step happened;
  * the in-app agent's own tools (`fetch_task`, `get_task_history`, `fetch_chat_thread`,
    `fetch_note_history`): what each returns and in which order. No model runs here.

HOW TO READ A FAILURE. A passing test is a regression guard: today's behaviour is right and must
stay. A test marked `@unittest.expectedFailure` with a `GAP-n:` line states the DESIRED behaviour
and fails today: it is a named gap. When someone fixes the gap the test becomes an UNEXPECTED
SUCCESS, which the runner reports as a failure: delete the decorator then. Every gap has its own
Genos task (see the PR); none is fixed here.

The gaps, in one place:

  GAP-1  steps 1 and 2 (the chat before the thread the task came from) are on no line;
  GAP-2  nobody tells the requester the task closed (A, V1 or V2);
  GAP-3  the briefing counts one task comment twice (the task item and the project chat thread);
  GAP-4  the briefing never shows the task note C wrote (step 9) to the PM who reported the task;
  GAP-5  a note event on a line says "edited the note" and never what it says;
  GAP-6  the briefing's "waiting on others" never names the task B handed to C;
  GAP-7  in-app `fetch_task` names neither the task's thread nor its note;
  GAP-8  the in-app agent has no merged line (`get_item_line` is MCP only);
  GAP-9  B's top-level answer to A's question (step 2) is not in A's briefing.

Time: every instant is the local midnight three days ago plus minutes, the clock is frozen per
step and every briefing is given its `now`, so no test depends on the hour it runs at.
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime

from origin.models.chat.unified_models import Message
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY, ToolContext
from origin.tests.fixtures.sequence_scenario import (
    ACTOR,
    SequenceScenario,
    ground_truth,
    locate_steps,
    who_of,
)


def briefing_text(brief: dict, *sections: str) -> str:
    """The parts of a briefing that tell what happened, as one string without fences."""
    keys = sections or ("changed_for_me", "waiting_on_me", "waiting_on_others", "suggested_next")
    return re.sub(
        r"</?workspace_content>", "", json.dumps({k: brief[k] for k in keys}, default=str)
    )


class Story(SequenceScenario):
    """The cases below share the builder and the readers of `SequenceScenario`."""


# --------------------------------------------------------------------------- #
# The builder: the story really went through the funnels                       #
# --------------------------------------------------------------------------- #


class TestTheStoryIsBuiltTheRealWay(SequenceScenario):
    def test_the_rows_carry_the_real_funnels_marks(self):
        seq = self.build_sequence()
        task = seq.task
        # The create form's POST linked the thread (the origin chat), as the app sends it.
        self.assertEqual(task.thread_id, str(seq.thread_root.id))
        self.assertEqual(task.chat_id, str(seq.gm.id))
        self.assertEqual((task.reporter_id, task.assignee_id), (seq.b.id, seq.c.id))
        self.assertEqual(task.status, "Closed")
        # `via` says how each write arrived: the app for REST, the API key for MCP.
        via = {
            (row.actor_id, row.action_type): (row.metadata.get("via") or {}).get("kind")
            for row in TaskActivity.objects.filter(task=task)
        }
        self.assertEqual(via[(seq.b.id, "created")], "app")
        self.assertEqual(via[(seq.b.id, "assignee_changed")], "api_key")
        self.assertEqual(via[(seq.c.id, "status_changed")], "api_key")
        # The task card the app posts in the thread carries the task id.
        self.assertEqual(seq.messages["4card"].task_id, task.task_id)
        self.assertEqual(str(seq.messages["4card"].thread_root_id), str(seq.thread_root.id))
        # The task note has two versions (create, then edit), both by C.
        versions = NoteVersionMaster.objects.filter(note_type=2, note_id=seq.note["note_id"])
        self.assertEqual(versions.count(), 2)
        self.assertEqual({v.editor_id for v in versions}, {seq.c.id})

    def test_every_step_is_at_the_instant_it_was_scheduled(self):
        seq = self.build_sequence()
        self.assertEqual(seq.messages[1].ts_sent_at, seq.step_at[1])
        self.assertEqual(seq.messages[2].ts_sent_at, seq.step_at[2])
        self.assertEqual(seq.messages[3].ts_sent_at, seq.step_at[3])
        self.assertEqual(seq.comments[7].ts_sent_at, seq.step_at[7])
        self.assertEqual(seq.comments[8].ts_sent_at, seq.step_at[8])
        self.assertEqual(seq.comments[10].ts_sent_at, seq.step_at[10])
        created = TaskActivity.objects.get(task=seq.task, action_type="created")
        self.assertEqual(created.ts_created_at, seq.step_at[4])
        closed = TaskActivity.objects.get(task=seq.task, new_value="Closed")
        self.assertEqual(closed.ts_created_at, seq.close_at)


class TestTheFixtureForTheNextTest(SequenceScenario):
    """What MIP-1271 (behaviour cases for the in-app agent) reuses: the answer key and the
    text override."""

    def test_the_ground_truth_lists_every_step_in_the_order_it_happened(self):
        seq = self.build_sequence(announce=True)
        self.assertEqual(
            [f["step"] for f in ground_truth(seq)],
            [1, 2, 3, "3b", 4, 5, 6, 7, 8, 9, "9b", 10, "close", 11, "12a", 12],
        )
        self.assertEqual(
            [f["who"] for f in ground_truth(seq)],
            ["A", "B", "A", "B", "B", "B", "C", "C", "B", "C", "C", "C", "C", "B", "B", "A"],
        )

    def test_a_story_that_stops_early_lists_only_what_happened(self):
        seq = self.build_sequence(upto=5)
        self.assertEqual([f["step"] for f in ground_truth(seq)], [1, 2, 3, "3b", 4, 5])

    def test_the_texts_can_be_overridden_for_another_language(self):
        seq = self.build_sequence(texts={7: "進捗：原因は権限不足でした", 3: "Yを対応してください"})
        found = self.events_for(seq, seq.b)
        self.assertIn("進捗", found[7]["text"])
        self.assertIn("Yを対応", found[3]["text"])
        self.assertIn(seq.token[7], found[7]["text"])


# --------------------------------------------------------------------------- #
# get_item_line                                                                #
# --------------------------------------------------------------------------- #


class TestTheLineAsThePM(Story):
    """B created the task and asked C to do it: the line is B's view of the whole job."""

    def test_b_reads_steps_3_to_11_in_order_each_by_the_right_person(self):
        seq = self.build_sequence()
        found = self.events_for(seq, seq.b)
        steps = [3, "3b", 4, 5, 6, 7, 8, 9, "9b", 10, "close", 11]
        self.assertEqual(set(steps) - set(found), set(), "a step is missing from B's line")
        out, _ = self.line_as(seq.b, seq)
        events = out["line"]["events"]
        self.assertEqual([e["at"] for e in events], sorted(e["at"] for e in events))
        by_index = sorted(steps, key=lambda s: found[s]["_index"])
        self.assertEqual(by_index, [3, "3b", 4, 5, 6, 7, 8, 9, "9b", 10, "close", 11])
        for step in steps:
            self.assertEqual(who_of(found[step]), ACTOR[step], f"step {step}")

    def test_the_line_tells_the_steps_it_holds_in_the_order_they_happened(self):
        seq = self.build_sequence(announce=True)
        found = self.events_for(seq, seq.b)
        truth = [f["step"] for f in ground_truth(seq) if f["step"] in found]
        self.assertEqual(sorted(found, key=lambda s: found[s]["_index"]), truth)

    def test_every_step_sits_at_its_own_instant(self):
        seq = self.build_sequence()
        found = self.events_for(seq, seq.b)
        for step in (3, "3b", 4, 5, 6, 7, 8, 10, 11):
            self.assertEqual(
                datetime.fromisoformat(found[step]["at"]), seq.step_at[step], f"step {step}"
            )
        self.assertEqual(datetime.fromisoformat(found["close"]["at"]), seq.close_at)

    def test_the_thread_root_and_the_pms_answer_in_it_are_on_the_line(self):
        seq = self.build_sequence()
        found = self.events_for(seq, seq.b)
        self.assertEqual(found[3]["via"], "thread")
        self.assertEqual(found[3]["what"], "message")  # the thread root: A's request (step 3)
        self.assertEqual(found["3b"]["what"], "reply")
        self.assertEqual(found[7]["via"], "task")

    def test_the_task_card_in_the_thread_is_not_told_as_something_said(self):
        """The card (`Message.task` set) is the task's own record, told by the history row."""
        seq = self.build_sequence()
        out, _ = self.line_as(seq.b, seq)
        self.assertNotIn("created", self.plain([e.get("text") for e in out["line"]["events"]]))

    def test_the_pms_own_notice_is_a_seen_marker_and_nothing_more(self):
        seq = self.build_sequence()
        out, _ = self.line_as(seq.b, seq)  # the FIRST read: see the fixture's OBSERVER EFFECT
        found = locate_steps(seq, out["line"]["events"], viewer=seq.b)
        self.assertEqual(found[11]["summary"], "opened in your AI app")
        self.assertTrue(found[11]["mine"])
        self.assertEqual(datetime.fromisoformat(out["line"]["last_seen_at"]), seq.step_at[11])

    def test_reading_the_line_marks_the_task_seen_so_the_next_read_moves_last_seen(self):
        """Observer effect (not a gap: an AI-app read IS a look): the second read of the same
        line says the member last looked now, and `since_seen` restarts from there."""
        seq = self.build_sequence()
        first, _ = self.line_as(seq.b, seq)
        second, _ = self.line_as(seq.b, seq)
        self.assertEqual(datetime.fromisoformat(first["line"]["last_seen_at"]), seq.step_at[11])
        self.assertGreater(datetime.fromisoformat(second["line"]["last_seen_at"]), seq.step_at[11])

    def test_an_ai_app_write_is_on_the_line_twice_by_design(self):
        """A write over MCP leaves its history row AND the member's own marker (MIP-1198)."""
        seq = self.build_sequence()
        out, _ = self.line_as(seq.b, seq)
        at_assign = [e for e in out["line"]["events"] if e["at"][11:16] == "00:30"]
        self.assertEqual(sorted(e["source"] for e in at_assign), ["activity", "app"])

    def test_related_lines_add_nothing_for_a_task_with_no_neighbours(self):
        """The chat and the note are not items: related lines only join tasks."""
        seq = self.build_sequence()
        out, _ = self.line_as(seq.b, seq, include_related=True)
        self.assertEqual((out["related"], out["related_total"]), ([], 0))

    def test_what_the_line_cannot_place_is_counted_never_guessed_and_only_for_its_author(self):
        """B wrote step 2 outside the thread: one unattached message. A's step 1 is A's."""
        seq = self.build_sequence()
        out, _ = self.line_as(seq.b, seq)
        self.assertEqual(out["unattached"]["chat_messages"], 1)
        self.assertEqual(out["unattached"]["total"], 1)

    @unittest.expectedFailure
    def test_gap_1_steps_1_and_2_are_reachable_from_the_line(self):
        """GAP-1: A's question (1) and B's answer (2) came BEFORE the thread the task was made
        from, so no rule claims them: the PM's own line starts at step 3. Desired: the line
        reaches the conversation the task grew out of (steps 1 and 2)."""
        seq = self.build_sequence()
        found = self.events_for(seq, seq.b)
        self.assertIn(1, found)
        self.assertIn(2, found)


class TestTheLineAsTheDev(Story):
    """C does the work, is not in the client's chat, and only sees what the task holds."""

    def test_c_reads_steps_4_to_10_in_order_each_by_the_right_person(self):
        seq = self.build_sequence()
        found = self.events_for(seq, seq.c)
        steps = [4, 5, 6, 7, 8, 9, "9b", 10, "close"]
        self.assertEqual(set(steps) - set(found), set())
        order = sorted(steps, key=lambda s: found[s]["_index"])
        self.assertEqual(order, [4, 5, 6, 7, 8, 9, "9b", 10, "close"])
        for step in steps:
            self.assertEqual(who_of(found[step]), ACTOR[step], f"step {step}")
        self.assertEqual(found[5]["what"], "assigned_to_you")

    def test_c_does_not_see_the_client_chat_it_is_not_in(self):
        """Access runs before everything: a chat C cannot open adds nothing to C's line, not
        even a count."""
        seq = self.build_sequence()
        found = self.events_for(seq, seq.c)
        for step in (1, 2, 3, "3b"):
            self.assertNotIn(step, found)
        out, _ = self.line_as(seq.c, seq)
        self.assertEqual(out["unattached"]["total"], 0)
        self.assertNotIn("Client chat", json.dumps(out))

    def test_with_c_in_the_chat_c_gets_the_request_and_the_answer_in_the_thread(self):
        seq = self.build_sequence(c_in_gm=True)
        found = self.events_for(seq, seq.c)
        self.assertEqual({3, "3b"} - set(found), set())
        self.assertNotIn(1, found)  # the flat chat's lead-in is still on no line


class TestTheLineAsTheClientOnTheProject(Story):
    """V1: A is a member of the project, so A may open the task and read its whole line."""

    def test_a_reads_the_job_from_the_request_to_the_close(self):
        seq = self.build_sequence(client_on_project=True)
        found = self.events_for(seq, seq.a)
        steps = [3, "3b", 4, 5, 6, 7, 8, 9, "9b", 10, "close", 12]
        self.assertEqual(set(steps) - set(found), set())
        order = sorted(steps, key=lambda s: found[s]["_index"])
        self.assertEqual(order, [3, "3b", 4, 5, 6, 7, 8, 9, "9b", 10, "close", 12])
        for step in steps:
            self.assertEqual(who_of(found[step]), ACTOR[step], f"step {step}")

    def test_a_also_reads_the_developers_internal_comments_that_is_what_a_member_may_open(self):
        seq = self.build_sequence(client_on_project=True)
        found = self.events_for(seq, seq.a)
        self.assertIn(7, found)
        self.assertIn(8, found)

    def test_as_to_step_12_is_the_members_own_ai_app_read_nothing_tells_a_to_look(self):
        seq = self.build_sequence(client_on_project=True)
        out, _ = self.line_as(seq.a, seq)
        self.assertEqual(out["line"]["status"], "Closed")
        self.assertEqual(self.events_for(seq, seq.a)[12]["summary"], "opened in your AI app")

    def test_a_step_1_is_counted_as_unattached_and_b_s_step_2_is_not_counted_for_a(self):
        seq = self.build_sequence(client_on_project=True)
        out, _ = self.line_as(seq.a, seq)
        self.assertEqual(out["unattached"]["chat_messages"], 1)  # A's own step 1
        self.assertNotIn(1, self.events_for(seq, seq.a))
        self.assertNotIn(2, self.events_for(seq, seq.a))

    @unittest.expectedFailure
    def test_gap_1_a_sees_the_conversation_that_led_to_her_request(self):
        """GAP-1 (as A): the question (1) and the answer (2) are on no line, so even the client
        who asked cannot read them back from the task."""
        seq = self.build_sequence(client_on_project=True)
        found = self.events_for(seq, seq.a)
        self.assertIn(1, found)
        self.assertIn(2, found)


class TestTheLineAsTheClientOffTheProject(Story):
    """V2: A is only in the GM chat. The task is not hers to open."""

    def test_a_cannot_open_the_task_and_is_told_so_in_the_words_for_a_missing_task(self):
        seq = self.build_sequence(client_on_project=False)
        out, text = self.line_as(seq.a, seq)
        self.assertIsNone(out)
        self.assertIn(f"Task {seq.task.task_id} not found.", text)
        # The same words as a task that does not exist: nothing about it is given away.
        _, missing = self.line_as(seq.a, seq, task_id=999999)
        self.assertIn("Task 999999 not found.", missing)

    def test_the_dev_and_the_pm_read_the_same_line_whether_or_not_a_is_on_the_project(self):
        seq = self.build_sequence(client_on_project=False)
        self.assertEqual(
            {3, "3b", 4, 5, 6, 7, 8, 9, "9b", 10, "close", 11} - set(self.events_for(seq, seq.b)),
            set(),
        )
        self.assertEqual(
            {4, 5, 6, 7, 8, 9, "9b", 10, "close"} - set(self.events_for(seq, seq.c)), set()
        )

    def test_a_has_no_line_at_all_for_the_issue_she_raised(self):
        seq = self.build_sequence(client_on_project=False)
        self.assertEqual(self.events_for(seq, seq.a), {})


# --------------------------------------------------------------------------- #
# The chat shapes                                                              #
# --------------------------------------------------------------------------- #


class TestTheThreadedChatCarriesSteps1To3(Story):
    """When the whole exchange is ONE thread and the task is made from it, steps 1-3 ARE on the
    line (the linked thread claims every message in it, before the task existed)."""

    def test_the_question_the_answer_and_the_request_are_on_the_pms_line_in_order(self):
        seq = self.build_sequence(chat_shape="threaded")
        found = self.events_for(seq, seq.b)
        for step in (1, 2, 3, "3b"):
            self.assertIn(step, found, f"step {step}")
        self.assertEqual(
            sorted([1, 2, 3, "3b", 4], key=lambda s: found[s]["_index"]), [1, 2, 3, "3b", 4]
        )
        self.assertEqual([who_of(found[s]) for s in (1, 2, 3, "3b")], ["A", "B", "A", "B"])
        # The first message is the thread's root; the rest are replies.
        self.assertEqual(found[1]["what"], "message")
        self.assertEqual({found[2]["what"], found[3]["what"]}, {"reply"})

    def test_the_client_on_the_project_reads_them_too_and_nothing_is_unattached(self):
        seq = self.build_sequence(chat_shape="threaded", client_on_project=True)
        out, _ = self.line_as(seq.a, seq)
        found = locate_steps(seq, out["line"]["events"], viewer=seq.a)
        self.assertEqual({1, 2, 3} - set(found), set())
        self.assertEqual(out["unattached"]["total"], 0)


# --------------------------------------------------------------------------- #
# get_my_briefing                                                              #
# --------------------------------------------------------------------------- #


class TestTheBriefingForThePM(Story):
    def test_after_step_7_b_is_told_c_started_and_c_commented(self):
        seq = self.build_sequence(upto=7)
        brief = self.briefing_as(seq.b, seq)
        task_items = [i for i in brief["changed_for_me"]["items"] if i["type"] == "task"]
        self.assertEqual(len(task_items), 1)
        item = task_items[0]
        self.assertEqual(item["display_id"], seq.task.display_id)
        kinds = [
            (e["what"], re.sub(r"\s*</?workspace_content>\s*", "", str(e["by"])))
            for e in item["events"]
        ]
        self.assertEqual(kinds, [("status", "dev_c"), ("comment", "dev_c")])
        self.assertIn("S07", briefing_text(brief, "changed_for_me"))

    def test_after_step_10_b_is_told_the_close_last_with_the_comment_before_it(self):
        seq = self.build_sequence(upto=10)
        brief = self.briefing_as(seq.b, seq)
        item = next(i for i in brief["changed_for_me"]["items"] if i["type"] == "task")
        self.assertEqual(item["status"], "Closed")
        whats = [e["what"] for e in item["events"]]
        self.assertEqual(whats, ["status", "comment", "comment", "status"])
        self.assertTrue(item["events"][-1]["summary"].endswith("Closed"))
        self.assertIn("S10", briefing_text(brief, "changed_for_me"))

    def test_b_s_own_steps_are_not_news_to_b(self):
        seq = self.build_sequence(upto=10)
        text = briefing_text(self.briefing_as(seq.b, seq), "changed_for_me")
        for own in ("S02", "S3b", "S08", "assignee"):
            self.assertNotIn(own, text)

    def test_the_pms_notice_of_the_close_is_not_in_the_briefing_at_all(self):
        """Step 11 (B opened the closed task) is only a seen marker. B acted on the task today,
        so the briefing tells it once, as an item B changed, and does not list it under
        `looked_at`; the read is on the line (see `TestTheLineAsThePM`) and nowhere here."""
        seq = self.build_sequence(upto=11)
        did = self.briefing_as(seq.b, seq)["what_you_did"]["today"]
        self.assertEqual(did["looked_at"], [])
        self.assertEqual([i["display_id"] for i in did["items"]], [seq.task.display_id])

    def test_gap_3_a_task_comment_is_one_item_in_the_briefing_not_two(self):
        """GAP-3: a task comment is mirrored into the project chat as a thread reply, and the
        briefing lists that reply as a SECOND changed item (a 'thread' next to the task), so
        C's S07 and S10 are counted twice and the heat is inflated. (The line already told it
        once: it leaves the mirror out.)"""
        seq = self.build_sequence(upto=10)
        items = self.briefing_as(seq.b, seq)["changed_for_me"]["items"]
        holding_s07 = [i for i in items if "S07" in briefing_text({"x": i}, "x")]
        self.assertEqual(len(holding_s07), 1)

    @unittest.expectedFailure
    def test_gap_4_b_is_told_about_the_note_c_wrote_for_b_s_task(self):
        """GAP-4: the briefing reads edits to notes the member OWNS. C owns the task note, so
        step 9 reaches neither B (who reported and assigned the task) nor anyone else."""
        seq = self.build_sequence(upto=9)
        brief = self.briefing_as(seq.b, seq)
        self.assertIn("note_edit", briefing_text(brief, "changed_for_me"))

    @unittest.expectedFailure
    def test_gap_6_b_waits_on_c_and_the_briefing_says_so(self):
        """GAP-6: B handed the task to C and C has not finished, but `waiting_on_others` only
        lists tasks blocked by a dependency, so the PM's wait on C is not in it."""
        seq = self.build_sequence(upto=7)
        brief = self.briefing_as(seq.b, seq)
        self.assertIn(seq.task.display_id, briefing_text(brief, "waiting_on_others"))


class TestTheBriefingForTheDev(Story):
    def test_after_step_5_c_is_told_the_task_was_assigned_to_c_and_is_nudged_to_it(self):
        seq = self.build_sequence(upto=5)
        brief = self.briefing_as(seq.c, seq)
        item = next(i for i in brief["changed_for_me"]["items"] if i["type"] == "task")
        self.assertTrue(item["assigned_to_me"])
        self.assertEqual([e["what"] for e in item["events"]], ["change", "assigned_to_you"])
        self.assertEqual([s["display_id"] for s in brief["suggested_next"]], [seq.task.display_id])

    def test_after_step_8_b_s_reply_reaches_c_as_news_and_as_something_waiting_on_c(self):
        seq = self.build_sequence(upto=8)
        brief = self.briefing_as(seq.c, seq)
        item = next(i for i in brief["changed_for_me"]["items"] if i["type"] == "task")
        self.assertIn("S08", briefing_text({"x": item}, "x"))
        mentions = brief["waiting_on_me"]["mentions"]
        self.assertEqual([m["source"] for m in mentions], ["task_comment"])
        self.assertIn("S08", briefing_text({"x": mentions}, "x"))

    def test_c_s_own_steps_are_not_news_to_c(self):
        seq = self.build_sequence(upto=8)
        text = briefing_text(self.briefing_as(seq.c, seq), "changed_for_me")
        self.assertNotIn("S07", text)


class TheClientsBriefing:
    """Mixin: what A's briefing says. V1 and V2 differ only by `client_on_project`."""

    client_on_project = True

    def test_a_learns_of_the_task_from_the_card_in_the_thread_a_started(self):
        """Steps 3b and 4 reach A through the thread A started: the PM's answer and the card
        the app posts when the task is made. (A's own step 3 is not news to A.)"""
        seq = self.build_sequence(upto=4, client_on_project=self.client_on_project)
        brief = self.briefing_as(seq.a, seq)
        self.assertEqual([i["type"] for i in brief["changed_for_me"]["items"]], ["thread"])
        text = briefing_text(brief, "changed_for_me")
        self.assertIn("S3b", text)
        self.assertIn(f"Task {seq.task.display_id} created", text)

    def test_a_is_not_told_the_internal_work(self):
        seq = self.build_sequence(upto=10, client_on_project=self.client_on_project)
        text = briefing_text(self.briefing_as(seq.a, seq))
        for internal in ("S07", "S08", "S10"):
            self.assertNotIn(internal, text)

    def test_with_an_announcement_the_pms_message_reaches_a(self):
        """The one thing that tells a client the issue is solved today: the PM says so in the
        thread and @mentions her. It is news and it is waiting on her."""
        seq = self.build_sequence(
            upto="12a", client_on_project=self.client_on_project, announce=True
        )
        brief = self.briefing_as(seq.a, seq)
        self.assertIn("S12a", briefing_text(brief, "changed_for_me"))
        self.assertEqual([m["source"] for m in brief["waiting_on_me"]["mentions"]], ["chat"])
        self.assertIn("S12a", briefing_text(brief, "waiting_on_me"))


class TestTheBriefingForTheClientOnTheProject(TheClientsBriefing, Story):
    """V1."""

    client_on_project = True

    def test_a_s_own_read_of_the_closed_task_shows_in_what_a_did(self):
        seq = self.build_sequence(upto=12, client_on_project=True)
        looked = self.briefing_as(seq.a, seq)["what_you_did"]["today"]["looked_at"]
        self.assertEqual([row["display_id"] for row in looked], [seq.task.display_id])

    @unittest.expectedFailure
    def test_gap_9_a_top_level_answer_to_the_clients_question_reaches_her_briefing(self):
        """GAP-9: in a flat chat B answers A's question (step 2) with an ordinary message, not a
        thread reply and not an @mention, and the briefing reads only those two, so nothing in
        A's briefing says B answered her. Desired: a reply to the member's own message, or any
        message in a chat they asked in, since they last looked, is news. (A product call: the
        app's unread badge covers it for a person; the briefing is what an AI app reads.)"""
        seq = self.build_sequence(upto=2)
        self.assertIn("S02", briefing_text(self.briefing_as(seq.a, seq)))

    @unittest.expectedFailure
    def test_gap_2_the_client_on_the_project_is_told_the_task_closed(self):
        """GAP-2 (V1): A asked for Y in a thread and is on the project, but A holds no role on
        the task (not assignee, reporter, collaborator or commenter), so step 10 never reaches
        A's briefing. Desired: the person whose request made the task hears it closed."""
        seq = self.build_sequence(upto=10, client_on_project=True)
        text = briefing_text(self.briefing_as(seq.a, seq))
        self.assertIn("Closed", text)


class TestTheBriefingForTheClientOffTheProject(TheClientsBriefing, Story):
    """V2."""

    client_on_project = False

    @unittest.expectedFailure
    def test_gap_2_the_client_off_the_project_is_told_the_task_closed(self):
        """GAP-2 (V2): A is only in the GM chat. Nothing in the chat or the briefing says the
        task closed unless the PM writes it. Desired: the thread the task came from hears the
        close, so A (in that thread) is told without anyone typing."""
        seq = self.build_sequence(upto=10, client_on_project=False)
        text = briefing_text(self.briefing_as(seq.a, seq))
        self.assertIn("Closed", text)

    @unittest.expectedFailure
    def test_gap_2_the_origin_thread_hears_the_close(self):
        """GAP-2 (the thread), ONLY IF the fix chosen is a post in the origin thread (the other
        option is the briefing: then delete this test): after the close nothing is posted in the
        thread the task was created from; its last word is still the card that says 'created'."""
        seq = self.build_sequence(upto=10, client_on_project=False)
        after = Message.objects.filter(
            thread_root_id=seq.thread_root.id, ts_sent_at__gte=seq.close_at
        )
        self.assertTrue(after.exists())


# --------------------------------------------------------------------------- #
# The notes on a line                                                          #
# --------------------------------------------------------------------------- #


class TestTheTaskNoteOnTheLine(Story):
    def test_both_versions_are_on_the_line_credited_to_c_and_named_by_the_note(self):
        seq = self.build_sequence()
        found = self.events_for(seq, seq.b)
        for step in (9, "9b"):
            self.assertEqual(found[step]["what"], "note_edit")
            self.assertEqual(found[step]["via"], "note_task")
            self.assertEqual(who_of(found[step]), "C")
            self.assertIn("Y: progress summary", found[step]["note"])
        self.assertLess(found[9]["_index"], found["9b"]["_index"])

    @unittest.expectedFailure
    def test_gap_5_a_note_event_says_what_the_note_says(self):
        """GAP-5: step 9 is on the line only as 'edited the note' with the note's title; what C
        wrote (the progress summary) is not on the line, unlike a comment, which carries a
        snippet. Desired: a short snippet of the version, as comments have."""
        seq = self.build_sequence()
        event = self.events_for(seq, seq.b)[9]
        self.assertIn("Summary of Y", json.dumps(event))


# --------------------------------------------------------------------------- #
# The in-app agent's tools                                                     #
# --------------------------------------------------------------------------- #


class TestWhatTheInAppToolsReturnAndInWhichOrder(Story):
    """No model runs: the in-app agent's own read tools, called as B, and the order of each.

    | tool                          | order of its output                        | text of |
    | `fetch_task`                  | comments oldest first (the last 20)        | comments, body |
    | `get_task_history`            | newest first                               | field changes (a comment is a row with no text) |
    | `fetch_chat_thread` (thread)  | the root, then replies oldest first        | messages |
    | `fetch_chat_thread` (channel) | the newest 50, oldest first                | messages |
    | `fetch_note_history`          | versions newest first                      | a preview of each |
    | `search_knowledge_base`       | by relevance, one row per entity (not run) | snippets, `updated_at` per entity |
    """

    def ctx(self, user):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))

    def run_tool(self, user, name, **args):
        return REGISTRY[name].run(args, self.ctx(user))

    def test_get_task_history_is_newest_first(self):
        seq = self.build_sequence()
        rows = self.run_tool(seq.b, "get_task_history", task_id=seq.task.task_id)["history"]
        ats = [r["at"] for r in rows]
        self.assertEqual(ats, sorted(ats, reverse=True))
        self.assertEqual([r["action"] for r in rows][:2], ["status_changed", "comment_added"])
        self.assertEqual(rows[-1]["action"], "created")

    def test_get_task_history_names_a_comment_but_does_not_carry_its_text(self):
        seq = self.build_sequence()
        rows = self.run_tool(seq.b, "get_task_history", task_id=seq.task.task_id)["history"]
        comment_rows = [r for r in rows if r["action"] == "comment_added"]
        self.assertEqual(len(comment_rows), 3)
        self.assertNotIn("S07", json.dumps(rows))

    def test_fetch_task_lists_the_comments_oldest_first_with_their_text(self):
        seq = self.build_sequence()
        out = self.run_tool(seq.b, "fetch_task", task_id=seq.task.task_id)
        comments = out["comments"]
        self.assertEqual([c["ts"] for c in comments], sorted(c["ts"] for c in comments))
        self.assertEqual(len(comments), 3)
        for token, comment in zip(("S07", "S08", "S10"), comments):
            self.assertIn(token, comment["text"])

    def test_fetch_chat_thread_reads_the_thread_root_first_then_replies_oldest_first(self):
        seq = self.build_sequence()
        out = self.run_tool(
            seq.b,
            "fetch_chat_thread",
            chat_type="gm",
            chat_id=str(seq.gm.id),
            thread_id=str(seq.thread_root.id),
        )
        messages = out["messages"]
        self.assertTrue(messages[0]["is_thread_anchor"])
        self.assertIn("S03", messages[0]["text"])
        ats = [m["ts"] for m in messages]
        self.assertEqual(ats, sorted(ats))

    def test_fetch_chat_thread_reads_the_channel_oldest_first_so_steps_1_and_2_are_there(self):
        """The in-app agent CAN read steps 1-3 if it knows the chat: the channel page."""
        seq = self.build_sequence()
        out = self.run_tool(seq.b, "fetch_chat_thread", chat_type="gm", chat_id=str(seq.gm.id))
        texts = [
            re.sub(r"</?workspace_content>", "", m["text"]).split()[0] for m in out["messages"]
        ]
        self.assertEqual(texts, ["S01", "S02", "S03"])

    def test_fetch_note_history_lists_the_versions_newest_first(self):
        seq = self.build_sequence()
        out = self.run_tool(
            seq.b, "fetch_note_history", note_id=seq.note["note_id"], note_type="task"
        )
        self.assertEqual([v["version_no"] for v in out["versions"]], [2, 1])

    def test_the_in_app_agent_cannot_read_a_task_it_may_not_open(self):
        from origin.search_engine.agent.tools.base import ToolForbidden  # noqa: PLC0415

        seq = self.build_sequence(client_on_project=False)
        with self.assertRaises(ToolForbidden):
            self.run_tool(seq.a, "fetch_task", task_id=seq.task.task_id)

    @unittest.expectedFailure
    def test_gap_7_fetch_task_names_the_thread_and_the_note_of_the_task(self):
        """GAP-7: `fetch_task` returns the task, its comments and its deliveries but neither the
        chat thread the task came from (`thread_id`, `chat_id`) nor its task note, so an agent
        that starts from the task cannot get to steps 1-3 or to step 9 except by guessing a
        search. Desired: the task says where its thread and note are."""
        seq = self.build_sequence()
        out = self.run_tool(seq.b, "fetch_task", task_id=seq.task.task_id)
        blob = json.dumps(out, default=str)
        self.assertIn(str(seq.thread_root.id), blob)
        self.assertIn("note", " ".join(out.keys()))

    @unittest.expectedFailure
    def test_gap_8_the_in_app_agent_has_one_tool_that_tells_the_task_as_a_line(self):
        """GAP-8: `get_item_line` is MCP-only. The in-app agent has to merge `fetch_task`
        (comments oldest first), `get_task_history` (newest first), `fetch_chat_thread` (oldest
        first) and `fetch_note_history` (newest first) into one time order itself. Desired: it
        can read the line. (Model-visible: waits for the December eval.)"""
        self.assertIn("get_item_line", REGISTRY)

    def test_today_the_line_is_an_mcp_only_tool(self):
        """Delete this guard together with `test_gap_8_...`: the fix flips both."""
        self.assertIn("get_item_line", MCP_ONLY_REGISTRY)
        self.assertNotIn("get_item_line", REGISTRY)
