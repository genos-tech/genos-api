"""Milestone handling on a project-changing task PUT.

A PUT that changes `project` is a "move", and the view can't blindly
trust a `milestone` in the same payload — two callers send one with
opposite intent:

  * The task preview's picker re-sends the task's EXISTING (now stale,
    source-project) milestone id alongside the new project. Honoring it
    would re-parent the moved task back into the source project. It
    must be cleared (the api #79/#81 behavior).

  * The create form finalizes a scaffold row that was POSTed under the
    page's current project — picking a different project in the form
    makes the finalize PUT a "move", and the milestone the user picked
    belongs to the DESTINATION. The old unconditional clear silently
    un-linked it: `parent_task_id` still saved (never popped), so the
    task nested under the milestone while `milestone_id` stayed NULL.

Ownership disambiguates: a milestone belonging to the destination
project is kept, anything else is cleared. These tests pin both sides.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster

User = get_user_model()


class TaskProjectMoveMilestoneTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="mover", email="mover@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        self.team = TeamMaster.objects.create(
            team_name="Move Team", team_email="move@test.com", owner=self.user
        )
        self.project_a = ProjectMaster.objects.create(
            team=self.team,
            project_name="Source",
            owner=self.user,
            project_system_user=self.user,
        )
        self.project_b = ProjectMaster.objects.create(
            team=self.team,
            project_name="Destination",
            owner=self.user,
            project_system_user=self.user,
        )
        for p in (self.project_a, self.project_b):
            ProjectMembers.objects.create(team=self.team, project=p, attendee=self.user)

        self.milestone_a = self._make_milestone(self.project_a, "MS A")
        self.milestone_b = self._make_milestone(self.project_b, "MS B")

    def _make_milestone(self, project, title):
        backing = TaskMaster.objects.create(
            team=self.team,
            project=project,
            title=title,
            status="Open",
            is_milestone=True,
        )
        return MilestoneMaster.objects.create(
            team=self.team,
            project=project,
            task=backing,
            title=title,
            reporter=self.user,
        )

    def _make_task(self, project, **kwargs):
        return TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="Open", **kwargs
        )

    def _move(self, task, dest_project, **extra):
        payload = {"task_id": task.task_id, "project": dest_project.project_id, **extra}
        res = self.client.put("/api/v2/task/", payload, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        task.refresh_from_db()
        return task

    # ── The create-form flow (the bug) ────────────────────────────────

    def test_destination_milestone_is_kept_on_project_change(self):
        """Scaffold under A, finalized with project B + B's milestone —
        the exact create-form flow. The linkage must fully land, not just
        the parent edge."""
        task = self._make_task(self.project_a)

        self._move(
            task,
            self.project_b,
            milestone=self.milestone_b.milestone_id,
            parent_task_id=self.milestone_b.task_id,
        )

        self.assertEqual(task.project_id, self.project_b.project_id)
        self.assertEqual(task.milestone_id, self.milestone_b.milestone_id)
        self.assertEqual(task.parent_task_id, self.milestone_b.task_id)
        self.assertEqual(task.root_task_id, self.milestone_b.task_id)

    def test_destination_milestone_without_parent_defaults_to_backing_task(self):
        task = self._make_task(self.project_a)

        self._move(task, self.project_b, milestone=self.milestone_b.milestone_id)

        self.assertEqual(task.milestone_id, self.milestone_b.milestone_id)
        self.assertEqual(task.parent_task_id, self.milestone_b.task_id)

    # ── The preview move flow (must keep clearing) ────────────────────

    def test_stale_source_milestone_is_cleared_on_move(self):
        """The task preview re-sends the task's existing milestone with the
        new project; keeping it would re-parent the task back into the
        source project. Pins the pre-existing move semantics."""
        task = self._make_task(
            self.project_a,
            milestone_id=self.milestone_a.milestone_id,
            parent_task_id=self.milestone_a.task_id,
            root_task_id=self.milestone_a.task_id,
        )

        self._move(
            task,
            self.project_b,
            milestone=self.milestone_a.milestone_id,
            parent_task_id=self.milestone_a.task_id,
        )

        self.assertEqual(task.project_id, self.project_b.project_id)
        self.assertIsNone(task.milestone_id)
        self.assertIsNone(task.sprint_id)
        # The only parent was the stale milestone's backing task — the
        # clear branch detaches it too.
        self.assertIsNone(task.parent_task_id)

    def test_move_without_milestone_key_still_clears_stale_link(self):
        """A partial PUT ({task_id, project}) — e.g. the task-graph
        diagram — must still shed the source project's milestone."""
        task = self._make_task(
            self.project_a,
            milestone_id=self.milestone_a.milestone_id,
            parent_task_id=self.milestone_a.task_id,
            root_task_id=self.milestone_a.task_id,
        )

        self._move(task, self.project_b)

        self.assertIsNone(task.milestone_id)

    def test_deleted_destination_milestone_is_cleared(self):
        """A soft-deleted milestone can't be linked even if it belongs to
        the destination — same is_deleted filter the bridge itself uses."""
        self.milestone_b.is_deleted = True
        self.milestone_b.save(update_fields=["is_deleted"])
        task = self._make_task(self.project_a)

        self._move(task, self.project_b, milestone=self.milestone_b.milestone_id)

        self.assertIsNone(task.milestone_id)
        self.assertIsNone(task.parent_task_id)

    def test_third_project_milestone_is_cleared(self):
        """A milestone from neither source nor destination is never
        honored (only destination-owned ids pass the ownership check)."""
        project_c = ProjectMaster.objects.create(
            team=self.team,
            project_name="Elsewhere",
            owner=self.user,
            project_system_user=self.user,
        )
        milestone_c = self._make_milestone(project_c, "MS C")
        task = self._make_task(self.project_a)

        self._move(task, self.project_b, milestone=milestone_c.milestone_id)

        self.assertIsNone(task.milestone_id)

    # ── No-move control ───────────────────────────────────────────────

    def test_same_project_put_still_links_milestone(self):
        """Without a project change the ownership gate must not run at
        all — the plain create/edit path keeps working."""
        task = self._make_task(self.project_a)

        self._move(task, self.project_a, milestone=self.milestone_a.milestone_id)

        self.assertEqual(task.milestone_id, self.milestone_a.milestone_id)
        self.assertEqual(task.parent_task_id, self.milestone_a.task_id)
