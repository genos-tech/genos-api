"""Tests for DM (Direct Message) chat endpoints."""

from django.test import TestCase
from rest_framework import status

from origin.models.chat.dm_models import DMMaster, DMMessages, UserDMMapping
from origin.models.chat.chat_master_models import UserChatMaster
from origin.tests.test_base import BaseAPITestCase


class DMMasterViewTests(BaseAPITestCase):
    """POST /api/v2/dm/create/"""

    url = "/api/v2/dm/create/"

    def test_create_dm_success(self):
        self.authenticate()
        data = {
            "team": str(self.team.team_id),
            "user_1_id": str(self.user.id),
            "user_2_id": str(self.user2.id),
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn("dm_id", resp.data)
        self.assertTrue(DMMaster.objects.filter(dm_id=resp.data["dm_id"]).exists())

    def test_create_dm_returns_existing(self):
        """Creating the same DM again should return the existing dm_id."""
        self.authenticate()
        data = {
            "team": str(self.team.team_id),
            "user_1_id": str(self.user.id),
            "user_2_id": str(self.user2.id),
        }
        resp1 = self.client.post(self.url, data, format="json")
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)

        resp2 = self.client.post(self.url, data, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertTrue(resp2.data["dm_exists"])
        self.assertEqual(resp2.data["dm_id"], resp1.data["dm_id"])

    def test_create_dm_reverse_order_returns_existing(self):
        """Swapping user_1_id / user_2_id should still detect the existing DM."""
        self.authenticate()
        data = {
            "team": str(self.team.team_id),
            "user_1_id": str(self.user.id),
            "user_2_id": str(self.user2.id),
        }
        resp1 = self.client.post(self.url, data, format="json")
        self.assertEqual(resp1.status_code, status.HTTP_201_CREATED)

        data_reversed = {
            "team": str(self.team.team_id),
            "user_1_id": str(self.user2.id),
            "user_2_id": str(self.user.id),
        }
        resp2 = self.client.post(self.url, data_reversed, format="json")
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertTrue(resp2.data["dm_exists"])

    def test_create_dm_missing_params(self):
        self.authenticate()
        resp = self.client.post(self.url, {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("error", resp.data)

    def test_create_dm_missing_user_2(self):
        self.authenticate()
        data = {
            "team": str(self.team.team_id),
            "user_1_id": str(self.user.id),
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_dm_unauthorized(self):
        resp = self.client.post(self.url, {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_create_dm_creates_user_dm_mappings(self):
        self.authenticate()
        data = {
            "team": str(self.team.team_id),
            "user_1_id": str(self.user.id),
            "user_2_id": str(self.user2.id),
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        dm_id = resp.data["dm_id"]
        self.assertTrue(UserDMMapping.objects.filter(dm_id=dm_id, user_id=self.user.id).exists())
        self.assertTrue(UserDMMapping.objects.filter(dm_id=dm_id, user_id=self.user2.id).exists())


class CheckDMExistsViewTests(BaseAPITestCase):
    """GET /api/v2/dm/checkExistence/"""

    url = "/api/v2/dm/checkExistence/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.dm = DMMaster.objects.create(
            team=self.team,
            user_1_id=self.user.id,
            user_2_id=self.user2.id,
        )

    def test_dm_exists(self):
        resp = self.client.get(
            self.url,
            {
                "team_id": str(self.team.team_id),
                "user_1_id": str(self.user.id),
                "user_2_id": str(self.user2.id),
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["dm_exists"])
        self.assertEqual(resp.data["dm_id"], self.dm.dm_id)

    def test_dm_exists_reversed(self):
        resp = self.client.get(
            self.url,
            {
                "team_id": str(self.team.team_id),
                "user_1_id": str(self.user2.id),
                "user_2_id": str(self.user.id),
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["dm_exists"])

    def test_dm_not_exists(self):
        from django.contrib.auth import get_user_model

        user3 = get_user_model().objects.create_user(
            username="user3", email="user3@example.com", password="pass123"
        )
        resp = self.client.get(
            self.url,
            {
                "team_id": str(self.team.team_id),
                "user_1_id": str(self.user.id),
                "user_2_id": str(user3.id),
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["dm_exists"])
        self.assertIsNone(resp.data["dm_id"])

    def test_check_dm_missing_params(self):
        resp = self.client.get(self.url, {"team_id": str(self.team.team_id)})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_check_dm_unauthorized(self):
        self.unauthenticate()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class DMIdViewTests(BaseAPITestCase):
    """GET /api/v2/dm/id/"""

    url = "/api/v2/dm/id/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.dm = DMMaster.objects.create(
            team=self.team,
            user_1_id=self.user.id,
            user_2_id=self.user2.id,
        )

    def test_get_dm_id(self):
        resp = self.client.get(
            self.url,
            {
                "team_id": str(self.team.team_id),
                "user_1_id": str(self.user.id),
                "user_2_id": str(self.user2.id),
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["dm_id"], self.dm.dm_id)

    def test_get_dm_id_reversed_users(self):
        resp = self.client.get(
            self.url,
            {
                "team_id": str(self.team.team_id),
                "user_1_id": str(self.user2.id),
                "user_2_id": str(self.user.id),
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["dm_id"], self.dm.dm_id)

    def test_get_dm_id_not_found(self):
        from django.contrib.auth import get_user_model

        user3 = get_user_model().objects.create_user(
            username="user3", email="u3@example.com", password="p"
        )
        resp = self.client.get(
            self.url,
            {
                "team_id": str(self.team.team_id),
                "user_1_id": str(self.user.id),
                "user_2_id": str(user3.id),
            },
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIsNone(resp.data["dm_id"])

    def test_get_dm_id_missing_params(self):
        resp = self.client.get(self.url, {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_dm_id_unauthorized(self):
        self.unauthenticate()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class DMSingleMessageViewTests(BaseAPITestCase):
    """POST /api/v2/dm/message/"""

    url = "/api/v2/dm/message/"

    def setUp(self):
        super().setUp()
        self.authenticate()
        self.dm = DMMaster.objects.create(
            team=self.team,
            user_1_id=self.user.id,
            user_2_id=self.user2.id,
        )

    def test_send_message_success(self):
        data = {
            "dm_id": self.dm.dm_id,
            "sender_id": str(self.user.id),
            "receiver_id": str(self.user2.id),
            "message_body": [{"type": "text", "text": "Hello!"}],
        }
        resp = self.client.post(self.url, data, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["message_id"], 1)
        self.assertTrue(DMMessages.objects.filter(dm=self.dm, message_id=1).exists())

    def test_send_multiple_messages_increments_id(self):
        base = {
            "dm_id": self.dm.dm_id,
            "sender_id": str(self.user.id),
            "receiver_id": str(self.user2.id),
            "message_body": [{"type": "text", "text": "msg"}],
        }
        resp1 = self.client.post(self.url, base, format="json")
        resp2 = self.client.post(self.url, base, format="json")
        self.assertEqual(resp1.data["message_id"], 1)
        self.assertEqual(resp2.data["message_id"], 2)

    def test_send_init_message_skips_if_already_exists(self):
        DMMessages.objects.create(
            dm=self.dm,
            sender=self.user,
            receiver=self.user2,
            message_id=1,
            message_body=[{"type": "text", "text": "init"}],
        )
        data = {
            "dm_id": self.dm.dm_id,
            "sender_id": str(self.user.id),
            "receiver_id": str(self.user2.id),
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
