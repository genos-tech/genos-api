"""Converting a root task to a milestone and a milestone back to a task (MIP-1042).

A milestone is a task row (`is_milestone=True`) plus a `MilestoneMaster` for the
milestone-only data (its id, folder, sprint, several assignees). A conversion
keeps the SAME task row, so its display id, body, comments, history,
collaborators and custom fields stay, and only adds or retires the side row.

The rules (decided with Ken):
  * task -> milestone: only a ROOT task (no parent, no milestone, not already a
    milestone), in a project the caller belongs to;
  * the parent/child tree is never changed, in either direction: a root task's
    sub-tasks stay under it, and a milestone's tasks stay under its row as
    ordinary sub-tasks;
  * milestone -> task: the first assignee becomes the task's assignee, the others
    its collaborators; folder and sprint are dropped;
  * an old milestone link redirects: reading a converted milestone answers 410
    with the task it became.
"""

from __future__ import annotations

from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneFolder, MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskMaster

User = get_user_model()

TO_MILESTONE = "/api/v2/task/convert-to-milestone/"
TO_TASK = "/api/v2/milestone/convert-to-task/"
PURGE = "origin.services.task_milestone_conversion.purge_milestone"


class ConversionBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner", email="owner@test.com", password="pw123456"
        )
        self.alice = User.objects.create_user(
            username="alice", email="alice@test.com", password="pw123456"
        )
        self.bob = User.objects.create_user(
            username="bob", email="bob@test.com", password="pw123456"
        )
        self.stranger = User.objects.create_user(
            username="stranger", email="stranger@test.com", password="pw123456"
        )
        self.team = TeamMaster.objects.create(
            team_name="Conv Team", team_email="conv@test.com", owner=self.owner
        )
        for u in (self.owner, self.alice, self.bob):
            TeamMembers.objects.create(team=self.team, attendee=u)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Conv",
            code="CNV",
            owner=self.owner,
            project_system_user=self.owner,
        )
        for u in (self.owner, self.alice, self.bob):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=u)
        self.sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S1",
            sequence_number=1,
            start_date="2026-11-01",
            end_date="2026-11-14",
        )
        self.client = APIClient()
        self.auth(self.owner)

    def auth(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    # -- fixtures --

    def task(self, title, **kw):
        defaults = dict(
            team=self.team,
            project=self.project,
            title=title,
            status="Open",
            reporter=self.owner,
            assignee=self.alice,
            content=[{"type": "paragraph", "content": [{"type": "text", "text": f"{title} body"}]}],
            links=[],
            tags=[],
        )
        defaults.update(kw)
        return TaskMaster.objects.create(**defaults)

    def milestone(self, title="Launch", assignees=(), **kw):
        backing = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title=title,
            status="Open",
            is_milestone=True,
            reporter=self.owner,
            assignee=assignees[0] if assignees else self.owner,
            content=[{"type": "paragraph", "content": [{"type": "text", "text": "goal"}]}],
        )
        m = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            task=backing,
            title=title,
            reporter=self.owner,
            description=backing.content,
            **kw,
        )
        backing.milestone = m
        backing.save(update_fields=["milestone", "ts_updated_at"])
        for u in assignees:
            MilestoneAssignees.objects.create(milestone=m, team=self.team, user=u)
        return m

    def in_milestone(self, title, m, parent=None):
        return self.task(
            title,
            milestone=m,
            sprint=m.sprint,
            parent_task_id=(parent or m.task).task_id,
        )

    def to_milestone(self, task, expect=200):
        res = self.client.post(TO_MILESTONE, {"task_id": task.task_id}, format="json")
        self.assertEqual(res.status_code, expect, getattr(res, "data", None))
        return res

    def to_task(self, milestone, expect=200):
        res = self.client.post(TO_TASK, {"milestone_id": milestone.milestone_id}, format="json")
        self.assertEqual(res.status_code, expect, getattr(res, "data", None))
        return res

    def fresh(self, row):
        return TaskMaster.objects.get(pk=row.pk)

    def tree(self, ids):
        """parent / root of each task: the part a conversion must never change."""
        return {
            r.task_id: (r.parent_task_id, r.root_task_id)
            for r in TaskMaster.objects.filter(task_id__in=ids)
        }


