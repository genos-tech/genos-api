from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.unified_models import Channel, ChannelKind, Message
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
            ProjectMembers.objects.filter(project=project, attendee=self.user).exists()
        )

    def test_join_project_duplicate(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Dup Join",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
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
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
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

    # ── Project Profile Image → PM Channel mirror ──────────────────

    def _png(self, name="profile.jpg"):
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return SimpleUploadedFile(name, png, content_type="image/png")

    def test_project_image_upload_mirrors_to_pm_channel(self):
        """Uploading a project avatar must propagate to the PM channel.

        Regression guard: the v3 chat UI reads a PM chat's avatar ONLY
        from `Channel.profile_image_url`, but `ProjectProfileImageView`
        writes `ProjectMaster`. The `_ensure_pm_channel_for_project`
        signal bridges the two. Drive the REAL endpoint (not a synthetic
        save) so the view's two-step save ordering — file save, then the
        `profile_image_file_name` recompute that the signal mirrors from —
        is actually exercised.
        """
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Avatar Project",
            owner=self.user,
            project_system_user=self.user,
        )
        # The signal auto-creates the PM channel with no avatar yet.
        channel = Channel.objects.get(project_id=project.project_id, kind=ChannelKind.PM)
        self.assertEqual(channel.profile_image_url, "")

        response = self.client.put(
            "/api/v2/project/profile/image/",
            {"project_id": str(project.project_id), "profile_image": self._png()},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200)

        project.refresh_from_db()
        channel.refresh_from_db()
        # Channel avatar now mirrors the project's stored media path.
        self.assertTrue(project.profile_image_file_name)
        self.assertEqual(channel.profile_image_url, project.profile_image_file_name)
        self.assertTrue(channel.profile_image_url.startswith("project_profiles/"))
        # Carries the per-upload cache-buster so a future overwrite-storage
        # switch can't serve a stale cached avatar (mirrors the user flow).
        self.assertIn("?v=", channel.profile_image_url)


class TestProjectDelete(TestCase):
    """DELETE /api/v2/project/

    Regression: every project gets a PM channel from the
    `_ensure_pm_channel_for_project` post_save signal, and `Channel.project`
    is PROTECT. So `target_project.delete()` raised ProtectedError for every
    project that had ever been created through the app — an uncaught 500,
    since only `ProjectMaster.DoesNotExist` was handled.

    Hard-deleting the channel is not the fix: `Message.channel` is PROTECT
    too, so a project with any chat history would just move the 500 down a
    level. The channel is soft-deleted and its FK released instead.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="owner", email="owner@test.com", password="pw"
        )
        self.other = User.objects.create_user(
            username="other", email="other@test.com", password="pw"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        self.team = TeamMaster.objects.create(
            team_name="Delete Team", team_email="del@test.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Doomed Project",
            owner=self.user,
            project_system_user=self.user,
        )

    def _delete(self):
        return self.client.delete(
            f"/api/v2/project/?team_id={self.team.team_id}&project_id={self.project.project_id}"
        )

    def test_delete_project_that_has_a_pm_channel(self):
        # The signal must actually have produced one, else this test would
        # pass against the broken code.
        self.assertEqual(Channel.objects.filter(project=self.project).count(), 1)

        response = self._delete()

        self.assertEqual(response.status_code, 204)
        self.assertFalse(ProjectMaster.objects.filter(project_id=self.project.project_id).exists())

    def test_pm_channel_is_soft_deleted_and_detached(self):
        channel = Channel.objects.get(project=self.project)

        self._delete()

        channel.refresh_from_db()
        self.assertTrue(channel.is_deleted)
        self.assertIsNone(channel.project_id)

    def test_delete_preserves_chat_history(self):
        """`Message.channel` is PROTECT on purpose — messages must survive."""
        channel = Channel.objects.get(project=self.project)
        message = Message.objects.create(
            channel=channel, sender=self.user, seq=1, body={"text": "hello"}, body_text="hello"
        )

        response = self._delete()

        self.assertEqual(response.status_code, 204)
        self.assertTrue(Message.objects.filter(id=message.id).exists())

    def test_non_owner_cannot_delete(self):
        refresh = RefreshToken.for_user(self.other)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")

        self._delete()

        self.assertTrue(ProjectMaster.objects.filter(project_id=self.project.project_id).exists())

    def test_ownerless_project_is_not_a_500(self):
        """`ProjectMaster.owner` is SET_NULL, so it can be None."""
        self.project.owner = None
        self.project.save()

        response = self._delete()

        self.assertLess(response.status_code, 500)
