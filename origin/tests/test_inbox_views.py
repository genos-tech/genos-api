"""Tests for inbox API endpoints."""
from django.contrib.auth import get_user_model
from rest_framework import status

from origin.models.common.inbox_models import InboxItems
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class TestInboxCreate(BaseAPITestCase):
    """POST /api/v2/inbox/"""

    def test_create_inbox_item_success(self):
        self.authenticate()
        response = self.client.post(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "sender_id": str(self.user.id),
                "receiver_id": str(self.user2.id),
                "item_body": {"text": "You have a new message"},
                "item_type": 0,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("data", response.data)
        self.assertEqual(response.data["data"]["itemType"], 0)
        self.assertFalse(response.data["alreadyExist"])

    def test_create_inbox_item_duplicate_is_idempotent(self):
        """Sending the exact same inbox item again should return 201 with alreadyExist=True."""
        self.authenticate()
        payload = {
            "team_id": str(self.team.team_id),
            "sender_id": str(self.user.id),
            "receiver_id": str(self.user2.id),
            "item_body": {"text": "duplicate test"},
            "item_type": 0,
        }
        self.client.post("/api/v2/inbox/", payload, format="json")
        response = self.client.post("/api/v2/inbox/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["alreadyExist"])

    def test_create_inbox_item_unauthenticated(self):
        response = self.client.post(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "sender_id": str(self.user.id),
                "receiver_id": str(self.user2.id),
                "item_body": {"text": "unauth"},
                "item_type": 0,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TestInboxList(BaseAPITestCase):
    """GET /api/v2/inbox/"""

    def setUp(self):
        super().setUp()
        InboxItems.objects.create(
            team=self.team,
            sender=self.user,
            receiver=self.user2,
            item_body={"text": "item-1"},
            item_type=0,
        )
        InboxItems.objects.create(
            team=self.team,
            sender=self.user,
            receiver=self.user2,
            item_body={"text": "item-2"},
            item_type=0,
        )

    def test_list_inbox_items_success(self):
        self.authenticate(self.user2)
        response = self.client.get(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user2.id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)
        self.assertEqual(len(response.data), 2)

    def test_list_inbox_returns_empty_for_other_user(self):
        self.authenticate(self.user)
        response = self.client.get(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_list_inbox_missing_params(self):
        self.authenticate()
        response = self.client.get(
            "/api/v2/inbox/",
            {"team_id": str(self.team.team_id)},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_inbox_unauthenticated(self):
        response = self.client.get(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user2.id),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class TestInboxMarkRead(BaseAPITestCase):
    """PUT /api/v2/inbox/"""

    def setUp(self):
        super().setUp()
        self.inbox_item = InboxItems.objects.create(
            team=self.team,
            sender=self.user,
            receiver=self.user2,
            item_body={"text": "read me"},
            item_type=0,
            is_read=False,
        )

    def test_mark_inbox_read_success(self):
        self.authenticate(self.user2)
        response = self.client.put(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "item_id": self.inbox_item.item_id,
                "is_read": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inbox_item.refresh_from_db()
        self.assertTrue(self.inbox_item.is_read)

    def test_mark_inbox_read_missing_params(self):
        self.authenticate()
        response = self.client.put(
            "/api/v2/inbox/",
            {"team_id": str(self.team.team_id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mark_inbox_read_unauthenticated(self):
        response = self.client.put(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "item_id": self.inbox_item.item_id,
                "is_read": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