class TaskToMilestoneTests(ConversionBase):
    def test_a_root_task_becomes_a_milestone_on_the_same_row(self):
        t = self.task(
            "Plan the launch",
            priority="High",
            priority_code=3,
            effort_level="Moderate",
            status="WIP",
            due_date=date(2026, 12, 1),
            start_date=date(2026, 11, 10),
            tags=[{"tagName": "Feature"}],
            links=[{"id": "1", "url": "https://example.com", "title": "Doc"}],
        )
        display_id = t.display_id

        res = self.to_milestone(t)

        row = self.fresh(t)
        m = MilestoneMaster.objects.get(task=row)
        self.assertTrue(row.is_milestone)
        self.assertEqual(row.milestone_id, m.milestone_id)
        self.assertEqual(row.display_id, display_id)
        self.assertEqual(m.title, "Plan the launch")
        self.assertEqual(m.description, row.content)
        self.assertEqual(
            (m.status, m.priority, m.priority_code, m.effort_level, m.due_date, m.start_date),
            ("WIP", "High", 3, "Moderate", date(2026, 12, 1), date(2026, 11, 10)),
        )
        self.assertEqual(m.tags, [{"tagName": "Feature"}])
        self.assertEqual(m.links, row.links)
        self.assertEqual(m.reporter_id, self.owner.id)
        self.assertEqual(m.project_id, self.project.project_id)
        self.assertIsNone(m.sprint_id)
        self.assertIsNone(m.folder_id)
        self.assertFalse(m.is_deleted)
        self.assertIsNone(m.converted_to_task_id)
        # What the frontend needs to navigate and refresh.
        self.assertEqual(res.data["milestone"]["milestoneId"], m.milestone_id)
        self.assertEqual(res.data["task"]["taskId"], t.task_id)
        self.assertEqual(res.data["task"]["displayId"], display_id)
        self.assertEqual(res.data["task"]["projectId"], self.project.project_id)

    def test_the_assignee_becomes_the_milestones_assignee(self):
        t = self.task("T", assignee=self.alice)
        self.to_milestone(t)
        m = MilestoneMaster.objects.get(task_id=t.task_id)
        self.assertEqual(
            list(MilestoneAssignees.objects.filter(milestone=m).values_list("user_id", flat=True)),
            [self.alice.id],
        )
        # and the row keeps it
        self.assertEqual(self.fresh(t).assignee_id, self.alice.id)

    def test_a_task_with_no_assignee_stays_unassigned(self):
        t = self.task("T", assignee=None)
        self.to_milestone(t)
        m = MilestoneMaster.objects.get(task_id=t.task_id)
        self.assertFalse(MilestoneAssignees.objects.filter(milestone=m).exists())
        self.assertIsNone(self.fresh(t).assignee_id)

    def test_what_belongs_to_the_row_stays(self):
        t = self.task("T", custom_field_values={"7": "x"})
        t.collaborators.set([self.bob])
        TaskComments.objects.create(task=t, sender=self.owner, comment_id=1, comment_body=[{}])
        self.to_milestone(t)

        row = self.fresh(t)
        self.assertEqual(row.custom_field_values, {"7": "x"})
        self.assertEqual(list(row.collaborators.values_list("id", flat=True)), [self.bob.id])
        self.assertEqual(TaskComments.objects.filter(task=row).count(), 1)

    def test_sub_tasks_at_every_depth_join_the_milestone_and_the_tree_is_untouched(self):
        t = self.task("Root")
        a = self.task("A", parent_task_id=t.task_id)
        b = self.task("B", parent_task_id=a.task_id)
        c = self.task("C", parent_task_id=t.task_id)
        before = self.tree([t.task_id, a.task_id, b.task_id, c.task_id])
        old = timezone.now()
        TaskMaster.objects.filter(pk__in=[a.pk, b.pk, c.pk]).update(ts_updated_at=old)

        self.to_milestone(t)

        m = MilestoneMaster.objects.get(task_id=t.task_id)
        for row in (a, b, c):
            row = self.fresh(row)
            self.assertEqual(row.milestone_id, m.milestone_id, row.title)
            self.assertGreater(row.ts_updated_at, old, f"{row.title} not stamped for sync")
        self.assertEqual(self.tree([t.task_id, a.task_id, b.task_id, c.task_id]), before)

    def test_it_counts_those_sub_tasks_as_the_milestones_work(self):
        t = self.task("Root")
        self.task("A", parent_task_id=t.task_id, status="Closed")
        self.task("B", parent_task_id=t.task_id)
        res = self.to_milestone(t)
        self.assertEqual(res.data["milestone"]["tasksTotal"], 2)
        self.assertEqual(res.data["milestone"]["tasksClosed"], 1)

    def test_the_milestone_shows_in_the_project_list(self):
        t = self.task("Root")
        self.to_milestone(t)
        res = self.client.get("/api/v2/milestone/list/", {"project_id": self.project.project_id})
        self.assertEqual([m["taskId"] for m in res.data["milestones"]], [t.task_id])

    def test_the_conversion_is_in_the_tasks_history(self):
        t = self.task("Root")
        self.to_milestone(t)
        m = MilestoneMaster.objects.get(task_id=t.task_id)
        rows = TaskActivity.objects.filter(task=t, action_type="milestone_changed")
        self.assertEqual([r.new_value for r in rows], [m.milestone_id])

    def test_an_open_browser_hears_about_it(self):
        t = self.task("Root")
        TaskMaster.objects.filter(pk=t.pk).update(ts_updated_at=timezone.now())
        checkpoint = timezone.now()
        self.to_milestone(t)
        res = self.client.get(
            "/api/v2/task/getProjectTasks/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "since": checkpoint.isoformat(),
            },
        )
        rows = {r["id"]: r for r in res.data["data"]["tasks"]}
        self.assertTrue(rows[str(t.task_id)]["isMilestone"])

    # -- who and what is refused --

    def refused(self, task, code):
        res = self.to_milestone(task, expect=400)
        self.assertEqual(res.data["code"], code, res.data)
        self.assertFalse(MilestoneMaster.objects.filter(task_id=task.task_id).exists())
        self.assertFalse(self.fresh(task).is_milestone)

    def test_a_sub_task_is_refused(self):
        root = self.task("Root")
        self.refused(self.task("Sub", parent_task_id=root.task_id), "not_a_root_task")

    def test_a_task_inside_a_milestone_is_refused(self):
        m = self.milestone()
        self.refused(self.in_milestone("In", m), "not_a_root_task")

    def test_a_task_filed_in_a_milestone_without_a_parent_is_refused(self):
        m = self.milestone()
        self.refused(self.task("Odd", milestone=m), "task_in_milestone")

    def test_a_milestone_is_refused(self):
        m = self.milestone()
        res = self.to_milestone(m.task, expect=400)
        self.assertEqual(res.data["code"], "already_a_milestone")

    def test_a_deleted_task_is_refused(self):
        self.refused(self.task("Gone", is_deleted=True), "task_deleted")
        self.refused(self.task("Gone too", status="Deleted"), "task_deleted")

    def test_the_create_forms_scaffold_row_is_refused(self):
        self.refused(self.task("scaffold", is_init_task=True), "task_not_saved")

    def test_a_non_member_cannot_tell_the_task_exists(self):
        t = self.task("Root")
        self.auth(self.stranger)
        self.to_milestone(t, expect=404)
        self.assertFalse(self.fresh(t).is_milestone)

    def test_an_assignee_who_is_not_on_the_project_is_refused(self):
        outsider = User.objects.create_user(
            username="out", email="out@test.com", password="pw123456"
        )
        t = self.task("Root", assignee=outsider)
        self.auth(outsider)
        self.to_milestone(t, expect=404)

    def test_a_missing_or_malformed_task_id(self):
        for body in ({}, {"task_id": "abc"}, {"task_id": None}, {"task_id": 10**9}):
            res = self.client.post(TO_MILESTONE, body, format="json")
            self.assertIn(res.status_code, (400, 404), body)

    def test_it_needs_a_signed_in_user(self):
        t = self.task("Root")
        self.client.credentials()
        res = self.client.post(TO_MILESTONE, {"task_id": t.task_id}, format="json")
        self.assertIn(res.status_code, (401, 403))

    def test_converting_twice_is_refused_and_does_not_make_a_second_milestone(self):
        t = self.task("Root")
        self.to_milestone(t)
        res = self.to_milestone(t, expect=400)
        self.assertEqual(res.data["code"], "already_a_milestone")
        self.assertEqual(MilestoneMaster.objects.filter(task_id=t.task_id).count(), 1)


