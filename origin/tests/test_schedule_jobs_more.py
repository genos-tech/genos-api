"""`what_is_left` and `milestone_updates` (MIP-1152), and the suggested schedules.

What matters, as for the first two jobs (test_schedule_jobs): links to the real things, no
model fence, the member's own, nothing written, the parameters checked on save. Beyond that:
a milestone the member cannot open fails the run in words (it must not read as an empty
milestone), and the changes in a milestone are read only from tasks the member may open.
"""

from __future__ import annotations

import json
from datetime import timedelta

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services import schedule_defaults, schedules
from origin.services.schedule_jobs import JOBS, item_count
from origin.tests.test_schedule_jobs import JobBase


class TestCatalog(JobBase):
    def test_both_jobs_are_registered_and_checked_on_save(self):
        for key in ("what_is_left", "milestone_updates"):
            self.assertTrue(JOBS[key].title and JOBS[key].description)

        def save(job, **params):
            return schedules.create(
                self.ctx,
                name=f"x {job}",
                job=job,
                params=params,
                frequency="daily",
                time_of_day="09:00",
            )

        for job, params in (
            ("what_is_left", {"days": 0}),
            ("what_is_left", {"days": 32}),
            ("what_is_left", {"day": 3}),
            ("milestone_updates", {}),
            ("milestone_updates", {"milestone_id": 0}),
            ("milestone_updates", {"milestone_id": 1, "hours": 721}),
        ):
            with self.assertRaises(schedules.ScheduleError, msg=f"{job} {params}"):
                save(job, **params)
        save("what_is_left", days=14)
        save("milestone_updates", milestone_id=1, hours=24)


class TestWhatIsLeft(JobBase):
    def test_open_closed_and_unfinished_todos_each_get_their_own_section(self):
        still = self.make_task("Still going", status="WIP")
        done = self.make_task("Shipped", status="Closed")
        self.activity(still, self.user, "status_changed", "Open", "WIP", at=self.ago(days=3))
        self.activity(done, self.user, "status_changed", "WIP", "Closed", at=self.ago(days=2))
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=self.local_today() - timedelta(days=3)
        )
        left = ToDoItem.objects.create(group=group, title="Call the bank")
        # Today's own open to-do belongs to "what to do today", not to what was left over.
        todays = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=self.local_today()
        )
        ToDoItem.objects.create(group=todays, title="Still to do today")
        ToDoItem.objects.create(group=group, title="Done already", is_completed=True)

        report = self.run_job("what_is_left")

        open_ = self.section(report, "Still open from last time")
        self.assertEqual([i["link"] for i in open_], [self.task_link(still)])
        self.assertIn("Still going", open_[0]["text"])
        self.assertIn("(WIP)", open_[0]["text"])
        closed = self.section(report, "Closed after your work")
        self.assertEqual([i["link"] for i in closed], [self.task_link(done)])
        todos = self.section(report, "To-dos left unfinished")
        self.assertEqual(len(todos), 1)
        self.assertEqual(
            todos[0]["link"],
            f"/workspace/todo/{self.local_today() - timedelta(days=3)}/item/{left.item_id}",
        )
        self.assertNotIn("Done already", json.dumps(report))
        self.assertNotIn("Still to do today", json.dumps(report))
        self.assertIn("1 task still open, 1 task closed, 1 to-do left", report["summary"])
        self.assertNotIn("workspace_content", json.dumps(report))

    def test_the_window_is_a_parameter(self):
        task = self.make_task("Old work")
        self.activity(task, self.user, "status_changed", "Open", "WIP", at=self.ago(days=10))
        self.assertEqual(self.run_job("what_is_left")["sections"], [])
        wide = self.run_job("what_is_left", days=14)
        self.assertEqual(len(self.section(wide, "Still open from last time")), 1)

    def test_only_my_own_work_is_in_my_report(self):
        task = self.make_task("Theirs", assignee=self.user2)
        self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(days=2))
        self.assertEqual(self.run_job("what_is_left")["sections"], [])
        self.assertEqual(
            len(self.section(self.as_user2("what_is_left"), "Still open from last time")), 1
        )

    def test_a_quiet_week_says_so_and_invents_nothing(self):
        report = self.run_job("what_is_left")
        self.assertEqual(report["sections"], [])
        self.assertEqual(item_count(report), 0)
        self.assertEqual(report["summary"], "Nothing saved in Genos for the last 7 days.")

    def test_it_writes_nothing(self):
        task = self.make_task("Quiet")
        self.activity(task, self.user, "status_changed", "Open", "WIP", at=self.ago(days=1))
        before = (TaskMaster.objects.count(), ToDoItem.objects.count())
        self.run_job("what_is_left")
        self.assertEqual(before, (TaskMaster.objects.count(), ToDoItem.objects.count()))


