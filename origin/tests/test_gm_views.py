"""Tests for GM (Group Message) chat endpoints."""
from rest_framework import status

from origin.models.chat.gm_models import GMMaster, GMMembers, GMMessages
from origin.models.chat.chat_master_models import UserChatMaster
from origin.tests.test_base import BaseAPITestCase


class GMMasterCreateViewTests(BaseAPITestCase):
    """POST /api/v2/gm/create/"""

    url = "/api/v2/gm/create/"

    def test_create_gm_success(self):
        self.authenticate()
        data = {
            "owner_team": str(self.team.team_id),
            "owner_user": str(self.user.id),
            "group_name": "Test Group",
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["chatName"], "Test Group")
        self.assertIn("chatId", resp.data)
        self.assertTrue(GMMaster.objects.filter(gm_id=resp.data["chatId"]).exists())

    def test_create_gm_returns_existing(self):
        self.authenticate()
        data = {
            "owner_team": str(self.team.team_id),
            "owner_user": str(self.user.id),
            "group_name": "Dup Group",
        }
        resp1 = self.client.post(self.url, data, format="json")
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)

        resp2 = self.client.post(self.url, data, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertTrue(resp2.data["gm_exists"])
        self.assertEqual(resp2.data["gm_id"], resp1.data["chatId"])

    def test_create_gm_missing_group_name(self):
        self.authenticate()
        resp = self.client.post(self.url, {"owner_team": str(self.team.team_id)}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_gm_unauthorized(self):
        resp = self.client.post(self.url, {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class GMMasterProfileViewTests(BaseAPITestCase):
    """GET /api/v2/gm/profile/"""

    url = "/api/v2/gm/profile/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.gm = GMMaster.objects.create(
            owner_team=self.team,
            owner_user=self.user,
            group_name="Profile Group",
        )
        GMMembers.objects.create(gm=self.gm, attendee=self.user)

    def test_get_profile_success(self):
        resp = self.client.get(self.url, {
            "team_id": str(self.team.team_id),
            "gm_id": str(self.gm.gm_id),
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["gmId"], self.gm.gm_id)
        self.assertEqual(resp.data["gmName"], "Profile Group")
        self.assertIn("gmMembers", resp.data)
        self.assertTrue(len(resp.data["gmMembers"]) >= 1)

    def test_get_profile_missing_params(self):
        resp = self.client.get(self.url, {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_profile_nonexistent_gm(self):
        resp = self.client.get(self.url, {
            "team_id": str(self.team.team_id),
            "gm_id": "999999",
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_profile_unauthorized(self):
        self.unauthenticate()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class GMMembersJoinViewTests(BaseAPITestCase):
    """POST /api/v2/gm/join/"""

    url = "/api/v2/gm/join/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.gm = GMMaster.objects.create(
            owner_team=self.team,
            owner_user=self.user,
            group_name="Join Group",
        )

    def test_join_gm_success(self):
        data = {
            "gm_id": self.gm.gm_id,
            "attendee_id": str(self.user.id),
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            GMMembers.objects.filter(gm=self.gm, attendee=self.user).exists()
        )

    def test_join_gm_already_joined(self):
        GMMembers.objects.create(gm=self.gm, attendee=self.user)
        data = {
            "gm_id": self.gm.gm_id,
            "attendee_id": str(self.user.id),
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_join_gm_second_user(self):
        data = {
            "gm_id": self.gm.gm_id,
            "attendee_id": str(self.user2.id),
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            GMMembers.objects.filter(gm=self.gm, attendee=self.user2).exists()
        )

    def test_join_gm_unauthorized(self):
        self.unauthenticate()
        resp = self.client.post(self.url, {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class GMHistoryViewTests(BaseAPITestCase):
    """GET /api/v2/gm/history/"""

    url = "/api/v2/gm/history/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.gm = GMMaster.objects.create(
            owner_team=self.team,
            owner_user=self.user,
            group_name="History Group",
        )
        GMMembers.objects.create(gm=self.gm, attendee=self.user)
        UserChatMaster.objects.create(
            team=self.team,
            user=self.user,
            pinned_chats=[],
            flagged_messages=[],
        )

    def test_history_empty(self):
        resp = self.client.get(self.url, {
            "team_id": str(self.team.team_id),
            "team_name": self.team.team_name,
            "user_id": str(self.user.id),
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("chat_history", resp.data)
        self.assertEqual(resp.data["chat_history"], [])

    def test_history_with_messages(self):
        GMMessages.objects.create(
            gm=self.gm,
            sender=self.user,
            message_id=1,
            message_body=[{"type": "text", "text": "Hello group"}],
        )
        resp = self.client.get(self.url, {
            "team_id": str(self.team.team_id),
            "team_name": self.team.team_name,
            "user_id": str(self.user.id),
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(len(resp.data["chat_history"]) > 0)

    def test_history_no_membership_returns_empty(self):
        """A user not in any GM should get empty history."""
        UserChatMaster.objects.create(
            team=self.team,
            user=self.user2,
            pinned_chats=[],
            flagged_messages=[],
        )
        self.authenticate(self.user2)
        resp = self.client.get(self.url, {
            "team_id": str(self.team.team_id),
            "team_name": self.team.team_name,
            "user_id": str(self.user2.id),
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["chat_history"], [])

    def test_history_wrong_user_forbidden(self):
        resp = self.client.get(self.url, {
            "team_id": str(self.team.team_id),
            "team_name": self.team.team_name,
            "user_id": str(self.user2.id),
        })
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_history_missing_params(self):
        resp = self.client.get(self.url, {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_history_unauthorized(self):
        self.unauthenticate()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class GMSingleMessageViewTests(BaseAPITestCase):
    """POST /api/v2/gm/message/"""

    url = "/api/v2/gm/message/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.gm = GMMaster.objects.create(
            owner_team=self.team,
            owner_user=self.user,
            group_name="Msg Group",
        )
        GMMembers.objects.create(gm=self.gm, attendee=self.user)

    def test_send_message_success(self):
        data = {
            "gm_id": self.gm.gm_id,
            "sender_id": str(self.user.id),
            "message_body": [{"type": "text", "text": "Hi group!"}],
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["message_id"], 1)
        self.assertTrue(GMMessages.objects.filter(gm=self.gm, message_id=1).exists())

    def test_send_multiple_messages_increments_id(self):
        base = {
            "gm_id": self.gm.gm_id,
            "sender_id": str(self.user.id),
            "message_body": [{"type": "text", "text": "msg"}],
        }
        resp1 = self.client.post(self.url, base, format="json")
        resp2 = self.client.post(self.url, base, format="json")
        self.assertEqual(resp1.data["message_id"], 1)
        self.assertEqual(resp2.data["message_id"], 2)

    def test_send_init_message_skips_if_exists(self):
        GMMessages.objects.create(
            gm=self.gm,
            sender=self.user,
            message_id=1,
            message_body=[{"type": "text", "text": "init"}],
        )
        data = {
            "gm_id": self.gm.gm_id,
            "sender_id": str(self.user.id),
            "message_body": [{"type": "text", "text": "init again"}],
            "is_init": True,
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn("message", resp.data)

    def test_send_message_unauthorized(self):
        self.unauthenticate()
        resp = self.client.post(self.url, {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