class MilestoneToTaskTests(ConversionBase):
    def test_a_milestone_becomes_a_root_task_on_the_same_row(self):
        folder = MilestoneFolder.objects.create(team=self.team, project=self.project, name="F")
        m = self.milestone("Launch", sprint=self.sprint, folder=folder)
        row = m.task
        display_id = row.display_id
        row.sprint = self.sprint
        row.save(update_fields=["sprint", "ts_updated_at"])

        res = self.to_task(m)

        row = self.fresh(row)
        self.assertFalse(row.is_milestone)
        self.assertIsNone(row.milestone_id)
        self.assertIsNone(row.sprint_id)
        self.assertIsNone(row.parent_task_id)
        self.assertEqual(row.display_id, display_id)
        self.assertFalse(row.is_deleted)
        self.assertNotEqual(row.status, "Deleted")
        self.assertEqual(res.data["task"]["taskId"], row.task_id)
        self.assertEqual(res.data["task"]["displayId"], display_id)
        self.assertEqual(res.data["task"]["isMilestone"], False)
        self.assertEqual(res.data["convertedMilestoneId"], m.milestone_id)

    def test_the_milestone_is_retired_and_remembers_the_task(self):
        folder = MilestoneFolder.objects.create(team=self.team, project=self.project, name="F")
        m = self.milestone("Launch", sprint=self.sprint, folder=folder)
        task_id = m.task_id
        self.to_task(m)

        m = MilestoneMaster.objects.get(pk=m.pk)
        self.assertTrue(m.is_deleted)
        self.assertEqual(m.converted_to_task_id, task_id)
        # Unlinked from the row, so nothing that follows a milestone to its
        # backing row (the delete path above all) can reach it again.
        self.assertIsNone(m.task_id)
        self.assertIsNone(m.sprint_id)
        self.assertIsNone(m.folder_id)
        self.assertFalse(MilestoneAssignees.objects.filter(milestone=m).exists())

    def test_it_is_gone_from_the_milestone_list(self):
        m = self.milestone("Launch")
        self.to_task(m)
        res = self.client.get("/api/v2/milestone/list/", {"project_id": self.project.project_id})
        self.assertEqual(res.data["milestones"], [])

    def test_the_tasks_in_it_stay_under_the_row_as_sub_tasks(self):
        m = self.milestone("Launch", sprint=self.sprint)
        a = self.in_milestone("A", m)
        b = self.in_milestone("B", m, parent=a)
        c = self.in_milestone("C", m)
        ids = [m.task_id, a.task_id, b.task_id, c.task_id]
        before = self.tree(ids)
        old = timezone.now()
        TaskMaster.objects.filter(pk__in=[a.pk, b.pk, c.pk]).update(ts_updated_at=old)

        self.to_task(m)

        # The tree is exactly what it was.
        self.assertEqual(self.tree(ids), before)
        for row in (a, b, c):
            row = self.fresh(row)
            self.assertIsNone(row.milestone_id, row.title)
            self.assertIsNone(row.sprint_id, row.title)
            self.assertGreater(row.ts_updated_at, old, f"{row.title} not stamped for sync")
        # They are not detached, re-rooted, or deleted, which is what a milestone delete does.
        self.assertEqual(self.fresh(a).parent_task_id, m.task_id)
        self.assertEqual(self.fresh(a).root_task_id, m.task_id)

    def test_the_first_assignee_is_the_assignee_and_the_rest_are_collaborators(self):
        m = self.milestone("Launch", assignees=[self.alice, self.bob, self.owner])
        row = m.task
        row.collaborators.set([self.stranger])
        self.to_task(m)

        row = self.fresh(row)
        self.assertEqual(row.assignee_id, self.alice.id)
        self.assertEqual(
            set(row.collaborators.values_list("id", flat=True)),
            {self.stranger.id, self.bob.id, self.owner.id},
        )
        # The retired milestone keeps no assignees: they are on the task now.
        self.assertFalse(MilestoneAssignees.objects.filter(milestone=m).exists())

    def test_a_teammate_off_the_project_does_not_become_a_collaborator(self):
        # A milestone's assignee may be a teammate who is not on the project (MIP-915). They
        # could not open the task they would now collaborate on, so they are left off (MIP-1567).
        mate = User.objects.create_user(username="mate", email="mate@test.com", password="pw123456")
        TeamMembers.objects.create(team=self.team, attendee=mate)
        m = self.milestone("Launch", assignees=[self.alice, mate, self.bob])
        self.to_task(m)

        row = self.fresh(m.task)
        self.assertEqual(row.assignee_id, self.alice.id)
        self.assertEqual(list(row.collaborators.values_list("id", flat=True)), [self.bob.id])

    def test_the_oldest_assignee_wins_whatever_the_row_currently_says(self):
        # The row's own assignee column can lag the milestone's assignees; the
        # milestone's first (oldest) one is the answer.
        m = self.milestone("Launch", assignees=[self.bob, self.alice])
        m.task.assignee = self.owner
        m.task.save(update_fields=["assignee", "ts_updated_at"])
        self.to_task(m)
        row = self.fresh(m.task)
        self.assertEqual(row.assignee_id, self.bob.id)
        self.assertEqual(list(row.collaborators.values_list("id", flat=True)), [self.alice.id])

    def test_nobody_is_both_the_assignee_and_a_collaborator(self):
        m = self.milestone("Launch", assignees=[self.alice, self.bob])
        m.task.collaborators.set([self.alice])
        self.to_task(m)
        row = self.fresh(m.task)
        self.assertNotIn(self.alice.id, row.collaborators.values_list("id", flat=True))
        self.assertEqual(list(row.collaborators.values_list("id", flat=True)), [self.bob.id])

    def test_a_milestone_with_no_assignee_keeps_the_rows_assignee(self):
        m = self.milestone("Launch")
        m.task.assignee = self.bob
        m.task.save(update_fields=["assignee", "ts_updated_at"])
        self.to_task(m)
        self.assertEqual(self.fresh(m.task).assignee_id, self.bob.id)
        self.assertEqual(self.fresh(m.task).collaborators.count(), 0)

    def test_what_belongs_to_the_row_stays(self):
        m = self.milestone("Launch")
        row = m.task
        row.custom_field_values = {"7": "x"}
        row.save(update_fields=["custom_field_values", "ts_updated_at"])
        TaskComments.objects.create(task=row, sender=self.owner, comment_id=1, comment_body=[{}])
        self.to_task(m)
        row = self.fresh(row)
        self.assertEqual(row.custom_field_values, {"7": "x"})
        self.assertEqual(TaskComments.objects.filter(task=row).count(), 1)
        self.assertEqual(row.title, "Launch")

    def test_the_search_entry_of_the_milestone_goes_and_the_tasks_stays(self):
        m = self.milestone("Launch")
        with mock.patch(PURGE) as purge_milestone:
            self.to_task(m)
        purge_milestone.assert_called_once_with(m.milestone_id)

    def test_a_legacy_milestone_with_no_backing_row_is_converted_too(self):
        m = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Old", reporter=self.owner
        )
        self.assertIsNone(m.task_id)
        res = self.to_task(m)
        row = TaskMaster.objects.get(task_id=res.data["task"]["taskId"])
        self.assertEqual(row.title, "Old")
        self.assertFalse(row.is_milestone)

    def test_the_conversion_is_in_the_tasks_history(self):
        m = self.milestone("Launch")
        self.to_task(m)
        rows = TaskActivity.objects.filter(task=m.task, action_type="milestone_changed")
        self.assertEqual([r.old_value for r in rows if r.new_value is None], [m.milestone_id])

    # -- who and what is refused --

    def test_a_non_member_cannot_tell_the_milestone_exists(self):
        m = self.milestone("Launch")
        self.auth(self.stranger)
        self.to_task(m, expect=404)
        self.assertTrue(self.fresh(m.task).is_milestone)

    def test_an_unknown_or_already_deleted_milestone_is_a_404(self):
        res = self.client.post(TO_TASK, {"milestone_id": 10**9}, format="json")
        self.assertEqual(res.status_code, 404)
        m = self.milestone("Launch")
        m.is_deleted = True
        m.save(update_fields=["is_deleted", "ts_updated_at"])
        self.to_task(m, expect=404)

    def test_a_malformed_milestone_id(self):
        for body in ({}, {"milestone_id": "abc"}, {"milestone_id": None}):
            res = self.client.post(TO_TASK, body, format="json")
            self.assertIn(res.status_code, (400, 404), body)

    def test_converting_twice_is_a_404(self):
        m = self.milestone("Launch")
        self.to_task(m)
        self.to_task(m, expect=404)

    def test_it_needs_a_signed_in_user(self):
        m = self.milestone("Launch")
        self.client.credentials()
        res = self.client.post(TO_TASK, {"milestone_id": m.milestone_id}, format="json")
        self.assertIn(res.status_code, (401, 403))