class TestMilestoneUpdates(JobBase):
    def setUp(self):
        super().setUp()
        self.milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="v1 launch",
            status="Open",
        )

    def in_milestone(self, title, **extra):
        return self.make_task(title, milestone=self.milestone, **extra)

    def test_it_says_where_the_milestone_stands_and_what_changed_with_links(self):
        a = self.in_milestone("Write the copy", status="WIP")
        self.in_milestone("Fix the login", status="Closed")
        self.in_milestone("Late one", due_date=self.local_today() - timedelta(days=2))
        self.activity(a, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=5))
        self.comment(a, self.user2, "Looks good", at=self.ago(hours=3))

        report = self.run_job("milestone_updates", milestone_id=self.milestone.milestone_id)

        self.assertEqual(report["title"], "Milestone: v1 launch")
        stands = self.texts(
            {"sections": [s for s in report["sections"] if s["heading"] == "Where it stands"]}
        )
        self.assertIn("3 tasks: 1 open, 1 wip, 0 blocked, 0 pending, 1 closed", stands[0])
        self.assertIn("1 overdue", stands)
        changed = self.section(report, "Changed in the last 168 hours")
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["link"], self.task_link(a))
        self.assertIn("by otheruser", changed[0]["text"])
        self.assertIn("(+1 more)", changed[0]["text"])
        ready = self.section(report, "Ready to start")
        self.assertTrue(all(i["link"].startswith("/workspace/tasks/project/") for i in ready))
        self.assertNotIn("workspace_content", json.dumps(report))

    def test_changes_by_everyone_count_not_only_by_others(self):
        task = self.in_milestone("Mine")
        self.activity(task, self.user, "status_changed", "Open", "WIP", at=self.ago(hours=2))
        report = self.run_job("milestone_updates", milestone_id=self.milestone.milestone_id)
        self.assertEqual(len(self.section(report, "Changed in the last 168 hours")), 1)

    def test_the_window_is_a_parameter(self):
        task = self.in_milestone("Old change")
        self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=30))
        narrow = self.run_job(
            "milestone_updates", milestone_id=self.milestone.milestone_id, hours=24
        )
        self.assertEqual(self.section(narrow, "Changed in the last 24 hours"), [])
        wide = self.run_job("milestone_updates", milestone_id=self.milestone.milestone_id, hours=48)
        self.assertEqual(len(self.section(wide, "Changed in the last 48 hours")), 1)

    def test_a_milestone_the_member_cannot_open_fails_in_words_not_as_empty(self):
        from origin.services.schedules import ScheduleRunError  # noqa: PLC0415

        elsewhere = ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed shop",
            code="CLS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        hidden = MilestoneMaster.objects.create(
            team=self.team,
            project=elsewhere,
            reporter=self.user2,
            title="Secret plan",
            status="Open",
        )
        with self.assertRaises(ScheduleRunError) as caught:
            self.run_job("milestone_updates", milestone_id=hidden.milestone_id)
        self.assertNotIn("Secret plan", caught.exception.public)
        with self.assertRaises(ScheduleRunError) as caught:
            self.run_job("milestone_updates", milestone_id=999999)
        self.assertIn("not found", caught.exception.public)

    def test_a_task_the_member_cannot_open_is_not_in_the_changes(self):
        elsewhere = ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed shop",
            code="CLS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        hidden = self.make_task(
            "Hidden work", project=elsewhere, milestone=self.milestone, assignee=self.user2
        )
        visible = self.in_milestone("Visible work")
        for task in (hidden, visible):
            self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=2))
        report = self.run_job("milestone_updates", milestone_id=self.milestone.milestone_id)
        shown = json.dumps(report)
        self.assertIn("Visible work", shown)
        self.assertNotIn("Hidden work", shown)
        # Nor is its existence given away by an "and 1 more" in the list it would have been in.
        ready = self.section(report, "Ready to start")
        self.assertEqual(
            [i["text"] for i in ready], [f"{visible.display_id} · Visible work (Open)"]
        )

    def test_through_the_runner_a_missing_milestone_is_a_failed_run_with_the_sentence(self):
        sched = schedules.create(
            self.ctx,
            name="m",
            job="milestone_updates",
            params={"milestone_id": 999999},
            frequency="daily",
            time_of_day="09:00",
        )
        out = schedules.run_now(self.ctx, sched.pk)
        self.assertEqual(out["status"], "failed")
        self.assertIn("not found", out["error"])


class TestSuggestedSchedules(JobBase):
    def test_a_member_with_none_is_offered_two_ordinary_schedules(self):
        keys = [s["key"] for s in schedule_defaults.suggestions_for([])]
        self.assertEqual(keys, ["morning_plan", "evening_recap"])
        for s in schedule_defaults.suggestions_for([]):
            sched = schedules.create(
                self.ctx,
                name=s["name"],
                job=s["job"],
                params=s["params"],
                frequency=s["frequency"],
                time_of_day=s["time_of_day"],
            )
            out = schedules.run_now(self.ctx, sched.pk)
            self.assertEqual(out["status"], "ok", s)

    def test_one_already_made_is_not_offered_again_even_under_another_name(self):
        sched = schedules.create(
            self.ctx,
            name="My own name",
            job="what_to_do_today",
            params={},
            frequency="daily",
            time_of_day="09:00",
        )
        keys = [s["key"] for s in schedule_defaults.suggestions_for([sched])]
        self.assertEqual(keys, ["evening_recap"])

    def test_the_same_job_at_another_time_is_a_different_schedule(self):
        sched = schedules.create(
            self.ctx,
            name="Late",
            job="what_to_do_today",
            params={},
            frequency="daily",
            time_of_day="10:00",
        )
        self.assertEqual(len(schedule_defaults.suggestions_for([sched])), 2)

    def test_a_suggestion_whose_job_was_removed_is_not_offered(self):
        from unittest import mock  # noqa: PLC0415

        with mock.patch.dict(JOBS, clear=True):
            self.assertEqual(schedule_defaults.suggestions_for([]), [])
