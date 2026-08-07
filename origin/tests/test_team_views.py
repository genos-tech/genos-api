"""Tests for team-related and user-profile API endpoints."""

import uuid

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
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

    def test_get_my_teams_without_user_id_uses_the_token(self):
        """`user_id` is no longer required: identity comes from the JWT.
        It used to be read from the query string and used verbatim."""
        self.authenticate()
        response = self.client.get("/api/v2/team/getMyTeams/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual({str(t["teamId"]) for t in response.data}, {str(self.team.team_id)})

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

    def test_get_team_members_without_user_id_is_fine(self):
        """`user_id` was required but never used as a gate; membership of
        `team_id` is what is checked now."""
        self.authenticate()
        response = self.client.get(
            "/api/v2/team/getTeamMembers/",
            {"team_id": str(self.team.team_id)},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_get_team_members_requires_team_id(self):
        self.authenticate()
        response = self.client.get("/api/v2/team/getTeamMembers/")
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


class TestProfileLocationAndAbout(BaseAPITestCase):
    """The profile fields behind the "where are you / who are you" card."""

    def _put(self, payload, user=None):
        user = user or self.user
        self.authenticate(user)
        return self.client.put(
            "/api/v2/user/profile/",
            {"user_id": str(user.id), **payload},
            format="json",
        )

    def test_location_accepts_an_iana_zone(self):
        response = self._put({"current_location": "Asia/Tokyo"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.current_location, "Asia/Tokyo")

    def test_location_rejects_something_that_is_not_a_zone(self):
        # Stored unchecked, this would raise from `ZoneInfo` later — on
        # whoever opened the profile, not on the person who typed it.
        response = self._put({"current_location": "Middle/Earth"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_location", response.data)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.current_location)

    def test_location_can_be_cleared(self):
        self.user.current_location = "Asia/Tokyo"
        self.user.save(update_fields=["current_location"])
        response = self._put({"current_location": ""})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.current_location, "")

    def test_setting_a_location_leaves_the_detected_timezone_alone(self):
        """The two zones answer different questions and must not merge.

        `timezone` is what the browser reported and gets rewritten on
        every boot; `current_location` is what the user chose. Writing
        one through the other would mean a manual choice survived only
        until the user's laptop next disagreed with it.
        """
        self.user.timezone = "Europe/Paris"
        self.user.save(update_fields=["timezone"])
        response = self._put({"current_location": "Asia/Tokyo"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.current_location, "Asia/Tokyo")
        self.assertEqual(self.user.timezone, "Europe/Paris")

    def test_timezone_is_not_writable_on_this_endpoint(self):
        self.user.timezone = "Europe/Paris"
        self.user.save(update_fields=["timezone"])
        response = self._put({"timezone": "Asia/Tokyo"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "Europe/Paris")

    def test_about_me_round_trips_and_trims(self):
        response = self._put({"about_me": "  Runs on **coffee**.\n  "})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.about_me, "Runs on **coffee**.")

    def test_about_me_over_the_cap_is_rejected(self):
        response = self._put({"about_me": "x" * 501})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("about_me", response.data)

    def test_about_me_at_the_cap_is_accepted(self):
        response = self._put({"about_me": "x" * 500})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_trailing_whitespace_does_not_count_against_the_cap(self):
        # The characters pushing this over are ones the user can't see
        # and couldn't find to delete.
        response = self._put({"about_me": "x" * 500 + "\n\n  "})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(len(self.user.about_me), 500)

    def test_phone_is_still_writable_by_its_owner_only(self):
        response = self._put({"phone_number": "+81 90-1234-5678"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, "+81 90-1234-5678")

        self.authenticate(self.user2)
        response = self.client.put(
            "/api/v2/user/profile/",
            {"user_id": str(self.user.id), "phone_number": "+1 555-0000"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.user.refresh_from_db()
        self.assertEqual(self.user.phone_number, "+81 90-1234-5678")


class TestProfileFieldsOnTheRoster(BaseAPITestCase):
    """What the team roster discloses, and to whom.

    `self.user` and `self.user2` are both full members of `self.team`
    (see `BaseAPITestCase`), so these cover the teammate case; the guest
    case builds its own outsider.
    """

    def setUp(self):
        super().setUp()
        self.user2.phone_number = "+81 90-1234-5678"
        self.user2.current_location = "Asia/Tokyo"
        self.user2.timezone = "Europe/Paris"
        self.user2.about_me = "Ships things."
        self.user2.save()

    def _member_row(self, response, user):
        # Roster reads go out through the delta envelope
        # (`{server_time, data: {...}}`), unlike the single-member view.
        rows = response.data["data"]["members"]
        match = [r for r in rows if str(r["userId"]) == str(user.id)]
        self.assertEqual(len(match), 1, f"expected exactly one row for {user.email}")
        return match[0]

    def test_a_teammate_sees_the_new_fields(self):
        self.authenticate(self.user)
        response = self.client.get(
            "/api/v2/team/getTeamMembers/",
            {"team_id": str(self.team.team_id), "team_name": self.team.team_name},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = self._member_row(response, self.user2)
        self.assertEqual(row["currentLocation"], "Asia/Tokyo")
        self.assertEqual(row["aboutMe"], "Ships things.")
        self.assertEqual(row["phoneNumber"], "+81 90-1234-5678")

    def test_both_zones_travel_so_the_client_can_choose(self):
        """The picked zone wins over the detected one, but the client is
        the one that decides — which it can only do if it has both."""
        self.authenticate(self.user)
        response = self.client.get(
            "/api/v2/team/getTeamMembers/",
            {"team_id": str(self.team.team_id), "team_name": self.team.team_name},
        )
        row = self._member_row(response, self.user2)
        self.assertEqual(row["currentLocation"], "Asia/Tokyo")
        self.assertEqual(row["timezone"], "Europe/Paris")

    def test_getTeamMemberInfo_carries_the_fields_too(self):
        self.authenticate(self.user)
        response = self.client.get(
            "/api/v2/team/getTeamMemberInfo/",
            {"team_id": str(self.team.team_id), "user_id": str(self.user2.id)},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["currentLocation"], "Asia/Tokyo")
        self.assertEqual(response.data["timezone"], "Europe/Paris")
        self.assertEqual(response.data["aboutMe"], "Ships things.")
        self.assertEqual(response.data["phoneNumber"], "+81 90-1234-5678")


class TestTeamProfileImage(BaseAPITestCase):
    """PUT /api/v2/team/profile/image/"""

    def _png(self, name="profile.jpg"):
        png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\rIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        return SimpleUploadedFile(name, png, content_type="image/png")

    def test_team_image_upload_stores_cache_busted_path(self):
        """Uploading a team avatar must store a per-upload cache-busted path.

        Regression guard: the FE reads the team avatar straight from
        `profile_image_file_name` and forces the fixed filename `profile.jpg`.
        On overwrite storage (S3/R2/GCS on Railway / GCP) that path would
        repeat across uploads, so without a `?v=` query string the browser
        serves the stale cached avatar. Mirrors User / Project image flows.
        """
        self.authenticate(self.user)
        response = self.client.put(
            "/api/v2/team/profile/image/",
            {"team_id": str(self.team.team_id), "team_profile_image": self._png()},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.team.refresh_from_db()
        self.assertTrue(self.team.profile_image_file_name)
        self.assertTrue(self.team.profile_image_file_name.startswith("team_profiles/"))
        # The per-upload cache-buster is what keeps overwrite storage from
        # serving a stale cached team avatar.
        self.assertIn("?v=", self.team.profile_image_file_name)
