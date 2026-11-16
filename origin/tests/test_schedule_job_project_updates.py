"""`project_updates` (MIP-628): what moved, what slipped and what is blocked in one project.

What matters, as for `milestone_updates`: every line links to a real task, a project the member
cannot open fails the run in words (it must not read as an empty project), a task they may not
open is not named, and the job writes nothing and calls no model. Beyond that: the four
sections mean what `project_updates.py` says they mean, and the cost of a run does not grow
with the number of tasks.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services import schedule_delivery, schedules
from origin.services.schedule_jobs import JOBS
from origin.services.schedule_text import job_catalog
from origin.tests.test_schedule_jobs import JobBase


class TestProjectUpdates(JobBase):
    def run_pu(self, **params):
        params.setdefault("project_id", self.project.project_id)
        return self.run_job("project_updates", **params)

    def elsewhere(self, **extra):
        return ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed shop",
            code="CLS",
            owner=self.user2,
            project_system_user=self.user2,
            **extra,
        )

    def test_it_is_in_the_catalog_with_the_project_required(self):
        job = JOBS["project_updates"]
        self.assertTrue(job.title and job.description)
        self.assertFalse(job.uses_ai)
        row = [r for r in job_catalog() if r["job"] == "project_updates"][0]
        self.assertEqual(row["params"]["project_id"]["required"], True)
        self.assertNotIn("required", row["params"]["hours"])
        self.assertEqual((row["uses_ai"], row["effort_cost"]), (False, None))

    def test_parameters_are_checked_when_the_schedule_is_saved(self):
        def save(**params):
            return schedules.create(
                self.ctx,
                name=f"pu {len(params)} {sorted(params.items())}",
                job="project_updates",
                params=params,
                frequency="daily",
                time_of_day="09:00",
            )

        for params in (
            {},
            {"project_id": 0},
            {"project_id": "x"},
            {"project_id": 1, "hours": 0},
            {"project_id": 1, "hours": 721},
            {"project_id": 1, "milestone_id": 3},
        ):
            with self.assertRaises(schedules.ScheduleError, msg=str(params)):
                save(**params)
        save(project_id=self.project.project_id, hours=24)

    def test_it_says_what_moved_what_slipped_and_what_is_blocked_with_links(self):
        today = self.local_today()
        done = self.make_task("Ship the page", status="Closed")
        self.activity(done, self.user2, "status_changed", "WIP", "Closed", at=self.ago(hours=3))
        moved = self.make_task("Write the copy", status="WIP")
        self.activity(moved, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=5))
        went_late = self.make_task("Late one", due_date=today - timedelta(days=2))
        pushed = self.make_task("Pushed out", due_date=today + timedelta(days=20))
        self.activity(
            pushed,
            self.user2,
            "due_date_changed",
            str(today + timedelta(days=2)),
            str(today + timedelta(days=20)),
            at=self.ago(hours=4),
        )
        blocker = self.make_task("Get the API key")
        blocked = self.make_task("Call the API", status="Blocked")
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=blocked, team=self.team)
        self.activity(
            blocked, self.user2, "status_changed", "Open", "Blocked", at=self.ago(hours=2)
        )

        report = self.run_pu()

        self.assertEqual(report["title"], f"Project: {self.project.project_name}")
        self.assertEqual(
            report["summary"], "1 done, 1 moved, 2 slipped, 1 blocked in the last 7 days."
        )
        keys = {s["heading"]: s["key"] for s in report["sections"]}
        self.assertEqual(
            keys,
            {
                "Where it stands": "where_it_stands",
                "Done": "closed",
                "Moved": "changed",
                "Slipped": "overdue",
                "Blocked": "blocked",
            },
        )
        stands = self.section(report, "Where it stands")
        self.assertIn("Open", stands[0]["text"].title())
        self.assertIn("1 closed", stands[0]["text"])
        self.assertIn("1 blocked", stands[0]["text"])
        self.assertEqual([i["link"] for i in self.section(report, "Done")], [self.task_link(done)])
        self.assertIn("Open → WIP by otheruser", self.section(report, "Moved")[0]["text"])
        self.assertIn("WIP → Closed", self.section(report, "Done")[0]["text"])
        slipped = {i["task_id"]: i["detail"] for i in self.section(report, "Slipped")}
        self.assertEqual(set(slipped), {went_late.task_id, pushed.task_id})
        self.assertEqual(slipped[went_late.task_id], "went overdue")
        self.assertIn(
            f"due date moved {today + timedelta(days=2)} → {today + timedelta(days=20)} by otheruser",
            slipped[pushed.task_id],
        )
        blocked_items = self.section(report, "Blocked")
        self.assertEqual([i["link"] for i in blocked_items], [self.task_link(blocked)])
        self.assertIn(f"waiting on {blocker.display_id}", blocked_items[0]["detail"])
        self.assertTrue(blocked_items[0]["detail"].startswith("newly blocked"))
        for section in report["sections"][1:]:
            for line in section["items"]:
                self.assertEqual(
                    (line["kind"], line["project"]["id"]), ("task", self.project.project_id)
                )
        self.assertEqual(
            {s["key"]: s["count"] for s in report["stats"]},
            {"closed": 1, "changed": 1, "overdue": 2, "blocked": 1},
        )
        self.assertNotIn("workspace_content", json.dumps(report))

    def test_a_blocked_task_names_only_what_it_still_waits_on(self):
        finished = self.make_task("Already done", status="Closed")
        open_one = self.make_task("Still open")
        stuck = self.make_task("Stuck", status="Blocked")
        for blocker in (finished, open_one):
            TaskDependency.objects.create(blocker_task=blocker, blocked_task=stuck, team=self.team)
        TaskActivity.objects.all().delete()  # the dependency sync writes its own rows
        detail = self.section(self.run_pu(), "Blocked")[0]["detail"]
        self.assertEqual(detail, f"blocked, waiting on {open_one.display_id}")

    def test_newly_blocked_needs_the_last_status_change_to_be_to_blocked(self):
        stuck = self.make_task("Stuck", status="Blocked")
        TaskActivity.objects.all().delete()
        self.activity(stuck, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=1))
        self.assertEqual(self.section(self.run_pu(), "Blocked")[0]["detail"], "blocked")
        self.activity(stuck, self.user2, "status_changed", "Open", "Blocked", at=self.ago(minutes=30))
        self.assertTrue(self.section(self.run_pu(), "Blocked")[0]["detail"].startswith("newly"))

    def test_a_due_date_edited_to_a_past_day_is_not_a_slip(self):
        today = self.local_today()
        task = self.make_task("Backfilled", due_date=today - timedelta(days=1))
        self.activity(
            task,
            self.user2,
            "due_date_changed",
            str(today - timedelta(days=4)),
            str(today - timedelta(days=1)),
            at=self.ago(hours=1),
        )
        # Pushed later (4 days ago -> yesterday), so it IS a slip; moved earlier is not.
        self.assertEqual(len(self.section(self.run_pu(), "Slipped")), 1)
        other = self.make_task("Pulled in", due_date=today - timedelta(days=1))
        self.activity(
            other,
            self.user2,
            "due_date_changed",
            str(today + timedelta(days=3)),
            str(today - timedelta(days=1)),
            at=self.ago(hours=1),
        )
        slipped = [i["task_id"] for i in self.section(self.run_pu(), "Slipped")]
        self.assertEqual(slipped, [task.task_id])

    def test_it_is_a_valid_stored_report(self):
        task = self.make_task("Work", status="Closed")
        self.activity(task, self.user2, "status_changed", "WIP", "Closed", at=self.ago(hours=1))
        body = schedule_delivery.clean_report(self.run_pu(), "project_updates")
        done = [s for s in body["sections"] if s.get("key") == "closed"][0]
        self.assertEqual(done["emoji"], "🎉")
        self.assertEqual(
            (done["items"][0]["kind"], done["items"][0]["display_id"]), ("task", task.display_id)
        )

    def test_only_what_is_new_is_told_not_every_late_task_every_week(self):
        today = self.local_today()
        self.make_task("Late for a month", due_date=today - timedelta(days=30))
        self.make_task("Fine", status="Closed")
        report = self.run_pu()
        self.assertEqual(self.section(report, "Slipped"), [])
        self.assertEqual(report["summary"], "Nothing moved in the last 7 days.")
        # ... but the total is in "Where it stands".
        self.assertIn("1 overdue", self.texts(report))

    def test_a_task_that_slipped_and_was_closed_since_is_not_slipped(self):
        today = self.local_today()
        task = self.make_task("Done late", status="Closed", due_date=today - timedelta(days=1))
        self.activity(
            task,
            self.user2,
            "due_date_changed",
            str(today - timedelta(days=5)),
            str(today - timedelta(days=1)),
            at=self.ago(hours=6),
        )
        self.assertEqual(self.section(self.run_pu(), "Slipped"), [])

    def test_a_due_date_moved_earlier_is_not_a_slip(self):
        today = self.local_today()
        task = self.make_task("Sooner", due_date=today + timedelta(days=1))
        self.activity(
            task,
            self.user2,
            "due_date_changed",
            str(today + timedelta(days=9)),
            str(today + timedelta(days=1)),
            at=self.ago(hours=6),
        )
        self.assertEqual(self.section(self.run_pu(), "Slipped"), [])

    def test_the_window_is_a_parameter(self):
        task = self.make_task("Old change", status="WIP")
        self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=30))
        self.assertEqual(self.section(self.run_pu(hours=24), "Moved"), [])
        self.assertEqual(len(self.section(self.run_pu(hours=48), "Moved")), 1)
        self.assertIn("last 2 days", self.run_pu(hours=48)["summary"])
        self.assertIn("last 30 hours", self.run_pu(hours=30)["summary"])

    def test_only_this_projects_tasks_are_in_the_report(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        mine = self.make_task("In this project", status="WIP")
        theirs = self.make_task("In the other project", status="WIP", project=other)
        for task in (mine, theirs):
            self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=2))
        shown = json.dumps(self.run_pu())
        self.assertIn("In this project", shown)
        self.assertNotIn("In the other project", shown)

    def test_a_milestones_own_row_is_not_a_task_of_the_work(self):
        row = self.make_task("v1 launch", status="Blocked", is_milestone=True)
        MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="v1 launch",
            status="Blocked",
            task=row,
        )
        self.activity(row, self.user2, "status_changed", "Open", "Blocked", at=self.ago(hours=1))
        shown = json.dumps(self.run_pu())
        self.assertNotIn("v1 launch", shown)

    def test_a_task_under_a_closed_parent_is_not_late_or_blocked_when_the_flag_is_on(self):
        today = self.local_today()
        parent = self.make_task("Parent", status="Closed")
        late = self.make_task(
            "Child late", parent_task_id=parent.task_id, due_date=today - timedelta(days=1)
        )
        stuck = self.make_task("Child stuck", parent_task_id=parent.task_id, status="Blocked")
        plain_late = self.make_task("Plain late", due_date=today - timedelta(days=1))
        off = self.run_pu()
        self.assertEqual(len(self.section(off, "Slipped")), 2)
        self.assertEqual(len(self.section(off, "Blocked")), 1)
        moved = self.make_task("Child moved", parent_task_id=parent.task_id, status="WIP")
        self.activity(moved, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=1))
        self.assertEqual(len(self.section(self.run_pu(), "Moved")), 1)
        flagged = override_settings(
            SEARCH_ENGINE={**settings.SEARCH_ENGINE, "AGENT_CLOSED_ANCESTOR_RULE": True}
        )
        with flagged:
            report = self.run_pu()
        self.assertEqual(
            [i["task_id"] for i in self.section(report, "Slipped")], [plain_late.task_id]
        )
        self.assertEqual(self.section(report, "Blocked"), [])
        self.assertEqual(self.section(report, "Moved"), [])
        self.assertNotIn(late.display_id, json.dumps(report["sections"][1:]))
        self.assertNotIn(stuck.display_id, json.dumps(report["sections"][1:]))

    def test_a_list_is_capped_with_a_more_line_that_says_so(self):
        for n in range(11):
            task = self.make_task(f"Closed {n}", status="Closed")
            self.activity(
                task, self.user2, "status_changed", "WIP", "Closed", at=self.ago(hours=1 + n)
            )
        done = self.section(self.run_pu(), "Done")
        self.assertEqual(len(done), 9)
        self.assertEqual((done[-1]["kind"], done[-1]["text"]), ("more", "and 3 more tasks"))
        self.assertTrue(all(i["kind"] == "task" for i in done[:-1]))
        # Newest first: the one closed an hour ago leads.
        self.assertIn("Closed 0", done[0]["text"])

    def test_a_project_the_member_cannot_open_fails_in_words_not_as_empty(self):
        from origin.services.schedules import ScheduleRunError  # noqa: PLC0415

        hidden = self.elsewhere()
        self.make_task("Secret plan", project=hidden, assignee=self.user2)
        with self.assertRaises(ScheduleRunError) as caught:
            self.run_pu(project_id=hidden.project_id)
        self.assertIn("not a member", caught.exception.public)
        for secret in ("Closed shop", "Secret plan"):
            self.assertNotIn(secret, caught.exception.public)
        with self.assertRaises(ScheduleRunError) as caught:
            self.run_pu(project_id=999999)
        self.assertIn("not found", caught.exception.public)

    def test_a_project_of_another_team_reads_as_a_missing_one(self):
        from origin.models.common.team_models import TeamMaster  # noqa: PLC0415
        from origin.services.schedules import ScheduleRunError  # noqa: PLC0415

        team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.user
        )
        foreign = ProjectMaster.objects.create(
            team=team,
            project_name="Foreign",
            code="FOR",
            owner=self.user,
            project_system_user=self.user,
        )
        with self.assertRaises(ScheduleRunError) as caught:
            self.run_pu(project_id=foreign.project_id)
        self.assertIn("not found", caught.exception.public)
        self.assertNotIn("Foreign", caught.exception.public)

    def test_a_deleted_project_reads_as_a_missing_one(self):
        from origin.services.schedules import ScheduleRunError  # noqa: PLC0415

        gone = self.elsewhere(is_deleted=True)
        from origin.models.project.prj_models import ProjectMembers  # noqa: PLC0415

        ProjectMembers.objects.create(team=self.team, project=gone, attendee=self.user)
        with self.assertRaises(ScheduleRunError) as caught:
            self.run_pu(project_id=gone.project_id)
        self.assertIn("not found", caught.exception.public)

    def test_through_the_runner_a_missing_project_is_a_failed_run_with_the_sentence(self):
        sched = schedules.create(
            self.ctx,
            name="p",
            job="project_updates",
            params={"project_id": 999999},
            frequency="daily",
            time_of_day="09:00",
        )
        out = schedules.run_now(self.ctx, sched.pk)
        self.assertEqual(out["status"], "failed")
        self.assertIn("not found", out["error"])

    def test_through_the_runner_a_project_that_exists_is_an_ok_run(self):
        task = self.make_task("Work", status="Closed")
        self.activity(task, self.user2, "status_changed", "WIP", "Closed", at=self.ago(hours=1))
        sched = schedules.create(
            self.ctx,
            name="p",
            job="project_updates",
            params={"project_id": self.project.project_id},
            frequency="daily",
            time_of_day="09:00",
        )
        self.assertEqual(schedules.run_now(self.ctx, sched.pk)["status"], "ok")

    def test_a_task_the_member_may_not_open_is_not_named_nor_given_away_by_a_count(self):
        # Inside a project the member is in, every task is theirs to open today, so the check
        # (`LabelAccess`) is the defence the day that stops being true: make it refuse one.
        from origin.search_engine.agent.label_acl import LabelAccess  # noqa: PLC0415

        hidden = self.make_task("Hidden work", status="Closed")
        visible = self.make_task("Visible work", status="Closed")
        for task in (hidden, visible):
            self.activity(task, self.user2, "status_changed", "WIP", "Closed", at=self.ago(hours=2))
        real = LabelAccess.task_readable
        with mock.patch.object(
            LabelAccess,
            "task_readable",
            autospec=True,
            side_effect=lambda me, task: task.task_id != hidden.task_id and real(me, task),
        ):
            report = self.run_pu()
        shown = json.dumps(report)
        self.assertIn("Visible work", shown)
        self.assertNotIn("Hidden work", shown)
        self.assertEqual([i["kind"] for i in self.section(report, "Done")], ["task"])
        self.assertEqual(report["summary"], "1 done in the last 7 days.")

    def test_it_writes_nothing(self):
        task = self.make_task("Quiet", status="WIP")
        self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=1))
        before = (TaskMaster.objects.count(), MilestoneMaster.objects.count())
        self.run_pu()
        self.assertEqual(before, (TaskMaster.objects.count(), MilestoneMaster.objects.count()))

    def test_the_number_of_queries_does_not_grow_with_the_tasks(self):
        today = self.local_today()

        def fill(n):
            for i in range(n):
                closed = self.make_task(f"Closed {n}-{i}", status="Closed")
                self.activity(
                    closed, self.user2, "status_changed", "WIP", "Closed", at=self.ago(hours=1)
                )
                self.make_task(f"Late {n}-{i}", due_date=today - timedelta(days=1))
                blocked = self.make_task(f"Blocked {n}-{i}", status="Blocked")
                self.activity(
                    blocked, self.user2, "status_changed", "Open", "Blocked", at=self.ago(hours=1)
                )

        fill(1)
        with CaptureQueriesContext(connection) as small:
            self.run_pu()
        fill(6)
        with CaptureQueriesContext(connection) as large:
            self.run_pu()
        self.assertEqual(len(small), len(large), "queries grew with the number of tasks")
