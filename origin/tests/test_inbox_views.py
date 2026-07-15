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

    def test_create_inbox_item_persists_item_optionals(self):
        """Activity items must be able to carry routing ids.

        This endpoint built its `data` dict without `item_optionals`, so a
        caller could send them and they silently never persisted — which is
        why every activity row has null optionals and an activity card can't
        link to the project/GM it names. The joinXRequest/ endpoints always
        passed theirs through; only this one dropped them.
        """
        self.authenticate()
        response = self.client.post(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "sender_id": str(self.user.id),
                "receiver_id": str(self.user2.id),
                "item_body": {"text": "added you to a project"},
                "item_type": 0,
                "item_optionals": {"project_id": 7, "project_name": "Apollo"},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        item = InboxItems.objects.get(item_id=response.data["data"]["itemId"])
        self.assertEqual(item.item_optionals, {"project_id": 7, "project_name": "Apollo"})

    def test_create_inbox_item_echoes_item_optionals(self):
        """The POST response feeds the sockets `send()` body verbatim.

        A field missing from this response is a field the socket-delivered
        item lacks until the next full refetch — the GET has always returned
        `itemOptionals`, so the two would disagree.
        """
        self.authenticate()
        response = self.client.post(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "sender_id": str(self.user.id),
                "receiver_id": str(self.user2.id),
                "item_body": {"text": "added you to a group"},
                "item_type": 0,
                "item_optionals": {"gm_id": "gm-uuid", "gm_name": "Squad"},
            },
            format="json",
        )
        self.assertEqual(
            response.data["data"]["itemOptionals"], {"gm_id": "gm-uuid", "gm_name": "Squad"}
        )

    def test_create_inbox_item_without_optionals_still_works(self):
        """Every existing caller omits the key — it must stay optional."""
        self.authenticate()
        response = self.client.post(
            "/api/v2/inbox/",
            {
                "team_id": str(self.team.team_id),
                "sender_id": str(self.user.id),
                "receiver_id": str(self.user2.id),
                "item_body": {"text": "no optionals"},
                "item_type": 0,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(response.data["data"]["itemOptionals"])

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
        # Delta envelope: {server_time, data: {items: [...]}}.
        self.assertIn("server_time", response.data)
        self.assertIn("data", response.data)
        items = response.data["data"]["items"]
        self.assertIsInstance(items, list)
        self.assertEqual(len(items), 2)

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
        self.assertEqual(len(response.data["data"]["items"]), 0)

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
