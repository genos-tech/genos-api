"""A report that does not repeat itself, and the two answers a member can give it (MIP-1211).

What is pinned, each by a test that fails when its guard is removed:

  * a task row whose state did not change since the member's last OPENED report of the same job is
    held back and counted ("N unchanged since your last report": summary line and a stat), and the
    hard rules (due within 2 days, active sprint with a due date, newly blocked, newly overdue,
    changed by somebody else, one of the top tasks) list it every time;
  * the member's answer beats every rule until the task changes: `on_it` for 7 days, `not_important`
    until the signature changes; a mark with no signature (made before) never touches a report;
  * the memory is the previous report in the Inbox: deleted ones count, unread ones, other jobs',
    other members' and month-old ones do not; nothing new is stored but `seen` in `item_optionals`;
  * `list_my_inbox` shows the model exactly the old report shape;
  * the answers: the REST endpoint takes `on_it`, returns `previous`, records the signature and the
    source, refuses with recording off; the connected-app tool is unchanged;
  * with no marks and no memory a report is what it was.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest import mock

from django.test import SimpleTestCase

from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.common.member_importance_models import (
    MemberImportanceMark,
    purge_for_user,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.list_my_inbox import _model_body
from origin.services import importance, report_memory, schedule_delivery, schedules
from origin.services.schedule_jobs import JOBS
from origin.services.schedule_jobs.report_kit import TaskFacts
from origin.tests.test_schedule_jobs import JobBase

JOB = "what_to_do_today"


def facts(**kw):
    base = dict(
        task_id=1,
        number=1,
        project_id=1,
        project_name="P",
        project_code="PRJ",
        due_date=None,
        status="Open",
        priority="Normal",
    )
    return TaskFacts(**{**base, **kw})


class TestTheSignature(SimpleTestCase):
    TODAY = date(2026, 11, 8)

    def sig(self, **kw):
        return report_memory.signature_of_facts(facts(**kw), self.TODAY)

    def test_it_is_status_priority_due_and_whether_overdue(self):
        self.assertEqual(self.sig(due_date=date(2026, 11, 7)), "Open|Normal|2026-11-07|1")
        self.assertEqual(self.sig(due_date=date(2026, 11, 9)), "Open|Normal|2026-11-09|0")
        self.assertEqual(self.sig(), "Open|Normal||0")

    def test_days_late_and_days_idle_are_not_in_it(self):
        a = self.sig(due_date=date(2026, 11, 1))
        later = report_memory.signature_of_facts(
            facts(due_date=date(2026, 11, 1)), date(2026, 11, 20)
        )
        self.assertEqual(a, later)

    def test_a_closed_task_is_not_overdue(self):
        self.assertTrue(self.sig(due_date=date(2026, 11, 1), status="Closed").endswith("|0"))

    def test_each_part_changes_it(self):
        base = self.sig(due_date=date(2026, 11, 9))
        for change in (
            {"status": "WIP"},
            {"priority": "High"},
            {"due_date": date(2026, 11, 10)},
            {"due_date": date(2026, 11, 7)},
        ):
            self.assertNotEqual(self.sig(**{"due_date": date(2026, 11, 9), **change}), base, change)

    def test_a_separator_typed_in_a_status_cannot_forge_another_part(self):
        self.assertEqual(self.sig(status="Open|High").count("|"), 3)


class MemoryBase(JobBase):
    def setUp(self):
        super().setUp()
        self.schedule = schedules.create(
            self.ctx, name="Plan", job=JOB, frequency="daily", time_of_day="09:00"
        )
        # Three tasks that are always the top three ("Start with" holds three), so the tasks a
        # test is about are NOT top tasks, which are reported every time by design.
        self.fillers = [
            self.make_task(
                f"Top {n}",
                priority="Critical",
                due_date=self.local_today() - timedelta(days=60 + n),
            )
            for n in range(3)
        ]
        self.filler_ids = {t.task_id for t in self.fillers}

    # -- the previous report --

    def file(self, *, read=True, deleted=False, **kw):
        """Run the job and file it the way a run does; the member has opened it (or not)."""
        with mock.patch("django.utils.timezone.now", return_value=self.now):
            report = JOBS[JOB].run(self.ctx, kw.pop("params", {}))
        item = schedule_delivery.deliver(self.schedule, report)
        InboxItems.objects.filter(pk=item.pk).update(is_read=read, is_deleted=deleted)
        return report, item

    def remember(
        self, seen, *, job=JOB, read=True, age_days=1, user=None, deleted=False, team=None
    ):
        """A previous report holding exactly `seen` (`{task: signature}`)."""
        item = InboxItems.objects.create(
            team=team or self.team,
            sender=None,
            receiver=user or self.user,
            item_type=ITEM_TYPE_GENOS_REPORT,
            item_body={"title": "t", "text": "s", "sections": []},
            item_optionals={
                "kind": "schedule_report",
                "job": job,
                "schedule_id": str(self.schedule.id),
                "seen": {str(k): v for k, v in seen.items()},
            },
            request_status="",
            is_read=read,
            is_deleted=deleted,
        )
        InboxItems.objects.filter(pk=item.pk).update(
            ts_created_at=self.now - timedelta(days=age_days)
        )
        return item

    # -- the data --

    def late(self, title="Late", days=3, **kw):
        return self.make_task(title, due_date=self.local_today() - timedelta(days=days), **kw)

    def sig_of(self, task):
        task.refresh_from_db()
        return report_memory.signature_of_task(task, self.local_today())

    def listed(self, report):
        return [i for s in report["sections"] for i in s["items"] if i.get("task_id")]

    def listed_ids(self, report):
        return {i["task_id"] for i in self.listed(report)} - self.filler_ids

    def stat(self, report, key):
        return next((s["count"] for s in report.get("stats", []) if s["key"] == key), 0)

    def answer(self, task, mark, **kw):
        return importance.set_mark(self.ctx, task.task_id, mark, allowed=importance.ALL_MARKS, **kw)

    def report_now(self, **params):
        return self.run_job(JOB, **params)


class TestItDoesNotRepeatItself(MemoryBase):
    def test_a_second_report_on_unchanged_data_lists_nothing_it_listed_before(self):
        tasks = [self.late(f"Late {n}", days=3 + n) for n in range(4)]
        first, _ = self.file()
        self.assertEqual(self.listed_ids(first), {t.task_id for t in tasks})
        second = self.report_now()
        self.assertEqual(self.listed_ids(second), set())
        self.assertEqual(self.stat(second, "still_same"), 4)
        self.assertIn("4 unchanged since your last report.", second["summary"])
        self.assertEqual(second["seen"], first["seen"])

    def test_the_first_report_holds_back_nothing(self):
        tasks = [self.late(f"Late {n}", days=3 + n) for n in range(3)]
        report = self.report_now()
        self.assertEqual(self.listed_ids(report), {t.task_id for t in tasks})
        self.assertEqual(self.stat(report, "still_same"), 0)
        self.assertNotIn("unchanged", report["summary"])
        self.assertTrue({i["why"] for i in self.listed(report)} <= {"new", "important", "overdue"})

    def test_a_task_changed_since_comes_back_and_the_rest_stay_folded(self):
        keep, changed = self.late("Keep", days=4), self.late("Changed", days=5)
        self.file()
        TaskMaster.objects.filter(pk=changed.pk).update(priority="High")
        report = self.report_now()
        ids = self.listed_ids(report)
        self.assertIn(changed.task_id, ids)
        self.assertNotIn(keep.task_id, ids)
        self.assertEqual(self.stat(report, "still_same"), 1)
        row = next(i for i in self.listed(report) if i["task_id"] == changed.task_id)
        self.assertIn(row["why"], ("changed", "important"))

    def test_days_going_by_do_not_make_a_task_look_changed(self):
        task = self.late(days=4)
        self.file()
        self.now += timedelta(days=2)
        report = self.report_now()
        self.assertNotIn(task.task_id, self.listed_ids(report))

    def test_a_task_never_shown_before_is_listed_once_then_folded(self):
        self.late("Old", days=4)
        self.file()
        fresh = self.late("Fresh", days=5)
        second, _ = self.file()
        self.assertIn(fresh.task_id, self.listed_ids(second))
        third = self.report_now()
        self.assertNotIn(fresh.task_id, self.listed_ids(third))
        self.assertEqual(self.stat(third, "still_same"), 2)

    def test_a_folded_task_stays_in_the_memory_so_it_is_not_listed_every_other_report(self):
        task = self.late(days=4)
        self.file()
        second, _ = self.file()  # folds it
        self.assertEqual(self.stat(second, "still_same"), 1)
        self.assertEqual(self.stat(self.report_now(), "still_same"), 1)
        self.assertIn(str(task.task_id), second["seen"])

    def test_the_more_line_counts_only_what_the_cap_hid_not_what_was_folded(self):
        for n in range(8):
            self.late(f"Late {n}", days=3 + n)

        def more(report):
            return [
                i["text"] for s in report["sections"] for i in s["items"] if i.get("kind") == "more"
            ]

        first, _ = self.file()
        second = self.report_now()
        self.assertTrue(more(first))
        self.assertEqual(more(second), more(first))  # the cap hid the same ones; folding hid none
        self.assertGreater(self.stat(second, "still_same"), 0)

    def test_a_task_you_are_waiting_on_others_for_is_held_back_when_unchanged(self):
        mine = self.make_task("Mine", status="Blocked")
        blocker = self.make_task("Their work", assignee=self.user2, status="WIP")
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=mine)
        first, _ = self.file()
        self.assertIn(mine.task_id, self.listed_ids(first))
        second = self.report_now()
        self.assertNotIn(mine.task_id, self.listed_ids(second))
        self.assertNotIn("You are waiting on others", [s["heading"] for s in second["sections"]])
        self.assertGreaterEqual(self.stat(second, "still_same"), 1)

    def test_a_handed_over_task_is_held_back_when_unchanged_and_counted(self):
        """MIP-1538: the handed-over rows of the Morning plan obey the same no-repeat rule."""
        handed = self.make_task(
            "Handed over", reporter=self.user, assignee=self.user2, status="WIP"
        )
        first, _ = self.file()
        self.assertIn(handed.task_id, self.listed_ids(first))
        second = self.report_now()
        self.assertNotIn(handed.task_id, self.listed_ids(second))
        self.assertNotIn("You are waiting on others", [s["heading"] for s in second["sections"]])
        self.assertGreaterEqual(self.stat(second, "still_same"), 1)
        handed.status = "Review"
        handed.save()
        third = self.report_now()
        self.assertIn(handed.task_id, self.listed_ids(third))

    def test_waiting_on_you_and_changed_are_never_held_back(self):
        task = self.late(days=4)
        self.activity(task, self.user2, new="WIP", old="Open", at=self.ago(hours=2))
        first, _ = self.file()
        second = self.report_now()
        self.assertIn("Changed in the last 24 hours", [s["heading"] for s in second["sections"]])
        self.assertIn(task.task_id, self.listed_ids(second))  # it is "activity": reported

    def test_a_task_the_member_cannot_open_is_never_held_or_counted(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed",
            code="CLS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        hidden = self.make_task(
            "Hidden",
            project=project,
            assignee=self.user2,
            reporter=self.user2,
            due_date=self.local_today() - timedelta(days=3),
        )
        report = self.report_now()
        self.assertNotIn(str(hidden.task_id), report["seen"])

    def test_what_is_left_does_not_repeat_itself_either(self):
        task = self.make_task("Worked on", status="WIP")
        self.activity(task, self.user, new="WIP", old="Open", at=self.ago(days=2))
        schedule = schedules.create(
            self.ctx, name="Left", job="what_is_left", frequency="daily", time_of_day="09:00"
        )
        with mock.patch("django.utils.timezone.now", return_value=self.now):
            first = JOBS["what_is_left"].run(self.ctx, {})
        item = schedule_delivery.deliver(schedule, first)
        InboxItems.objects.filter(pk=item.pk).update(is_read=True)
        with mock.patch("django.utils.timezone.now", return_value=self.now):
            second = JOBS["what_is_left"].run(self.ctx, {})
        self.assertEqual(self.listed_ids(first), {task.task_id})
        self.assertEqual(self.listed_ids(second), set())
        self.assertEqual(self.stat(second, "still_same"), 1)
        self.assertIn("1 unchanged since your last report.", second["summary"])

    def test_what_is_left_never_holds_back_a_closed_task(self):
        task = self.make_task("Done", status="Closed")
        self.activity(task, self.user, new="Closed", old="WIP", at=self.ago(days=2))
        schedule = schedules.create(
            self.ctx, name="Left", job="what_is_left", frequency="daily", time_of_day="09:00"
        )
        for _ in range(2):
            with mock.patch("django.utils.timezone.now", return_value=self.now):
                report = JOBS["what_is_left"].run(self.ctx, {})
            item = schedule_delivery.deliver(schedule, report)
            InboxItems.objects.filter(pk=item.pk).update(is_read=True)
            self.assertEqual(self.listed_ids(report), {task.task_id})
            self.assertEqual(self.stat(report, "still_same"), 0)
            self.assertNotIn("unchanged", report["summary"])


class TestTheHardRules(MemoryBase):
    """The boundaries, through `judge` itself (a task due in 3 days is in no section of a report,
    so only the verdict can show where the rule stops), then the same rules end to end."""

    def verdict(self, task_facts, *, seen=None, start_with=(), changed=(), today=None):
        if seen is not None:
            self.remember(seen)
        return report_memory.judge(
            self.ctx,
            JOB,
            [task_facts.task_id],
            lambda _id: task_facts,
            today=today or self.local_today(),
            now=self.now,
            start_with=set(start_with),
            changed=set(changed),
        )

    def sig(self, f):
        return report_memory.signature_of_facts(f, self.local_today())

    def f(self, **kw):
        return facts(task_id=900, **kw)

    def assert_why(self, verdict, why, task_id=900):
        self.assertEqual(verdict.why.get(task_id), why, verdict)
        self.assertNotIn(task_id, verdict.held)

    def test_due_today_and_the_next_two_days_are_reported_the_third_is_not(self):
        today = self.local_today()
        for days, expect in ((0, "due_soon"), (1, "due_soon"), (2, "due_soon"), (3, None)):
            f = self.f(due_date=today + timedelta(days=days))
            v = self.verdict(f, seen={900: self.sig(f)})
            if expect:
                self.assert_why(v, expect)
            else:
                self.assertEqual(v.held, {900: "same"}, days)

    def test_a_closed_task_due_today_is_not_a_reason(self):
        f = self.f(due_date=self.local_today(), status="Closed")
        self.assertEqual(self.verdict(f, seen={900: self.sig(f)}).held, {900: "same"})

    def test_an_active_sprint_with_a_due_date_is_reported_without_one_it_is_not(self):
        f = self.f(due_date=self.local_today() + timedelta(days=9), in_active_sprint=True)
        self.assert_why(self.verdict(f, seen={900: self.sig(f)}), "sprint")
        for plain in (
            self.f(due_date=self.local_today() + timedelta(days=9)),
            self.f(in_active_sprint=True),
        ):
            self.assertEqual(self.verdict(plain, seen={900: self.sig(plain)}).held, {900: "same"})

    def test_newly_blocked_is_reported_one_that_was_blocked_is_not(self):
        f = self.f(status="Blocked")
        was_open = self.sig(self.f(status="Open"))
        self.assert_why(self.verdict(f, seen={900: was_open}), "blocked")
        self.assert_why(self.verdict(f, seen={}), "blocked")  # not in the memory at all
        self.assertEqual(self.verdict(f, seen={900: self.sig(f)}).held, {900: "same"})

    def test_newly_overdue_is_reported_one_that_was_overdue_is_not(self):
        f = self.f(due_date=self.local_today() - timedelta(days=2))
        not_late_then = self.sig(self.f(due_date=self.local_today() + timedelta(days=6)))
        self.assert_why(self.verdict(f, seen={900: not_late_then}), "overdue")
        self.assertEqual(self.verdict(f, seen={900: self.sig(f)}).held, {900: "same"})

    def test_activity_from_others_is_reported(self):
        f = self.f(due_date=self.local_today() - timedelta(days=4))
        v = self.verdict(f, seen={900: self.sig(f)}, changed={900})
        self.assert_why(v, "activity")

    def test_a_top_task_is_reported_every_time(self):
        f = self.f(due_date=self.local_today() - timedelta(days=4))
        self.assert_why(self.verdict(f, seen={900: self.sig(f)}, start_with={900}), "important")

    def test_a_signature_that_changed_without_a_rule_is_listed_as_changed(self):
        f = self.f(priority="High")
        was = self.sig(self.f(priority="Low"))
        self.assert_why(self.verdict(f, seen={900: was}), "changed")

    def test_a_task_not_in_the_memory_and_with_no_rule_is_new(self):
        self.assert_why(self.verdict(self.f(), seen={}), "new")

    def test_a_task_with_no_project_has_no_signature_and_is_never_held(self):
        f = self.f(project_id=None)
        v = self.verdict(f, seen={900: self.sig(f)})
        self.assertEqual((v.held, v.why, v.seen), ({}, {}, {}))

    def test_the_reason_order_is_due_soon_sprint_blocked_overdue_activity_important(self):
        f = self.f(
            status="Blocked",
            due_date=self.local_today(),
            in_active_sprint=True,
        )
        self.assert_why(self.verdict(f, seen={}, changed={900}, start_with={900}), "due_soon")

    # -- the same, in a real report --

    def one(self, task):
        report = self.report_now()
        rows = [i for i in self.listed(report) if i["task_id"] == task.task_id]
        return rows[0] if rows else None

    def test_an_overdue_task_in_an_active_sprint_is_reported_every_time(self):
        task = self.late("In sprint", days=5)
        sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S1",
            sequence_number=1,
            start_date=self.local_today() - timedelta(days=7),
            end_date=self.local_today() + timedelta(days=7),
            status="active",
        )
        TaskMaster.objects.filter(pk=task.pk).update(sprint=sprint)
        self.remember({task.task_id: self.sig_of(task)})
        self.assertEqual(self.one(task)["why"], "sprint")

    def test_a_sprint_that_is_not_active_is_not_a_reason(self):
        task = self.late("Old sprint", days=5)
        sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S0",
            sequence_number=2,
            start_date=self.local_today() - timedelta(days=30),
            end_date=self.local_today() - timedelta(days=16),
            status="completed",
        )
        TaskMaster.objects.filter(pk=task.pk).update(sprint=sprint)
        self.remember({task.task_id: self.sig_of(task)})
        self.assertIsNone(self.one(task))

    def test_a_task_due_today_is_reported_with_its_reason(self):
        task = self.make_task("Today", due_date=self.local_today(), priority="Low")
        self.remember({task.task_id: self.sig_of(task)})
        self.assertEqual(self.one(task)["why"], "due_soon")

    def test_a_memory_with_the_same_signature_and_no_rule_folds_it(self):
        task = self.late(days=6)
        self.remember({task.task_id: self.sig_of(task)})
        self.assertIsNone(self.one(task))


class TestTheMembersAnswer(MemoryBase):
    def test_on_it_quiets_a_task_even_one_a_hard_rule_would_report(self):
        task = self.make_task("Due today", due_date=self.local_today())
        self.answer(task, "on_it")
        report = self.report_now()
        self.assertNotIn(task.task_id, self.listed_ids(report))
        self.assertEqual(self.stat(report, "quieted"), 1)
        self.assertIn("1 quieted by you.", report["summary"])
        self.assertNotIn(str(task.task_id), report["seen"])

    def test_on_it_on_a_top_task_takes_it_out_of_start_with_too(self):
        top = self.fillers[0]
        self.answer(top, "on_it")
        report = self.report_now()
        self.assertNotIn(
            top.task_id, {i.get("task_id") for i in self.section(report, "Start with")}
        )
        self.assertNotIn(top.task_id, {i["task_id"] for i in self.listed(report)})
        self.assertEqual(self.stat(report, "quieted"), 1)

    def test_on_it_lasts_seven_days_then_the_rules_apply_again(self):
        task = self.make_task("Due today", due_date=self.local_today())
        self.answer(task, "on_it")
        MemberImportanceMark.objects.filter(task_id=task.task_id).update(
            ts=self.now - timedelta(days=6, hours=23)
        )
        self.assertNotIn(task.task_id, self.listed_ids(self.report_now()))
        MemberImportanceMark.objects.filter(task_id=task.task_id).update(
            ts=self.now - timedelta(days=7, minutes=1)
        )
        self.assertIn(task.task_id, self.listed_ids(self.report_now()))

    def test_on_it_ends_when_the_task_changes(self):
        task = self.make_task("Due today", due_date=self.local_today())
        self.answer(task, "on_it")
        TaskMaster.objects.filter(pk=task.pk).update(priority="Critical")
        self.assertIn(task.task_id, self.listed_ids(self.report_now()))

    def test_not_important_quiets_until_the_signature_changes(self):
        task = self.late("Meh", days=4)
        self.answer(task, "not_important")
        self.assertNotIn(task.task_id, self.listed_ids(self.report_now()))
        # ...for as long as it stays the same, however long that is
        self.now += timedelta(days=40)
        MemberImportanceMark.objects.filter(task_id=task.task_id).update(
            ts=self.now - timedelta(days=40)
        )
        self.assertNotIn(task.task_id, self.listed_ids(self.report_now()))
        TaskMaster.objects.filter(pk=task.pk).update(status="Blocked")
        self.assertIn(task.task_id, self.listed_ids(self.report_now()))

    def test_a_mark_made_before_has_no_signature_and_never_touches_a_report(self):
        task = self.late("Old mark", days=4)
        MemberImportanceMark.objects.create(
            user=self.user, team=self.team, task_id=task.task_id, mark="not_important"
        )
        self.assertIn(task.task_id, self.listed_ids(self.report_now()))

    def test_priority_is_right_never_touches_a_report(self):
        task = self.late("Right", days=4)
        self.answer(task, "priority_is_right")
        self.assertIn(task.task_id, self.listed_ids(self.report_now()))

    def test_the_answer_is_the_members_own(self):
        task = self.late(days=4)
        self.answer(task, "on_it")
        ctx2 = ToolContext(team_id=self.ctx.team_id, user_id=str(self.user2.id))
        with mock.patch("django.utils.timezone.now", return_value=self.now):
            report = JOBS[JOB].run(ctx2, {})
        self.assertEqual(self.stat(report, "quieted"), 0)

    def test_with_recording_off_nothing_can_be_answered_or_quieted(self):
        task = self.late(days=4)
        self.user.member_events_enabled = False
        self.user.save(update_fields=["member_events_enabled"])
        report = self.report_now()
        self.assertTrue(self.listed(report))
        self.assertFalse(any(i.get("can_answer") for i in self.listed(report)))
        with self.assertRaises(importance.RecordingOff):
            self.answer(task, "on_it")

    def test_with_recording_on_every_task_row_may_be_answered(self):
        self.late(days=4)
        rows = self.listed(self.report_now())
        self.assertTrue(rows)
        self.assertTrue(all(i.get("can_answer") is True for i in rows))


class TestTheMemory(MemoryBase):
    def setUp(self):
        super().setUp()
        self.task = self.late(days=5)
        self.sig = self.sig_of(self.task)

    def folded(self):
        return self.task.task_id not in self.listed_ids(self.report_now())

    def test_the_previous_report_counts(self):
        self.remember({self.task.task_id: self.sig})
        self.assertTrue(self.folded())

    def test_a_deleted_previous_report_still_counts(self):
        self.remember({self.task.task_id: self.sig}, deleted=True)
        self.assertTrue(self.folded())

    def test_an_unopened_previous_report_does_not_count(self):
        self.remember({self.task.task_id: self.sig}, read=False)
        self.assertFalse(self.folded())

    def test_the_newest_opened_report_is_the_memory(self):
        self.remember({self.task.task_id: "Open|Normal||0"}, age_days=3)
        self.remember({self.task.task_id: self.sig}, age_days=1)
        self.remember({self.task.task_id: "Open|Normal||0"}, age_days=0, read=False)
        self.assertTrue(self.folded())

    def test_a_report_older_than_the_retention_is_forgotten(self):
        self.remember({self.task.task_id: self.sig}, age_days=report_memory.MEMORY_DAYS + 1)
        self.assertFalse(self.folded())
        self.remember({self.task.task_id: self.sig}, age_days=report_memory.MEMORY_DAYS - 1)
        self.assertTrue(self.folded())

    def test_another_jobs_report_is_not_the_memory(self):
        self.remember({self.task.task_id: self.sig}, job="what_is_left")
        self.assertFalse(self.folded())

    def test_another_members_report_is_not_the_memory(self):
        self.remember({self.task.task_id: self.sig}, user=self.user2)
        self.assertFalse(self.folded())

    def test_another_teams_report_is_not_the_memory(self):
        other = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        self.remember({self.task.task_id: self.sig}, team=other)
        self.assertFalse(self.folded())

    def test_a_report_with_no_memory_in_it_is_no_memory(self):
        item = self.remember({})
        InboxItems.objects.filter(pk=item.pk).update(
            item_optionals={"kind": "schedule_report", "job": JOB}
        )
        self.assertFalse(self.folded())

    def test_the_memory_is_in_the_optionals_never_in_the_body(self):
        report, item = self.file()
        item.refresh_from_db()
        self.assertEqual(item.item_optionals["seen"], report["seen"])
        self.assertNotIn("seen", item.item_body)
        self.assertNotIn(str(self.task.task_id) + '":', json.dumps(item.item_body.get("seen", "")))

    def test_a_stranger_cannot_forge_the_memory(self):
        # What a job returns is cleaned: keys are whole numbers, values short signatures.
        cleaned = schedule_delivery.clean_seen(
            {"12": "Open|Normal||0", "x": "a", "-3": "a", "7": "z" * 500, "8": 5, "٣": "a"}
        )
        self.assertEqual(cleaned, {"12": "Open|Normal||0"})
        many = {str(n): "a" for n in range(1, 1000)}
        self.assertEqual(len(schedule_delivery.clean_seen(many)), schedule_delivery.MAX_SEEN)


class TestWhatTheCardGets(SimpleTestCase):
    def one(self, **entry):
        report = {
            "title": "t",
            "summary": "s",
            "sections": [{"heading": "h", "items": [{"text": "x", **entry}]}],
        }
        return schedule_delivery.clean_report(report, "t")["sections"][0]["items"][0]

    def test_task_id_why_and_can_answer_are_kept_when_valid(self):
        out = self.one(task_id=42, why="overdue", can_answer=True)
        self.assertEqual((out["task_id"], out["why"], out["can_answer"]), (42, "overdue", True))

    def test_every_why_is_kept_and_nothing_else(self):
        for why in report_memory.WHY_VALUES:
            self.assertEqual(self.one(why=why)["why"], why)
        for bad in ("Overdue", "", None, 3, "<b>", "due soon"):
            self.assertNotIn("why", self.one(why=bad))

    def test_a_bad_task_id_or_answer_flag_is_dropped_not_repaired(self):
        for bad in (0, -1, "5", 2.5, True, None, 2**63):
            self.assertNotIn("task_id", self.one(task_id=bad), bad)
        for bad in (False, 1, "true", None):
            self.assertNotIn("can_answer", self.one(can_answer=bad), bad)

    def test_the_new_stats_have_their_emoji_from_the_server(self):
        stats = schedule_delivery.clean_stats(
            [{"key": "still_same", "count": 4, "emoji": "💥"}, {"key": "quieted", "count": 2}]
        )
        self.assertEqual(
            stats,
            [
                {"key": "still_same", "count": 4, "emoji": "🔁"},
                {"key": "quieted", "count": 2, "emoji": "🔕"},
            ],
        )


class TestTheModelSeesTheOldShape(MemoryBase):
    def test_a_report_with_the_new_fields_is_shown_to_the_model_as_it_always_was(self):
        self.late(days=5)
        self.remember({})
        report = self.report_now()
        body = schedule_delivery.clean_report(report, "t")
        self.assertTrue(any("task_id" in i for s in body["sections"] for i in s["items"]))
        row = mock.Mock(item_type=ITEM_TYPE_GENOS_REPORT, item_body=body)
        seen = _model_body(row)
        self.assertEqual(set(seen), {"title", "text", "sections"})
        for section in seen["sections"]:
            self.assertEqual(set(section), {"heading", "items"})
            for item in section["items"]:
                self.assertTrue(set(item) <= {"text", "link"}, item)
        blob = json.dumps(seen)
        for leak in ("task_id", "why", "can_answer", "still_same", "quieted", "seen", "stats"):
            self.assertNotIn(leak, blob)


class TestTheEndpoint(MemoryBase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.task = self.late("Answer me", days=3)

    def url(self, task=None):
        return (
            f"/api/v2/user/memory/marks/{(task or self.task).task_id}/?team_id={self.team.team_id}"
        )

    def put(self, mark, **extra):
        return self.client.put(self.url(), {"mark": mark, **extra}, format="json")

    def test_on_it_is_accepted_and_stores_the_signature_and_the_source(self):
        res = self.put("on_it", source="report")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(
            (res.data["mark"], res.data["previous"], res.data["taskId"]),
            ("on_it", None, self.task.task_id),
        )
        row = MemberImportanceMark.objects.get(task_id=self.task.task_id)
        self.assertEqual(
            (row.mark, row.source, row.sig), ("on_it", "report", self.sig_of(self.task))
        )

    def test_it_says_what_was_there_so_an_undo_can_restore_it(self):
        self.put("not_important")
        res = self.put("on_it")
        self.assertEqual((res.data["mark"], res.data["previous"]), ("on_it", "not_important"))
        gone = self.client.delete(self.url())
        self.assertEqual((gone.data["mark"], gone.data["previous"]), (None, "on_it"))
        self.assertFalse(MemberImportanceMark.objects.filter(task_id=self.task.task_id).exists())

    def test_one_mark_per_task_a_tap_replaces_an_earlier_one(self):
        self.put("priority_is_right")
        self.put("on_it")
        self.assertEqual(MemberImportanceMark.objects.filter(task_id=self.task.task_id).count(), 1)

    def test_a_bad_mark_or_source_is_refused(self):
        self.assertEqual(self.put("useful").status_code, 400)
        self.assertEqual(self.put("on_it", source="mcp").status_code, 400)
        self.assertEqual(self.put("on_it", source="x").status_code, 400)

    def test_recording_off_is_409_recording_off(self):
        self.user.member_events_enabled = False
        self.user.save(update_fields=["member_events_enabled"])
        res = self.put("on_it")
        self.assertEqual((res.status_code, res.data["code"]), (409, "recording_off"))

    def test_a_task_the_member_cannot_open_is_the_same_404(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed",
            code="CLS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        hidden = self.make_task("H", project=project, assignee=self.user2, reporter=self.user2)
        res = self.client.put(self.url(hidden), {"mark": "on_it"}, format="json")
        self.assertEqual(res.status_code, 404)
        self.assertFalse(MemberImportanceMark.objects.exists())

    def test_the_marks_cap_still_holds_for_on_it(self):
        with mock.patch.object(importance, "MAX_MARKS", 0):
            res = self.put("on_it")
        self.assertEqual(res.status_code, 400)

    def test_settings_lists_it_with_its_words(self):
        self.put("on_it")
        res = self.client.get(f"/api/v2/user/memory/?team_id={self.team.team_id}")
        self.assertEqual([m["mark"] for m in res.data["marks"]], ["on_it"])
        self.assertIn("on_it", [m["key"] for m in res.data["catalog"]["marks"]])

    def test_the_connected_app_tool_is_unchanged(self):
        from origin.views.public.mcp.secretary.set_item_importance import SET_ITEM_IMPORTANCE

        enum = SET_ITEM_IMPORTANCE.parameters_schema["properties"]["mark"]["enum"]
        self.assertEqual(enum, ["not_important", "priority_is_right", "clear"])
        out = SET_ITEM_IMPORTANCE.run(
            {"task_id": self.task.task_id, "mark": "not_important"}, self.ctx
        )
        self.assertNotIn("previous", out)
        row = MemberImportanceMark.objects.get(task_id=self.task.task_id)
        self.assertEqual(row.source, "mcp")
        with self.assertRaises(ToolError):
            SET_ITEM_IMPORTANCE.run({"task_id": self.task.task_id, "mark": "on_it"}, self.ctx)

    def test_recording_off_purges_on_it_and_so_does_account_deletion(self):
        self.put("on_it")
        self.assertEqual(purge_for_user(self.user.id), 1)
        self.assertFalse(MemberImportanceMark.objects.exists())

    def test_the_export_lists_it_with_its_source(self):
        self.put("on_it", source="report")
        rows = importance.export_rows(str(self.user.id))["marks"]
        self.assertEqual([(r["mark"], r["source"]) for r in rows], [("on_it", "report")])


class TestBothDoorsOneDefault(MemoryBase):
    def test_a_member_who_never_taps_and_has_no_memory_gets_the_report_they_always_got(self):
        tasks = [self.late(f"T{n}", days=3 + n) for n in range(3)]
        report = self.report_now()
        self.assertEqual(self.listed_ids(report), {t.task_id for t in tasks})
        self.assertEqual(self.stat(report, "still_same") + self.stat(report, "quieted"), 0)
        for section in report["sections"]:
            for item in section["items"]:
                self.assertNotIn("quieted", item["text"])
