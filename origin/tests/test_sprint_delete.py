"""Deleting a sprint (MIP-941).

Two things were wrong, and a third was left half-done:

* Deleting a project's newest sprint made its sprint list answer 500, for good.
  The list tops the project up with auto-generated sprints, and it derived the
  next `sequence_number` from the sprints that are still alive, while the unique
  constraint on `(project, sequence_number)` counts the deleted ones too.
* A deleted auto sprint came straight back: the list saw its slot empty and filled
  it again, under a new number. A delete that doesn't stick isn't one.
* The delete detached the sprint's milestones with a bare queryset `.update()`,
  which skips `auto_now`. A browser that already holds a row only hears about a
  change as "changed since my checkpoint", so it never learned the milestone had
  left the sprint (the same bug MIP-936 fixed for a milestone delete). And the
  tasks in those milestones kept pointing at the dead sprint.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase


class SprintDeleteBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Release",
            code="REL",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.authenticate()

    def listing(self):
        res = self.client.get(
            "/api/v2/sprint/list/",
            {"team_id": str(self.team.team_id), "project_id": self.project.project_id},
        )
        return res

    def live(self):
        return list(
            Sprint.objects.filter(project=self.project, is_deleted=False).order_by("start_date")
        )

    def delete(self, sprint):
        return self.client.delete(
            f"/api/v2/sprint/{sprint.sprint_id}/", {"team_id": str(self.team.team_id)}
        )


class TheListSurvivesADelete(SprintDeleteBase):
    def test_deleting_the_newest_sprint_does_not_break_the_list(self):
        self.assertEqual(self.listing().status_code, 200)
        newest = Sprint.objects.filter(project=self.project).order_by("-sequence_number").first()

        self.assertEqual(self.delete(newest).status_code, 200)

        again = self.listing()
        self.assertEqual(again.status_code, 200, again.content)

    def test_the_list_keeps_working_after_every_delete(self):
        self.assertEqual(self.listing().status_code, 200)
        for _ in range(3):
            newest = (
                Sprint.objects.filter(project=self.project, is_deleted=False)
                .order_by("-sequence_number")
                .first()
            )
            self.delete(newest)
            self.assertEqual(self.listing().status_code, 200)

    def test_a_deleted_sprint_stays_deleted(self):
        self.listing()
        upcoming = [s for s in self.live() if s.start_date > timezone.localdate()][0]
        gone = (upcoming.sprint_id, upcoming.start_date)

        self.delete(upcoming)
        self.listing()

        starts = [s.start_date for s in self.live()]
        self.assertNotIn(gone[1], starts, "the slot must not be filled in again")
        self.assertTrue(Sprint.objects.get(sprint_id=gone[0]).is_deleted)

    def test_the_list_still_keeps_its_horizon_after_a_delete(self):
        """The upcoming window is topped up past the deleted slot instead."""
        self.listing()
        before = len(self.live())
        upcoming = [s for s in self.live() if s.start_date > timezone.localdate()][0]

        self.delete(upcoming)
        self.listing()

        self.assertEqual(len(self.live()), before)

    def test_new_sprints_never_reuse_a_deleted_number(self):
        self.listing()
        numbers = set(
            Sprint.objects.filter(project=self.project).values_list("sequence_number", flat=True)
        )
        newest = Sprint.objects.filter(project=self.project).order_by("-sequence_number").first()
        self.delete(newest)
        self.listing()
        fresh = Sprint.objects.filter(project=self.project, is_deleted=False).exclude(
            sequence_number__in=numbers
        )
        for sprint in fresh:
            self.assertGreater(sprint.sequence_number, newest.sequence_number)


class TheDeleteDetachesProperly(SprintDeleteBase):
    def setUp(self):
        super().setUp()
        self.sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S1",
            sequence_number=1,
            start_date="2026-10-01",
            end_date="2026-10-14",
        )
        self.milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            title="MS",
            reporter=self.user,
            sprint=self.sprint,
        )
        ensure_backing_task(self.milestone)
        self.milestone.refresh_from_db()
        self.kid = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="kid",
            status="Open",
            milestone=self.milestone,
            parent_task_id=self.milestone.task_id,
            sprint=self.sprint,
        )
        TaskActivity.objects.filter(task=self.kid).delete()

    def test_the_milestone_leaves_the_sprint(self):
        self.assertEqual(self.delete(self.sprint).status_code, 200)
        self.milestone.refresh_from_db()
        self.assertIsNone(self.milestone.sprint_id)

    def test_the_milestones_tasks_leave_it_too(self):
        self.delete(self.sprint)
        self.kid.refresh_from_db()
        self.assertIsNone(self.kid.sprint_id, "a task is in its milestone's sprint")

    def test_a_browser_holding_the_rows_hears_about_it(self):
        """A browser syncs rows changed since its checkpoint."""
        checkpoint = timezone.now() - timedelta(seconds=1)
        MilestoneMaster.objects.filter(pk=self.milestone.pk).update(
            ts_updated_at=checkpoint - timedelta(days=1)
        )
        TaskMaster.objects.filter(pk=self.kid.pk).update(
            ts_updated_at=checkpoint - timedelta(days=1)
        )

        self.delete(self.sprint)

        self.milestone.refresh_from_db()
        self.kid.refresh_from_db()
        self.assertGreater(self.milestone.ts_updated_at, checkpoint)
        self.assertGreater(self.kid.ts_updated_at, checkpoint)

    def test_the_task_history_records_the_move(self):
        self.delete(self.sprint)
        self.assertTrue(
            TaskActivity.objects.filter(task=self.kid, metadata__cascade="sprint_delete").exists()
        )

    def test_the_project_task_cache_is_dropped(self):
        with patch("origin.services.sprint_manage.invalidate_project_tasks_cache") as drop:
            self.delete(self.sprint)
        drop.assert_called_with(self.sprint.team_id, self.sprint.project_id)

    def test_deleting_twice_is_harmless(self):
        self.assertEqual(self.delete(self.sprint).status_code, 200)
        self.assertEqual(self.delete(self.sprint).status_code, 200)

    def test_a_non_member_still_cannot_delete_it(self):
        self.authenticate(self.user2)
        res = self.delete(self.sprint)
        self.assertEqual(res.status_code, 404, res.content)
        self.sprint.refresh_from_db()
        self.assertFalse(self.sprint.is_deleted)