class OldMilestoneLinksRedirectTests(ConversionBase):
    """`GET /milestone/<id>/` for a converted milestone is a 410 that names the
    task it became, so the frontend can follow it."""

    def test_the_old_link_answers_410_with_the_task(self):
        m = self.milestone("Launch")
        row = m.task
        self.to_task(m)

        res = self.client.get(f"/api/v2/milestone/{m.milestone_id}/")

        self.assertEqual(res.status_code, 410, res.data)
        self.assertEqual(res.data["code"], "milestone_converted_to_task")
        moved = res.data["convertedToTask"]
        self.assertEqual(moved["taskId"], row.task_id)
        self.assertEqual(moved["projectId"], self.project.project_id)
        self.assertEqual(moved["displayId"], row.display_id)
        self.assertFalse(moved["isMilestone"])
        self.assertIsNone(moved["milestoneId"])

    def test_a_task_converted_back_points_at_its_new_milestone(self):
        m = self.milestone("Launch")
        row = m.task
        self.to_task(m)
        res = self.to_milestone(self.fresh(row))
        new_id = res.data["milestone"]["milestoneId"]

        old = self.client.get(f"/api/v2/milestone/{m.milestone_id}/")

        self.assertEqual(old.status_code, 410)
        self.assertTrue(old.data["convertedToTask"]["isMilestone"])
        self.assertEqual(old.data["convertedToTask"]["milestoneId"], new_id)
        self.assertNotEqual(new_id, m.milestone_id)

    def test_a_non_member_still_gets_a_plain_404(self):
        m = self.milestone("Launch")
        self.to_task(m)
        self.auth(self.stranger)
        res = self.client.get(f"/api/v2/milestone/{m.milestone_id}/")
        self.assertEqual(res.status_code, 404)
        self.assertNotIn("convertedToTask", res.data)

    def test_someone_outside_the_milestones_project_gets_a_404_even_if_they_can_open_the_task(self):
        # An assignee can open a task without belonging to its project (the task ACL),
        # but the milestone's own link still answers by its PROJECT's membership.
        m = self.milestone("Launch")
        row = m.task
        self.to_task(m)
        outsider = User.objects.create_user(
            username="out", email="out@test.com", password="pw123456"
        )
        TaskMaster.objects.filter(pk=row.pk).update(assignee=outsider)
        self.auth(outsider)
        res = self.client.get(f"/api/v2/milestone/{m.milestone_id}/")
        self.assertEqual(res.status_code, 404)
        self.assertNotIn("convertedToTask", res.data)

    def test_someone_who_can_no_longer_see_the_task_gets_a_404(self):
        # The task moved to a project this person is not in.
        m = self.milestone("Launch")
        row = m.task
        self.to_task(m)
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.owner,
            project_system_user=self.owner,
        )
        TaskMaster.objects.filter(pk=row.pk).update(project=other, assignee=None, reporter=None)
        self.auth(self.bob)  # a member of the milestone's project, not of Other
        res = self.client.get(f"/api/v2/milestone/{m.milestone_id}/")
        self.assertEqual(res.status_code, 404)

    def test_a_milestone_deleted_the_ordinary_way_is_still_a_404(self):
        m = self.milestone("Launch")
        with (
            mock.patch("origin.services.milestone_manage.purge_milestone"),
            mock.patch("origin.services.milestone_manage.purge_task"),
        ):
            self.assertEqual(
                self.client.delete(f"/api/v2/milestone/{m.milestone_id}/").status_code, 200
            )
        res = self.client.get(f"/api/v2/milestone/{m.milestone_id}/")
        self.assertEqual(res.status_code, 404)

    def test_deleting_the_retired_milestone_does_not_touch_the_task(self):
        m = self.milestone("Launch")
        row = m.task
        kid = self.in_milestone("kid", m)
        self.to_task(m)
        with (
            mock.patch("origin.services.milestone_manage.purge_milestone"),
            mock.patch("origin.services.milestone_manage.purge_task") as purge_task,
        ):
            self.client.delete(f"/api/v2/milestone/{m.milestone_id}/")
        purge_task.assert_not_called()
        row = self.fresh(row)
        self.assertFalse(row.is_deleted)
        self.assertNotEqual(row.status, "Deleted")
        self.assertEqual(self.fresh(kid).parent_task_id, row.task_id)


