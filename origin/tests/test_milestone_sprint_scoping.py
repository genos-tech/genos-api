"""Milestones and sprints addressed by id, with no membership check.

Ten endpoints across `milestone_views` and `sprint_views` resolved their
target from a sequential integer — `project_id`, `milestone_id` or
`sprint_id` — and then acted on it. None asked whether the caller
belonged to the project.

The reads leak project planning: milestone titles, bodies, dates,
assignee and collaborator identities *with email* (the milestone
serializer enumerates them). The writes are worse — they mutate another
tenant's plan:

  * `MilestoneView.delete` soft-deletes the milestone AND its backing
    task, then detaches every child task from it.
  * `SprintView.delete` soft-deletes the sprint and detaches every
    milestone pointing at it.
  * `SprintConfigView.post` rewrites a project's sprint cadence and
    materializes new Sprint rows from it.
  * `MilestoneView.post` / `SprintView.post` plant new objects inside a
    project the caller has nothing to do with.

Refusal is 404, not 403: these ids are countable, so 403 would make each
endpoint an existence oracle for the whole install.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class MilestoneSprintScopingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Planned Project", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Q3 launch", reporter=self.user
        )
        self.sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="Sprint 1",
            sequence_number=1,
            start_date="2026-08-01",
            end_date="2026-08-14",
        )

        self.outsider = User.objects.create_user(
            username="planout", email="planout@example.com", password="pw"
        )
        self.outsider_team = TeamMaster.objects.create(
            team_name="Plan Outsider", team_email="planout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)

    # ── milestone reads ───────────────────────────────────────────────

    def test_outsider_cannot_read_a_milestone(self):
        self.authenticate(self.outsider)
        res = self.client.get(f"/api/v2/milestone/{self.milestone.milestone_id}/")
        self.assertEqual(res.status_code, 404)

    def test_member_can_read_a_milestone(self):
        self.authenticate(self.user)
        res = self.client.get(f"/api/v2/milestone/{self.milestone.milestone_id}/")
        self.assertEqual(res.status_code, 200)

    def test_outsider_cannot_list_project_milestones(self):
        self.authenticate(self.outsider)
        res = self.client.get("/api/v2/milestone/list/", {"project_id": self.project.project_id})
        self.assertEqual(res.status_code, 404)

    # ── milestone writes ──────────────────────────────────────────────

    def test_outsider_cannot_create_a_milestone_in_a_foreign_project(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            "/api/v2/milestone/",
            {"project_id": self.project.project_id, "title": "planted"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(MilestoneMaster.objects.filter(title="planted").exists())

    def test_outsider_cannot_patch_a_milestone(self):
        self.authenticate(self.outsider)
        res = self.client.patch(
            f"/api/v2/milestone/{self.milestone.milestone_id}/",
            {"title": "pwned"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.milestone.refresh_from_db()
        self.assertEqual(self.milestone.title, "Q3 launch")

    def test_outsider_cannot_delete_a_milestone(self):
        """Also soft-deletes the backing task and orphans every child."""
        self.authenticate(self.outsider)
        res = self.client.delete(f"/api/v2/milestone/{self.milestone.milestone_id}/")
        self.assertEqual(res.status_code, 404)
        self.milestone.refresh_from_db()
        self.assertFalse(self.milestone.is_deleted)

    def test_outsider_cannot_add_a_milestone_assignee(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            f"/api/v2/milestone/{self.milestone.milestone_id}/assignees/",
            {"user_id": str(self.outsider.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_outsider_cannot_remove_a_milestone_assignee(self):
        self.authenticate(self.outsider)
        res = self.client.delete(
            f"/api/v2/milestone/{self.milestone.milestone_id}/assignees/{self.user.id}/"
        )
        self.assertEqual(res.status_code, 404)

    # ── sprints ───────────────────────────────────────────────────────

    def test_outsider_cannot_list_sprints(self):
        self.authenticate(self.outsider)
        res = self.client.get("/api/v2/sprint/list/", {"project_id": self.project.project_id})
        self.assertEqual(res.status_code, 404)

    def test_member_can_list_sprints(self):
        self.authenticate(self.user)
        res = self.client.get("/api/v2/sprint/list/", {"project_id": self.project.project_id})
        self.assertEqual(res.status_code, 200)

    def test_outsider_cannot_read_sprint_config(self):
        self.authenticate(self.outsider)
        res = self.client.get("/api/v2/sprint/config/", {"project_id": self.project.project_id})
        self.assertEqual(res.status_code, 404)

    def test_outsider_cannot_rewrite_sprint_config(self):
        """Rewrites the cadence and materializes Sprint rows from it."""
        self.authenticate(self.outsider)
        res = self.client.post(
            "/api/v2/sprint/config/",
            {
                "project_id": self.project.project_id,
                "duration_days": 1,
                "anchor_date": "2026-01-01",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_outsider_cannot_create_a_sprint(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            "/api/v2/sprint/",
            {
                "project_id": self.project.project_id,
                "name": "planted",
                "start_date": "2026-09-01",
                "end_date": "2026-09-14",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(Sprint.objects.filter(name="planted").exists())

    def test_outsider_cannot_patch_a_sprint(self):
        self.authenticate(self.outsider)
        res = self.client.patch(
            f"/api/v2/sprint/{self.sprint.sprint_id}/", {"name": "pwned"}, format="json"
        )
        self.assertEqual(res.status_code, 404)
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.name, "Sprint 1")

    def test_outsider_cannot_delete_a_sprint(self):
        """Also detaches every milestone pointing at it."""
        self.authenticate(self.outsider)
        res = self.client.delete(f"/api/v2/sprint/{self.sprint.sprint_id}/")
        self.assertEqual(res.status_code, 404)
        self.sprint.refresh_from_db()
        self.assertFalse(self.sprint.is_deleted)

    def test_member_can_still_patch_a_sprint(self):
        self.authenticate(self.user)
        res = self.client.patch(
            f"/api/v2/sprint/{self.sprint.sprint_id}/", {"name": "Sprint One"}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.sprint.refresh_from_db()
        self.assertEqual(self.sprint.name, "Sprint One")
