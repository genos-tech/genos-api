"""What a busy member reads in a built-in report (MIP-1417): one line per task, no noise, and
nothing at all on a day with nothing to say.

Each test is a fact a member could see in the Inbox card:

  * a task is listed ONCE per report, in the section that matters most, and what the later section
    would have said is not lost;
  * the sentence under a task does not repeat what the row's chips already show;
  * a quiet scheduled run files nothing, sends no push and posts nothing in a chat, while
    "Run now" still answers; a run that paid for AI is always filed;
  * wording: numbers and plurals are right, windows read "7 days" not "168 hours", no "0 pending";
  * the urgent sections of the project and milestone reports come first.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase

from origin.models.chat.unified_models import Message
from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.common.schedule_models import MemberSchedule
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskDependency
from origin.services import schedule_delivery, schedules
from origin.services.schedule_jobs.report_kit import counted, dedupe_tasks, span, status_line
from origin.tests.test_schedule_chat_delivery import ChatBase
from origin.tests.test_schedule_jobs import JobBase


def task_row(task_id, text="t", detail=None, kind="task"):
    row = {"text": text, "kind": kind}
    if kind == "task":
        row["task_id"] = task_id
    if detail:
        row["detail"] = detail
    return row


class TestTheWords(SimpleTestCase):
    def test_a_window_reads_in_days_from_two_days_up(self):
        self.assertEqual(span(1), "1 hour")
        self.assertEqual(span(24), "24 hours")
        self.assertEqual(span(36), "36 hours")
        self.assertEqual(span(48), "2 days")
        self.assertEqual(span(168), "7 days")
        self.assertEqual(span(170), "170 hours")

    def test_a_count_and_its_noun_agree(self):
        self.assertEqual(counted(1, "task"), "1 task")
        self.assertEqual(counted(2, "task"), "2 tasks")
        self.assertEqual(counted(0, "task"), "0 tasks")
        self.assertEqual(counted(2, "new task"), "2 new tasks")
        self.assertEqual(counted(2, "person", "people"), "2 people")

    def test_the_status_line_leaves_out_a_status_nobody_has(self):
        line = status_line(17, {"Open": 9, "WIP": 5, "Blocked": 1, "Pending": 0, "Closed": 2})
        self.assertEqual(line, "17 tasks: 9 open, 5 wip, 1 blocked, 2 closed")
        self.assertEqual(status_line(1, {"Open": 1}), "1 task: 1 open")
        self.assertEqual(status_line(0, {}), "0 tasks")


class TestOneLinePerTask(SimpleTestCase):
    def sections(self):
        return [
            {"heading": "Start with", "key": "start_with", "items": [task_row(1, "one")]},
            {
                "heading": "Overdue",
                "key": "overdue",
                "items": [
                    task_row(1, "one again"),
                    task_row(2, "two"),
                    {"text": "and 3 more", "kind": "more"},
                ],
            },
            {
                "heading": "Changed",
                "key": "changed",
                "items": [
                    task_row(2, "two again", detail="commented by mina"),
                    task_row(3, "three"),
                ],
            },
        ]

    def test_the_first_section_keeps_the_row_and_the_later_one_loses_it(self):
        out = dedupe_tasks(self.sections())
        self.assertEqual(
            [[i["text"] for i in s["items"]] for s in out],
            [["one"], ["two", "and 3 more"], ["three"]],
        )

    def test_what_the_dropped_row_said_goes_to_the_kept_one_when_it_has_no_sentence(self):
        out = dedupe_tasks(self.sections())
        two = out[1]["items"][0]
        self.assertEqual(two["detail"], "commented by mina")

    def test_a_kept_sentence_is_not_replaced(self):
        sections = self.sections()
        sections[1]["items"][1]["detail"] = "waiting for the vendor"
        out = dedupe_tasks(sections)
        self.assertEqual(out[1]["items"][0]["detail"], "waiting for the vendor")

    def test_the_priority_order_decides_who_keeps_it_and_the_display_order_stays(self):
        out = dedupe_tasks(self.sections(), ("changed", "overdue", "start_with"))
        # "changed" is asked first, so it keeps tasks 2 and 3; "overdue" keeps task 1, and
        # "start_with" is left with nothing and goes. The sections stay in their display order.
        self.assertEqual([s["key"] for s in out], ["overdue", "changed"])
        self.assertEqual([i["task_id"] for i in out[1]["items"]], [2, 3])
        self.assertEqual([i["text"] for i in out[0]["items"]], ["one again", "and 3 more"])

    def test_a_section_left_with_nothing_goes_and_a_line_that_is_not_a_task_stays(self):
        sections = [
            {"heading": "A", "key": "a", "items": [task_row(1)]},
            {
                "heading": "B",
                "key": "b",
                "items": [task_row(1), {"text": "a todo", "kind": "todo"}],
            },
            {"heading": "C", "key": "c", "items": [task_row(1)]},
        ]
        out = dedupe_tasks(sections)
        self.assertEqual([s["key"] for s in out], ["a", "b"])
        self.assertEqual(out[1]["items"], [{"text": "a todo", "kind": "todo"}])


class TestMorningPlan(JobBase):
    def late(self, title, days, **kw):
        return self.make_task(title, due_date=self.local_today() - timedelta(days=days), **kw)

    def decoys(self):
        """Three tasks that outrank everything else (long overdue, and someone commented), so they
        fill "Start with" and the sections below are looked at on their own."""
        out = []
        for n in range(3):
            task = self.late(f"Decoy {n}", 40)
            self.comment(task, self.user2, "ping", at=self.ago(hours=1))
            out.append(task)
        return out

    def links(self, report):
        return [i["link"] for s in report["sections"] for i in s["items"] if i.get("link")]

    def test_a_task_that_is_overdue_changed_and_blocked_is_one_line(self):
        self.decoys()
        task = self.late("Everything at once", 2, status="Blocked")
        self.comment(task, self.user2, "any news?", at=self.ago(hours=2))
        blocker = self.make_task("Vendor", assignee=self.user2, status="WIP")
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=task)
        report = self.run_job("what_to_do_today")
        links = self.links(report)
        self.assertEqual(len(links), len(set(links)), "a task is listed twice")
        row = self.row(report, self.task_link(task))
        self.assertTrue(row["detail"])  # what the other sections said is not lost

    def test_what_the_changed_section_said_moves_to_the_row_that_keeps_the_task(self):
        self.decoys()
        task = self.late("Overdue and commented", 2)
        self.comment(task, self.user2, "hello", at=self.ago(hours=2))
        report = self.run_job("what_to_do_today")
        row = self.row(report, self.task_link(task))
        self.assertIn("commented by otheruser", row["detail"])
        self.assertEqual([s["heading"] for s in report["sections"]][:2], ["Start with", "Overdue"])
        self.assertNotIn("Changed in the last 24 hours", [s["heading"] for s in report["sections"]])

    def test_the_sentence_does_not_repeat_what_the_chips_show(self):
        self.make_task(
            "High and late", due_date=self.local_today() - timedelta(days=4), priority="High"
        )
        report = self.run_job("what_to_do_today")
        (row,) = report["sections"][0]["items"]
        self.assertNotIn("detail", row)  # only "overdue by 4 days; priority High": all on chips
        self.assertNotIn("overdue by", row["text"])
        self.assertNotIn("priority High", row["text"])
        self.assertIn("4 days late", row["text"])  # the text a plain reader gets is kept

    def test_a_reason_the_chips_do_not_show_is_kept(self):
        task = self.late("Late and talked about", 4, priority="High")
        self.comment(task, self.user2, "hello", at=self.ago(hours=2))
        report = self.run_job("what_to_do_today")
        row = self.row(report, self.task_link(task))
        self.assertIn("comment", row["detail"])
        self.assertNotIn("overdue by", row["detail"])
        self.assertNotIn("priority", row["detail"])

    def test_overdue_goes_by_priority_before_how_late_it_is(self):
        self.decoys()
        low = self.late("Low, a month late", 30, priority="Low")
        critical = self.late("Critical, yesterday", 1, priority="Critical")
        report = self.run_job("what_to_do_today")
        overdue = [i["link"] for i in self.section(report, "Overdue")]
        self.assertLess(overdue.index(self.task_link(critical)), overdue.index(self.task_link(low)))

    def test_a_chip_is_dropped_with_the_section_it_would_name(self):
        self.late("One late", 1)
        report = self.run_job("what_to_do_today")
        self.assertEqual([s["heading"] for s in report["sections"]], ["Start with"])
        self.assertNotIn("overdue", {s["key"] for s in report["stats"]})
        self.assertIn("1 overdue", report["summary"])  # the summary still says it

    def test_a_nothing_to_do_day_is_quiet(self):
        report = self.run_job("what_to_do_today")
        self.assertTrue(report["quiet"])
        self.assertEqual(report["sections"], [])
        self.assertEqual(report["summary"], "Nothing is overdue, due today or waiting on you.")

    def test_a_day_with_a_task_is_not_quiet(self):
        self.late("Late", 1)
        self.assertFalse(self.run_job("what_to_do_today")["quiet"])


class TestTheOtherReports(JobBase):
    def test_what_is_left_says_its_numbers_in_the_right_plural(self):
        done = self.make_task("Shipped", status="Closed")
        wip = self.make_task("Open one", status="WIP")
        self.activity(done, self.user, "status_changed", "WIP", "Closed", at=self.ago(days=2))
        self.activity(wip, self.user, "status_changed", "Open", "WIP", at=self.ago(days=2))
        report = self.run_job("what_is_left")
        self.assertEqual(
            report["summary"], "1 task still open, 1 task closed from the last 7 days."
        )
        wip2 = self.make_task("Open two", status="WIP")
        self.activity(wip2, self.user, "status_changed", "Open", "WIP", at=self.ago(days=3))
        self.assertIn("2 tasks still open", self.run_job("what_is_left")["summary"])
        self.assertTrue(self.run_job("what_is_left", days=1)["summary"].startswith("Nothing saved"))
        self.assertIn("last day", self.run_job("what_is_left", days=1)["summary"])

    def test_a_week_with_nothing_is_quiet_for_what_is_left_and_what_you_did(self):
        self.assertTrue(self.run_job("what_is_left")["quiet"])
        self.assertTrue(self.run_job("what_you_did", day="today")["quiet"])

    def test_a_day_with_work_is_not_quiet_for_what_you_did(self):
        task = self.make_task("Done today", status="Closed")
        self.activity(task, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=1))
        report = self.run_job("what_you_did", day="today")
        self.assertFalse(report["quiet"])

    def test_a_task_the_member_changed_and_their_ai_app_opened_is_one_line(self):
        from origin.models.common.member_event_models import MemberEvent  # noqa: PLC0415

        task = self.make_task("Both ways", status="WIP")
        self.activity(task, self.user, "status_changed", "Open", "WIP", at=self.ago(hours=1))
        when = self.now - timedelta(hours=1)
        MemberEvent.objects.create(
            user=self.user,
            team=self.team,
            kind="read",
            object_type="task",
            object_id=str(task.task_id),
            client="Claude",
            ts=when,
            bucket=int(when.timestamp() // 600),
        )
        report = self.run_job("what_you_did", day="today")
        links = [i["link"] for s in report["sections"] for i in s["items"] if i.get("link")]
        self.assertEqual(links.count(self.task_link(task)), 1)
        self.assertEqual([s["heading"] for s in report["sections"]], ["Tasks"])


class TestProjectUpdates(JobBase):
    def run_pu(self, **kw):
        return self.run_job("project_updates", project_id=self.project.project_id, **kw)

    def test_a_task_that_slipped_and_moved_is_one_line_under_slipped(self):
        today = self.local_today()
        task = self.make_task("Late and started", status="WIP", due_date=today - timedelta(days=1))
        self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=2))
        report = self.run_pu()
        self.assertEqual(self.row(report, self.task_link(task))["detail"], "went overdue")
        self.assertEqual([s["heading"] for s in report["sections"]], ["Slipped", "Where it stands"])
        self.assertEqual(report["summary"], "1 slipped in the last 7 days.")

    def test_a_blocked_task_is_not_also_slipped(self):
        today = self.local_today()
        task = self.make_task(
            "Late and blocked", status="Blocked", due_date=today - timedelta(days=1)
        )
        self.activity(task, self.user2, "status_changed", "WIP", "Blocked", at=self.ago(hours=2))
        report = self.run_pu()
        self.assertEqual([s["heading"] for s in report["sections"]], ["Blocked", "Where it stands"])
        self.assertEqual(report["summary"], "1 blocked in the last 7 days.")

    def test_the_urgent_sections_come_first_and_the_counts_last(self):
        done = self.make_task("Done", status="Closed")
        self.activity(done, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=3))
        stuck = self.make_task("Stuck", status="Blocked")
        self.activity(stuck, self.user2, "status_changed", "WIP", "Blocked", at=self.ago(hours=2))
        report = self.run_pu()
        self.assertEqual(
            [s["heading"] for s in report["sections"]], ["Blocked", "Done", "Where it stands"]
        )

    def test_the_counts_leave_out_a_status_nobody_has(self):
        self.make_task("Only one", status="WIP")
        stands = self.section(self.run_pu(), "Where it stands")
        self.assertEqual(stands[0]["text"], "2 tasks: 1 open, 1 wip")

    def test_a_week_with_nothing_moved_is_quiet_but_run_now_still_answers(self):
        self.make_task("Sitting there", status="Open")
        report = self.run_pu()
        self.assertTrue(report["quiet"])
        self.assertEqual(report["summary"], "Nothing moved in the last 7 days.")
        self.assertEqual([s["key"] for s in report["sections"]], ["where_it_stands"])


class TestMilestoneUpdates(JobBase):
    def setUp(self):
        super().setUp()
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v1", status="Open"
        )

    def run_mu(self, **kw):
        return self.run_job("milestone_updates", milestone_id=self.milestone.milestone_id, **kw)

    def test_the_window_reads_in_days_and_the_count_agrees(self):
        task = self.make_task("Work", milestone=self.milestone)
        self.comment(task, self.user2, "hi", at=self.ago(hours=1))
        report = self.run_mu()
        self.assertEqual(report["summary"], "1 task changed in the last 7 days; 1 in all.")
        self.assertIn("Changed in the last 7 days", [s["heading"] for s in report["sections"]])
        self.assertIn("2 days", self.run_mu(hours=48)["summary"])

    def test_a_task_that_changed_and_is_ready_is_listed_once_under_changed(self):
        task = self.make_task("Work", milestone=self.milestone)
        self.comment(task, self.user2, "hi", at=self.ago(hours=1))
        report = self.run_mu()
        links = [i["link"] for s in report["sections"] for i in s["items"] if i.get("link")]
        self.assertEqual(links, [self.task_link(task)])
        self.assertNotIn("Ready to start", [s["heading"] for s in report["sections"]])

    def test_what_waits_on_a_blocker_comes_first_and_the_counts_last(self):
        waiting = self.make_task("Waiting", milestone=self.milestone, status="Blocked")
        blocker = self.make_task("Their part", assignee=self.user2, milestone=self.milestone)
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=waiting)
        self.comment(blocker, self.user2, "on it", at=self.ago(hours=1))
        report = self.run_mu()
        headings = [s["heading"] for s in report["sections"]]
        self.assertEqual(headings[0], "Waiting on blockers")
        self.assertEqual(headings[-1], "Where it stands")
        self.assertFalse(report["quiet"])

    def test_nothing_changed_and_nothing_blocked_is_quiet(self):
        self.make_task("Waiting for its turn", milestone=self.milestone)
        report = self.run_mu()
        self.assertTrue(report["quiet"])
        self.assertEqual(report["summary"], "0 tasks changed in the last 7 days; 1 in all.")

    def test_the_counts_leave_out_a_status_nobody_has(self):
        self.make_task("A", milestone=self.milestone, status="WIP")
        stands = self.section(self.run_mu(), "Where it stands")
        self.assertNotIn("pending", stands[0]["text"])
        self.assertNotIn("blocked", stands[0]["text"])


class TestTeamAndChatReports(JobBase):
    def test_team_activity_says_one_task_closed_and_has_no_bare_chip(self):
        task = self.make_task("Shipped", status="Closed")
        self.activity(task, self.user2, "status_changed", "WIP", "Closed", at=self.ago(hours=2))
        report = self.run_job("team_activity")
        self.assertIn("1 task closed", report["summary"])
        self.assertNotIn("1 tasks", report["summary"])
        stat_keys = {s["key"] for s in report["stats"]}
        self.assertNotIn(
            "closed", stat_keys
        )  # no section carries its name: it would be a bare number
        sections = {s["key"] for s in report["sections"]}
        self.assertTrue(stat_keys <= sections, (stat_keys, sections))
        self.assertFalse(report["quiet"])

    def test_team_activity_does_not_list_a_quiet_project(self):
        quiet = ProjectMaster.objects.create(
            team=self.team,
            project_name="Sleepy project",
            code="SLP",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=quiet, attendee=self.user)
        task = self.make_task("Shipped", status="Closed")
        self.activity(task, self.user2, "status_changed", "WIP", "Closed", at=self.ago(hours=2))
        report = self.run_job("team_activity")
        texts = " ".join(i["text"] for s in report["sections"] for i in s["items"])
        self.assertIn("Website Redesign", texts)
        self.assertNotIn("Sleepy project", texts)
        self.assertNotIn("had no new or closed", texts)
        self.assertNotIn("(s)", json.dumps(report))

    def test_a_window_with_nothing_is_quiet_for_chat_catch_up(self):
        chats = self.run_job("chat_catch_up")
        self.assertTrue(chats["quiet"])
        self.assertEqual(chats["summary"], "No group chat was busy in the last 24 hours.")

    def test_busy_chats_are_named_with_a_plural_that_agrees(self):
        channel = self.channel([self.user, self.user2], title="Design")
        for n in range(4):
            self.message(channel, self.user2, f"m{n}", at=self.ago(hours=1 + n))
        report = self.run_job("chat_catch_up")
        self.assertEqual(report["summary"], "1 busy group chat in the last 24 hours.")
        self.assertFalse(report["quiet"])
        (section,) = report["sections"]
        self.assertEqual(section["key"], "chats")  # the headline chip is named by this heading
        self.assertEqual({s["key"] for s in report["stats"]}, {"chats"})


class TestAQuietDayFilesNothing(ChatBase):
    def inbox_schedule(self, job="what_to_do_today", **params):
        return MemberSchedule.objects.create(
            user=self.user,
            team=self.team,
            name="Plan",
            job=job,
            params=params,
            frequency="daily",
            time_of_day="09:00",
            tz_used="Asia/Tokyo",
            deliver_push=True,
        )

    def filed(self):
        return InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT)

    def tick(self, sched):
        at = self.now.replace(microsecond=0)
        with (
            mock.patch("django.utils.timezone.now", return_value=self.now),
            mock.patch("origin.services.schedule_delivery.dispatch_push_for_inbox_item") as push,
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = schedules._execute(sched, run_at=at)
        return result, push

    def test_a_scheduled_run_with_nothing_to_report_files_nothing_and_pushes_nothing(self):
        sched = self.inbox_schedule()
        result, push = self.tick(sched)
        self.assertEqual(result, ("ok", ""))  # it ran; that is not a failure
        self.assertEqual(self.filed().count(), 0)
        push.assert_not_called()
        sched.refresh_from_db()
        self.assertEqual(sched.last_result["items"], 0)  # the Schedules page says it ran
        self.assertTrue(sched.last_result["title"].startswith("What to do today"))

    def test_the_same_quiet_run_twice_is_still_nothing(self):
        sched = self.inbox_schedule()
        self.tick(sched)
        self.tick(sched)
        self.assertEqual(self.filed().count(), 0)

    def test_run_now_still_files_the_answer(self):
        sched = self.inbox_schedule()
        with (
            mock.patch("django.utils.timezone.now", return_value=self.now),
            mock.patch("origin.services.schedule_delivery.dispatch_push_for_inbox_item"),
        ):
            self.assertEqual(schedules._execute(sched), ("ok", ""))
        self.assertEqual(self.filed().count(), 1)
        self.assertEqual(
            self.filed().get().item_body["text"], "Nothing is overdue, due today or waiting on you."
        )

    def test_a_day_with_something_to_do_is_filed_and_pushed(self):
        self.make_task("Late", due_date=self.local_today() - timedelta(days=1))
        sched = self.inbox_schedule()
        result, push = self.tick(sched)
        self.assertEqual(self.filed().count(), 1)
        push.assert_called_once()

    def test_a_job_that_does_not_say_quiet_is_filed_as_before(self):
        sched = self.inbox_schedule()
        report = {"title": "Anything", "summary": "", "sections": []}
        item = schedule_delivery.deliver(sched, report, run_at=self.now)
        self.assertIsNotNone(item)
        self.assertEqual(item.item_body["text"], schedule_delivery.DEFAULT_SUMMARY)

    def test_a_quiet_report_that_paid_for_ai_is_never_hidden(self):
        report = {"title": "t", "summary": "", "sections": [], "quiet": True}
        self.assertTrue(schedule_delivery.is_quiet_tick(report, self.now))
        self.assertFalse(
            schedule_delivery.is_quiet_tick({**report, "ai_request_id": "r1"}, self.now)
        )
        self.assertFalse(schedule_delivery.is_quiet_tick(report, None))  # "Run now"
        self.assertFalse(schedule_delivery.is_quiet_tick({**report, "quiet": False}, self.now))

    def test_a_quiet_project_report_posts_nothing_in_a_chat_on_a_scheduled_run(self):
        sched = self.save(self.pm)
        self.assertEqual(self.run_it(sched, run_at=self.now.replace(microsecond=0))[0], "ok")
        self.assertEqual(self.posted().count(), 0)
        self.run_it(sched)  # "Run now"
        self.assertEqual(self.posted().count(), 1)
        self.assertEqual(Message.objects.filter(metadata__has_key="scheduleReport").count(), 1)

    def test_a_chat_the_report_may_not_be_posted_in_still_says_so_on_a_quiet_day(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat)
        # Somebody who is not on the project joins the chat since: the rule is asked at every run,
        # before the quiet check, so the owner is told why nothing arrives.
        from origin.models.chat.unified_models import ChannelMember  # noqa: PLC0415

        ChannelMember.objects.create(channel=chat, user=self.outsider)
        status, error = self.run_it(sched, run_at=self.now.replace(microsecond=0))
        self.assertEqual(status, "failed")
        self.assertTrue(error)
