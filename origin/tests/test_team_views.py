"""Tests for team-related and user-profile API endpoints."""

import uuid

from django.contrib.auth import get_user_model
from rest_framework import status

from origin.models.common.team_models import TeamMembers
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class TestTeamCreation(BaseAPITestCase):
    """POST /api/v2/team/create/"""

    def test_create_team_success(self):
        """The view omits profile_image_file from the serializer data, so
        we mark it optional on the serializer to let creation succeed."""
        self.authenticate()
        response = self.client.post(
            "/api/v2/team/create/",
            {
                "team_name": "New Team",
                "team_email": "newteam@test.com",
                "owner_id": str(self.user.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("teamDetails", response.data)
        self.assertEqual(response.data["teamDetails"]["teamName"], "New Team")

    def test_create_team_duplicate_name(self):
        self.authenticate()
        response = self.client.post(
            "/api/v2/team/create/",
            {
                "team_name": "Test Team",
                "team_email": "another@example.com",
                "owner_id": str(self.user.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_team_unauthenticated(self):
        response = self.client.post(
            "/api/v2/team/create/",
            {
                "team_name": "Anon Team",
                "team_email": "anon@example.com",
                "owner_id": str(self.user.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TestCheckTeamExists(BaseAPITestCase):
    """GET /api/v2/team/exist/"""

    def test_team_exists(self):
        self.authenticate()
        response = self.client.get(
            "/api/v2/team/exist/",
            {"team_id": str(self.team.team_id)},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["exist"])
        self.assertEqual(response.data["teamDetails"]["teamName"], "Test Team")

    def test_team_does_not_exist(self):
        self.authenticate()
        fake_id = str(uuid.uuid4())
        response = self.client.get(
            "/api/v2/team/exist/",
            {"team_id": fake_id},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["exist"])

    def test_missing_team_id_param(self):
        self.authenticate()
        response = self.client.get("/api/v2/team/exist/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated(self):
        response = self.client.get(
            "/api/v2/team/exist/",
            {"team_id": str(self.team.team_id)},
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TestTeamJoin(BaseAPITestCase):
    """POST /api/v2/team/join/"""

    def test_join_team_new_member(self):
        self.authenticate()
        new_user = User.objects.create_user(
            username="newguy",
            email="newguy@example.com",
            password="pass1234",
        )
        response = self.client.post(
            "/api/v2/team/join/",
            {
                "team_id": str(self.team.team_id),
                "attendee_id": str(new_user.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(TeamMembers.objects.filter(team=self.team, attendee=new_user).exists())

    def test_join_team_already_member(self):
        """Re-joining should still return 201 (idempotent)."""
        self.authenticate()
        response = self.client.post(
            "/api/v2/team/join/",
            {
                "team_id": str(self.team.team_id),
                "attendee_id": str(self.user.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_join_team_unauthenticated(self):
        response = self.client.post(
            "/api/v2/team/join/",
            {
                "team_id": str(self.team.team_id),
                "attendee_id": str(self.user2.id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TestGetMyTeams(BaseAPITestCase):
    """GET /api/v2/team/getMyTeams/"""

    def test_get_my_teams_success(self):
        self.authenticate()
        response = self.client.get(
            "/api/v2/team/getMyTeams/",
            {"user_id": str(self.user.id)},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)
        self.assertGreaterEqual(len(response.data), 1)
        team_data = response.data[0]
        self.assertEqual(team_data["teamName"], "Test Team")
        self.assertIn("teamMembers", team_data)

    def test_get_my_teams_missing_user_id(self):
        self.authenticate()
        response = self.client.get("/api/v2/team/getMyTeams/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_my_teams_unauthenticated(self):
        response = self.client.get(
            "/api/v2/team/getMyTeams/",
            {"user_id": str(self.user.id)},
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TestGetTeamMembers(BaseAPITestCase):
    """GET /api/v2/team/getTeamMembers/"""

    def test_get_team_members_success(self):
        self.authenticate()
        response = self.client.get(
            "/api/v2/team/getTeamMembers/",
            {
                "team_id": str(self.team.team_id),
                "team_name": "Test Team",
                "user_id": str(self.user.id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Delta envelope: {server_time, data: {members: [...]}}.
        self.assertIn("server_time", response.data)
        members = response.data["data"]["members"]
        self.assertIsInstance(members, list)
        self.assertEqual(len(members), 2)
        emails = {m["userEmail"] for m in members}
        self.assertIn("test@example.com", emails)
        self.assertIn("other@example.com", emails)

    def test_get_team_members_missing_params(self):
        self.authenticate()
        response = self.client.get(
            "/api/v2/team/getTeamMembers/",
            {"team_id": str(self.team.team_id)},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_team_members_unauthenticated(self):
        response = self.client.get(
            "/api/v2/team/getTeamMembers/",
            {
                "team_id": str(self.team.team_id),
                "team_name": "Test Team",
                "user_id": str(self.user.id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TestGetTeamMemberInfo(BaseAPITestCase):
    """GET /api/v2/team/getTeamMemberInfo/"""

    def test_get_member_info_success(self):
        self.authenticate()
        response = self.client.get(
            "/api/v2/team/getTeamMemberInfo/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["userName"], "testuser")
        self.assertEqual(response.data["userEmail"], "test@example.com")

    def test_custom_status_returned(self):
        """Verify that the customStatus field is correctly returned."""
        self.user.custom_status = "In a meeting"
        self.user.save(update_fields=["custom_status"])

        self.authenticate()
        response = self.client.get(
            "/api/v2/team/getTeamMemberInfo/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["customStatus"], "In a meeting")

    def test_custom_status_none_when_not_set(self):
        """customStatus should be None when the user has no status set."""
        self.authenticate()
        response = self.client.get(
            "/api/v2/team/getTeamMemberInfo/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["customStatus"])

    def test_member_not_found(self):
        self.authenticate()
        fake_user_id = str(uuid.uuid4())
        response = self.client.get(
            "/api/v2/team/getTeamMemberInfo/",
            {
                "team_id": str(self.team.team_id),
                "user_id": fake_user_id,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_params(self):
        self.authenticate()
        response = self.client.get(
            "/api/v2/team/getTeamMemberInfo/",
            {"team_id": str(self.team.team_id)},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unauthenticated(self):
        response = self.client.get(
            "/api/v2/team/getTeamMemberInfo/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TestUserProfileUpdate(BaseAPITestCase):
    """PUT /api/v2/user/profile/"""

    def test_profile_update_own_user(self):
        self.authenticate(self.user)
        response = self.client.put(
            "/api/v2/user/profile/",
            {"user_id": str(self.user.id), "username": "updatedname"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "updatedname")

    def test_profile_update_wrong_user_returns_403(self):
        self.authenticate(self.user2)
        response = self.client.put(
            "/api/v2/user/profile/",
            {"user_id": str(self.user.id), "username": "hacked"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.username, "hacked")

    def test_profile_update_unauthenticated(self):
        response = self.client.put(
            "/api/v2/user/profile/",
            {"user_id": str(self.user.id), "username": "anon"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_profile_update_missing_user_id(self):
        self.authenticate(self.user)
        response = self.client.put(
            "/api/v2/user/profile/",
            {"username": "noid"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