class RoundTripTests(ConversionBase):
    def snapshot(self, row, kids):
        row = self.fresh(row)
        return {
            "id": row.task_id,
            "display": row.display_id,
            "title": row.title,
            "body": row.content,
            "assignee": row.assignee_id,
            "comments": TaskComments.objects.filter(task=row).count(),
            "tree": self.tree([row.task_id, *[k.task_id for k in kids]]),
        }

    def test_task_to_milestone_to_task_changes_nothing_that_matters(self):
        t = self.task("Root")
        a = self.task("A", parent_task_id=t.task_id)
        b = self.task("B", parent_task_id=a.task_id)
        TaskComments.objects.create(task=t, sender=self.owner, comment_id=1, comment_body=[{}])
        before = self.snapshot(t, [a, b])

        self.to_milestone(t)
        m = MilestoneMaster.objects.get(task_id=t.task_id)
        self.to_task(m)

        self.assertEqual(self.snapshot(t, [a, b]), before)
        for row in (a, b):
            self.assertIsNone(self.fresh(row).milestone_id)

    def test_milestone_with_tasks_to_task_to_milestone_again(self):
        m = self.milestone("Launch", assignees=[self.alice])
        a = self.in_milestone("A", m)
        b = self.in_milestone("B", m, parent=a)
        row = m.task
        before = self.snapshot(row, [a, b])

        self.to_task(m)
        again = self.to_milestone(self.fresh(row))

        new = MilestoneMaster.objects.get(milestone_id=again.data["milestone"]["milestoneId"])
        self.assertNotEqual(new.milestone_id, m.milestone_id)
        self.assertEqual(self.snapshot(row, [a, b]), before)
        for r in (a, b):
            self.assertEqual(self.fresh(r).milestone_id, new.milestone_id)
        # one live milestone, one retired
        live = MilestoneMaster.objects.filter(project=self.project, is_deleted=False)
        self.assertEqual([x.milestone_id for x in live], [new.milestone_id])
