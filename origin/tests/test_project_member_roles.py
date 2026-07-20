"""Project permission roles: owner / editor / viewer.

Same shape as the Team suite. The invariant to protect is again that
ownership lives in `ProjectMaster.owner`, NOT in
`ProjectMembers.member_role` — so the owner's own row reads the `viewer`
default and every gate must go through `resolve_project_role`.

Project differs from Team in one deliberate way: adding members stays
open to ANY project member. Viewers keep that, because taking it away
would remove a capability every project member has today.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.member_roles import EDITOR, OWNER, VIEWER, resolve_project_role

User = get_user_model()


class TestProjectMemberRoles(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="pmowner", email="pmowner@test.com", password="pw"
        )
        self.editor = User.objects.create_user(
            username="pmeditor", email="pmeditor@test.com", password="pw"
        )
        self.viewer = User.objects.create_user(
            username="pmviewer", email="pmviewer@test.com", password="pw"
        )
        self.team = TeamMaster.objects.create(
            team_name="PM Role Team", team_email="pmrole@test.com", owner=self.owner
        )
        for u in (self.owner, self.editor, self.viewer):
            TeamMembers.objects.create(team=self.team, attendee=u)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Role Project",
            owner=self.owner,
            project_system_user=self.owner,
        )
        self.owner_row = ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.owner
        )
        self.editor_row = ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.editor, member_role=EDITOR
        )
        self.viewer_row = ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.viewer
        )
        self._auth(self.owner)

    def _auth(self, user):
        refresh = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")

    def _set_role(self, target, role):
        return self.client.put(
            "/api/v2/project/member-role/",
            {
                "project_id": self.project.project_id,
                "user_id": str(target.id),
                "member_role": role,
            },
            format="json",
        )

    def _rename(self, name="Renamed Project"):
        return self.client.put(
            "/api/v2/project/",
            {"project_id": self.project.project_id, "project_name": name},
            format="json",
        )

    def _set_code(self, code="ZZZ"):
        return self.client.put(
            "/api/v2/project/", {"project_id": self.project.project_id, "code": code}, format="json"
        )

    # ── The owner-is-not-in-the-column invariant ───────────────────

    def test_owner_row_reads_viewer_but_owner_can_manage(self):
        self.assertEqual(self.owner_row.member_role, VIEWER)
        self.assertEqual(resolve_project_role(self.project, self.owner.id), OWNER)
        self.assertEqual(self._rename().status_code, 200)

    def test_members_default_to_viewer(self):
        self.assertEqual(self.viewer_row.member_role, VIEWER)
        self.assertEqual(resolve_project_role(self.project, self.viewer.id), VIEWER)

    def test_transfer_moves_owner_without_column_writes(self):
        self.client.put(
            "/api/v2/project/",
            {"project_id": self.project.project_id, "owner_id": str(self.editor.id)},
            format="json",
        )
        self.project.refresh_from_db()
        self.assertEqual(resolve_project_role(self.project, self.editor.id), OWNER)

    # ── Role endpoint ──────────────────────────────────────────────

    def test_owner_can_promote_to_editor(self):
        self.assertEqual(self._set_role(self.viewer, EDITOR).status_code, 200)
        self.viewer_row.refresh_from_db()
        self.assertEqual(self.viewer_row.member_role, EDITOR)

    def test_editor_can_manage_roles(self):
        self._auth(self.editor)
        self.assertEqual(self._set_role(self.viewer, EDITOR).status_code, 200)

    def test_viewer_cannot_manage_roles(self):
        self._auth(self.viewer)
        self.assertEqual(self._set_role(self.editor, VIEWER).status_code, 403)

    def test_cannot_assign_owner_or_unknown_role(self):
        self.assertEqual(self._set_role(self.viewer, OWNER).status_code, 400)
        self.assertEqual(self._set_role(self.viewer, "root").status_code, 400)

    def test_cannot_change_the_owners_role(self):
        self.assertEqual(self._set_role(self.owner, VIEWER).status_code, 400)

    def test_non_member_404s(self):
        outsider = User.objects.create_user(
            username="pmoutsider", email="pmoutsider@test.com", password="pw"
        )
        self.assertEqual(self._set_role(outsider, EDITOR).status_code, 404)

    # ── Gates ──────────────────────────────────────────────────────

    def test_editor_can_rename(self):
        self._auth(self.editor)
        self.assertEqual(self._rename("Editor Renamed").status_code, 200)

    def test_viewer_cannot_rename(self):
        self._auth(self.viewer)
        self.assertEqual(self._rename("Viewer Renamed").status_code, 403)

    def test_editor_cannot_transfer_ownership(self):
        self._auth(self.editor)
        res = self.client.put(
            "/api/v2/project/",
            {"project_id": self.project.project_id, "owner_id": str(self.viewer.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_editor_can_set_code_but_viewer_cannot(self):
        """`code` was previously open to ANY authenticated caller."""
        self._auth(self.editor)
        self.assertEqual(self._set_code("EDT").status_code, 200)
        self._auth(self.viewer)
        self.assertEqual(self._set_code("VWR").status_code, 403)

    def test_editor_can_manage_project_labels(self):
        self._auth(self.editor)
        res = self.client.post(
            "/api/v2/project/label/",
            {"project_id": self.project.project_id, "name": "Editor Label"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    def test_viewer_cannot_manage_project_labels(self):
        self._auth(self.viewer)
        res = self.client.post(
            "/api/v2/project/label/",
            {"project_id": self.project.project_id, "name": "Viewer Label"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_editor_can_set_task_field_rules(self):
        self._auth(self.editor)
        res = self.client.put(
            "/api/v2/project/task-field-rules/",
            {"project_id": self.project.project_id, "rules": {}},
            format="json",
        )
        self.assertEqual(res.status_code, 200)

    def test_viewer_cannot_change_project_image(self):
        """This endpoint had NO authorization check before this change."""
        self._auth(self.viewer)
        res = self.client.put(
            "/api/v2/project/profile/image/",
            {"project_id": self.project.project_id, "profile_image": ""},
            format="multipart",
        )
        self.assertIn(res.status_code, (400, 403))

    def test_viewer_can_still_add_members(self):
        """Deliberately NOT tightened: adding members is open to any
        project member today, and viewers keep that."""
        newcomer = User.objects.create_user(
            username="pmnewcomer", email="pmnewcomer@test.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=newcomer)
        self._auth(self.viewer)
        res = self.client.post(
            "/api/v2/project/join/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "attendee_id": str(newcomer.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    # ── Serialization ──────────────────────────────────────────────

    def test_profile_carries_member_role_separate_from_job_title(self):
        self.editor.role = "Staff Engineer"
        self.editor.save(update_fields=["role"])
        res = self.client.get(
            f"/api/v2/project/profile/?team_id={self.team.team_id}"
            f"&project_id={self.project.project_id}"
        )
        self.assertEqual(res.status_code, 200)
        by_id = {str(m["userId"]): m for m in res.data["projectMembers"]}
        self.assertEqual(by_id[str(self.editor.id)]["memberRole"], EDITOR)
        self.assertEqual(by_id[str(self.editor.id)]["role"], "Staff Engineer")
        self.assertEqual(by_id[str(self.viewer.id)]["memberRole"], VIEWER)
