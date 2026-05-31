from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags

User = get_user_model()


class TestProjectViews(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass123"
        )
        self.user2 = User.objects.create_user(
            username="testuser2", email="test2@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.access_token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.access_token}")

        self.team = TeamMaster.objects.create(
            team_name="Test Team",
            team_email="team@test.com",
            owner=self.user,
        )

    # ── Project Create ─────────────────────────────────────────────

    def test_create_project(self):
        response = self.client.post(
            "/api/v2/project/",
            {
                "team": str(self.team.team_id),
                "project_name": "New Project",
                "owner": self.user.id,
                "project_system_user": self.user.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["project_name"], "New Project")

    def test_create_project_duplicate_name(self):
        ProjectMaster.objects.create(
            team=self.team,
            project_name="Existing Project",
            owner=self.user,
            project_system_user=self.user,
        )
        response = self.client.post(
            "/api/v2/project/",
            {
                "team": str(self.team.team_id),
                "project_name": "Existing Project",
                "owner": self.user.id,
                "project_system_user": self.user.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("hint", response.data)

    def test_create_project_missing_name(self):
        response = self.client.post(
            "/api/v2/project/",
            {
                "team": str(self.team.team_id),
                "owner": self.user.id,
                "project_system_user": self.user.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    # ── Project Existence Check ────────────────────────────────────

    def test_check_project_exists_true(self):
        ProjectMaster.objects.create(
            team=self.team,
            project_name="My Project",
            owner=self.user,
            project_system_user=self.user,
        )
        response = self.client.get(
            "/api/v2/project/exist/",
            {"team_id": str(self.team.team_id), "project_name": "My Project"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["project_exists"])

    def test_check_project_exists_false(self):
        response = self.client.get(
            "/api/v2/project/exist/",
            {"team_id": str(self.team.team_id), "project_name": "Nonexistent"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["project_exists"])

    def test_check_project_exists_missing_params(self):
        response = self.client.get("/api/v2/project/exist/")
        self.assertEqual(response.status_code, 400)

    # ── Project Join ───────────────────────────────────────────────

    def test_join_project(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Join Target",
            owner=self.user,
            project_system_user=self.user,
        )
        response = self.client.post(
            "/api/v2/project/join/",
            {
                "team_id": str(self.team.team_id),
                "project_id": project.project_id,
                "attendee_id": self.user.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            ProjectMembers.objects.filter(
                project=project, attendee=self.user
            ).exists()
        )

    def test_join_project_duplicate(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Dup Join",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(
            team=self.team, project=project, attendee=self.user
        )
        response = self.client.post(
            "/api/v2/project/join/",
            {
                "team_id": str(self.team.team_id),
                "project_id": project.project_id,
                "attendee_id": self.user.id,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    # ── Get Projects ───────────────────────────────────────────────

    def test_get_projects(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Listed Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(
            team=self.team, project=project, attendee=self.user
        )
        response = self.client.get(
            "/api/v2/project/projects/",
            {
                "team_id": str(self.team.team_id),
                "attendee_id": self.user.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertTrue(len(response.data) >= 1)
        entry = response.data[0]
        self.assertIn("projectId", entry)
        self.assertIn("isJoined", entry)

    def test_get_projects_missing_params(self):
        response = self.client.get("/api/v2/project/projects/")
        self.assertEqual(response.status_code, 400)

    # ── Project Tags ───────────────────────────────────────────────

    def test_create_project_tag(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Tag Project",
            owner=self.user,
            project_system_user=self.user,
        )
        response = self.client.post(
            "/api/v2/project/tag/",
            {
                "team_id": str(self.team.team_id),
                "project_id": project.project_id,
                "tag_name": "Bug",
                "tag_color": "#FF0000",
                "tag_text_color": "#FFFFFF",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["tag_name"], "Bug")

    def test_get_project_tags(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Tag List Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectTags.objects.create(
            team=self.team,
            project=project,
            tag_id=1,
            tag_name="Feature",
            tag_color="#00FF00",
            tag_text_color="#000000",
        )
        response = self.client.get(
            "/api/v2/project/tag/",
            {
                "team_id": str(self.team.team_id),
                "project_id": project.project_id,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["tagName"], "Feature")

    def test_get_project_tags_missing_params(self):
        response = self.client.get("/api/v2/project/tag/")
        self.assertEqual(response.status_code, 400)

    # ── Unauthorized ───────────────────────────────────────────────

    def test_unauthenticated_request(self):
        client = APIClient()
        response = client.get("/api/v2/project/projects/")
        self.assertEqual(response.status_code, 401)
