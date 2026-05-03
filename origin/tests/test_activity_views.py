"""Tests for activity and read-status API endpoints."""

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework import status
from django.contrib.auth import get_user_model

from origin.tests.test_base import BaseAPITestCase
from origin.models.chat.activity_models import ActivityFact
from origin.models.chat.read_status_models import ReadStatus, ActivityReadStatus
from origin.models.project.prj_models import ProjectMaster

User = get_user_model()


class ActivityViewPutTests(BaseAPITestCase):
    """PUT /api/v2/chat/activity/ — upsert activity."""

    URL = "/api/v2/chat/activity/"

    def _make_activity_data(self, **overrides):
        defaults = {
            "team": str(self.team.team_id),
            "activity_id": "1-1-100-500",
            "activity_type": 1,
            "chat_type": 1,
            "chat_id": 100,
            "chat_name": "test-chat",
            "is_thread": False,
            "thread_id": 0,
            "message_id": 500,
            "message_unique_key": "msg-unique-1",
            "first_line_content": "Hello world",
            "sender": str(self.user.id),
            "latest_reaction": {},
            "latest_reaction_user": None,
            "reactions": [],
            "mentioned_user_ids": [],
            "dm_partner_user": None,
            "project": None,
            "task": None,
        }
        defaults.update(overrides)
        return defaults

    def test_create_new_activity(self):
        self.authenticate()
        data = self._make_activity_data()
        response = self.client.put(self.URL, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ActivityFact.objects.filter(activity_id="1-1-100-500").exists())

    def test_update_existing_activity(self):
        self.authenticate()
        data = self._make_activity_data()
        self.client.put(self.URL, data, format="json")

        updated = self._make_activity_data(first_line_content="Updated body")
        response = self.client.put(self.URL, updated, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["first_line_content"], "Updated body")

    def test_activity_type_3_prefix_rewritten_to_1(self):
        """When activity_type=3 (mention), the leading '3' in activity_id is rewritten to '1'."""
        self.authenticate()
        data = self._make_activity_data(activity_id="3-1-100-500", activity_type=1)
        response = self.client.put(self.URL, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ActivityFact.objects.filter(activity_id="1-1-100-500").exists())
        self.assertFalse(ActivityFact.objects.filter(activity_id="3-1-100-500").exists())


class ActivityViewDeleteTests(BaseAPITestCase):
    """DELETE /api/v2/chat/activity/ — delete activity."""

    URL = "/api/v2/chat/activity/"

    def _create_activity(self):
        self.authenticate()
        return ActivityFact.objects.create(
            team=self.team,
            activity_id="1-1-100-500",
            activity_type=1,
            chat_type=1,
            chat_id=100,
            is_thread=False,
            thread_id=0,
            message_id=500,
            message_unique_key="msg-unique-1",
            first_line_content="Hello",
            sender=self.user,
            latest_reaction={},
            reactions=[],
            mentioned_user_ids=[],
        )

    def test_delete_existing_activity(self):
        self._create_activity()
        response = self.client.delete(
            self.URL,
            {"team_id": str(self.team.team_id), "activity_id": "1-1-100-500"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ActivityFact.objects.filter(activity_id="1-1-100-500").exists())

    def test_delete_not_found(self):
        self.authenticate()
        response = self.client.delete(
            self.URL,
            {"team_id": str(self.team.team_id), "activity_id": "1-1-999-999"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_with_mention_prefix_rewrite(self):
        """Deleting with activity_id starting with '3' rewrites to '1'."""
        self._create_activity()
        response = self.client.delete(
            self.URL,
            {"team_id": str(self.team.team_id), "activity_id": "3-1-100-500"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


class ActivityHistoryViewTests(BaseAPITestCase):
    """GET /api/v2/chat/activity/history/ — fetch activity history."""

    URL = "/api/v2/chat/activity/history/"

    def test_returns_200_with_valid_params(self):
        self.authenticate()
        response = self.client.get(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "period_days": 7,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)

    def test_missing_team_id(self):
        self.authenticate()
        response = self.client.get(
            self.URL,
            {"user_id": str(self.user.id), "period_days": 7},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_missing_user_id(self):
        self.authenticate()
        response = self.client.get(
            self.URL,
            {"team_id": str(self.team.team_id), "period_days": 7},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_wrong_user_id_returns_403(self):
        self.authenticate()
        response = self.client.get(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user2.id),
                "period_days": 7,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_defaults_period_days_to_30(self):
        self.authenticate()
        response = self.client.get(
            self.URL,
            {"team_id": str(self.team.team_id), "user_id": str(self.user.id)},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ReadStatusViewTests(BaseAPITestCase):
    """PUT /api/v2/chat/read/ — upsert read status."""

    URL = "/api/v2/chat/read/"

    def _read_status_data(self, **overrides):
        defaults = {
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.id),
            "chat_type": 1,
            "chat_id": 100,
            "is_thread": False,
            "thread_id": 0,
            "last_read_message_id": 50,
        }
        defaults.update(overrides)
        return defaults

    def test_create_new_read_status(self):
        self.authenticate()
        response = self.client.put(self.URL, self._read_status_data(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_update_existing_read_status_with_higher_message_id(self):
        self.authenticate()
        self.client.put(self.URL, self._read_status_data(), format="json")

        response = self.client.put(
            self.URL,
            self._read_status_data(last_read_message_id=100),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_update_existing_read_status_with_lower_message_id_no_change(self):
        """last_read_message_id should not regress to a lower value."""
        self.authenticate()
        self.client.put(
            self.URL,
            self._read_status_data(last_read_message_id=100),
            format="json",
        )

        response = self.client.put(
            self.URL,
            self._read_status_data(last_read_message_id=50),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rs = ReadStatus.objects.get(
            team=self.team,
            user=self.user,
            chat_type=1,
            chat_id=100,
            thread_id=0,
        )
        self.assertEqual(rs.last_read_message_id, 100)

    def test_missing_required_field(self):
        self.authenticate()
        data = self._read_status_data()
        del data["chat_type"]
        response = self.client.put(self.URL, data, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ActivityReadStatusViewTests(BaseAPITestCase):
    """PUT /api/v2/chat/activity/read/ — mark single activity as read."""

    URL = "/api/v2/chat/activity/read/"

    def _create_activity(self, activity_id="1-1-100-500"):
        return ActivityFact.objects.create(
            team=self.team,
            activity_id=activity_id,
            activity_type=1,
            chat_type=1,
            chat_id=100,
            is_thread=False,
            thread_id=0,
            message_id=500,
            message_unique_key="msg-unique-1",
            first_line_content="Hello",
            sender=self.user,
            latest_reaction={},
            reactions=[],
            mentioned_user_ids=[],
        )

    def test_create_activity_read_status(self):
        self.authenticate()
        self._create_activity()
        response = self.client.put(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "activity_id": "1-1-100-500",
                "is_read": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_already_exists_returns_200(self):
        self.authenticate()
        self._create_activity()
        payload = {
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.id),
            "activity_id": "1-1-100-500",
            "is_read": True,
        }
        self.client.put(self.URL, payload, format="json")
        response = self.client.put(self.URL, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_existing_row_value_change_persists(self):
        """Regression: the update branch used to validate but never call
        `.save()`, so PUT-ing a different `is_read` against an existing row
        returned 200 yet left the DB untouched. Pre-seed an `is_read=False`
        row, PUT with `is_read=True`, and verify the row is actually flipped.
        """
        self.authenticate()
        activity = self._create_activity()
        ActivityReadStatus.objects.create(
            team=self.team,
            user=self.user,
            activity=activity,
            is_read=False,
        )

        response = self.client.put(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "activity_id": activity.activity_id,
                "is_read": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        row = ActivityReadStatus.objects.get(user=self.user, activity=activity)
        self.assertTrue(
            row.is_read,
            "Expected the existing row to be flipped to is_read=True; "
            "the update branch was missing serializer.save().",
        )
        # And no duplicate row got created.
        self.assertEqual(
            ActivityReadStatus.objects.filter(user=self.user, activity=activity).count(),
            1,
        )

    def test_mention_prefix_rewrite(self):
        """activity_id starting with '3' is rewritten to '1' before lookup."""
        self.authenticate()
        self._create_activity(activity_id="1-2-200-600")
        response = self.client.put(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "activity_id": "3-2-200-600",
                "is_read": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(ActivityReadStatus.objects.filter(activity_id="1-2-200-600").exists())


class MarkAllActivityAsReadViewTests(BaseAPITestCase):
    """PUT /api/v2/chat/activity/read/all/ — mark all activities for a single
    chat (DM/GM/PM/MDM) as read, including thread-message activities."""

    URL = "/api/v2/chat/activity/read/all/"

    def _seed_unread_statuses(
        self,
        count=3,
        chat_type=1,
        chat_id=100,
        is_thread=False,
        thread_id=0,
        id_prefix="a",
        with_read_status=True,
    ):
        """Seed `count` ActivityFact rows for the given chat, optionally with
        matching unread `ActivityReadStatus` rows.

        `id_prefix` keeps activity_id unique across multiple calls so callers
        can seed several distinct chats / threads in the same test.

        `with_read_status=False` skips creating the `ActivityReadStatus` rows
        — that mirrors the real-world case where a user has never opened any
        of the activities, so `ActivityReadStatusView.put` was never called
        and no rows exist yet.
        """
        activities = []
        for i in range(count):
            act = ActivityFact.objects.create(
                team=self.team,
                activity_id=f"1-{chat_type}-{chat_id}-{id_prefix}-{500 + i}",
                activity_type=1,
                chat_type=chat_type,
                chat_id=chat_id,
                is_thread=is_thread,
                thread_id=thread_id,
                message_id=500 + i,
                message_unique_key=f"msg-unique-{id_prefix}-{i}",
                first_line_content=f"msg {i}",
                sender=self.user,
                latest_reaction={},
                reactions=[],
                mentioned_user_ids=[],
            )
            activities.append(act)
            if with_read_status:
                ActivityReadStatus.objects.create(
                    team=self.team,
                    user=self.user,
                    activity=act,
                    is_read=False,
                )
        return activities

    def test_mark_all_as_read(self):
        self.authenticate()
        self._seed_unread_statuses(3, chat_type=1, chat_id=100)
        # Different chat — should NOT be flipped by the targeted call.
        other = self._seed_unread_statuses(2, chat_type=2, chat_id=200, id_prefix="other")

        response = self.client.put(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "chat_type": 1,
                "chat_id": 100,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Targeted chat: every entry now read.
        unread_target = ActivityReadStatus.objects.filter(
            team=self.team,
            user=self.user,
            is_read=False,
            activity__chat_type=1,
            activity__chat_id=100,
        ).count()
        self.assertEqual(unread_target, 0)

        # Other chat: untouched.
        unread_other = ActivityReadStatus.objects.filter(
            activity__activity_id__in=[a.activity_id for a in other],
            is_read=False,
        ).count()
        self.assertEqual(unread_other, 2)

    def test_mark_all_creates_read_statuses_when_none_exist(self):
        """Regression: clicking "Mark all as read" before the user has ever
        opened any activity in the chat must still persist as read.

        Reproduces the production bug where the bulk endpoint relied on a
        plain `.update()` that only flipped existing rows. With no
        `ActivityReadStatus` rows seeded, `.update()` matched zero rows and
        the next history fetch reported every activity as unread again.
        After the fix, `bulk_create(..., ignore_conflicts=True)` materialises
        the rows so `EXISTS(... is_read=True)` returns True on next read.
        """
        self.authenticate()
        activities = self._seed_unread_statuses(
            3, chat_type=1, chat_id=100, with_read_status=False
        )

        # Sanity: nothing yet.
        self.assertEqual(
            ActivityReadStatus.objects.filter(user=self.user).count(),
            0,
            "Precondition: no ActivityReadStatus rows should exist yet.",
        )

        response = self.client.put(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "chat_type": 1,
                "chat_id": 100,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # One row per activity, all is_read=True.
        rows = ActivityReadStatus.objects.filter(
            user=self.user,
            activity__activity_id__in=[a.activity_id for a in activities],
        )
        self.assertEqual(rows.count(), 3)
        self.assertEqual(rows.filter(is_read=True).count(), 3)

    def test_mark_all_mixed_existing_and_missing(self):
        """Some activities have a False row, some have no row at all.
        Both paths should end up is_read=True with exactly one row each.
        """
        self.authenticate()
        with_status = self._seed_unread_statuses(2, chat_type=3, chat_id=300, id_prefix="with")
        without_status = self._seed_unread_statuses(
            2, chat_type=3, chat_id=300, id_prefix="without", with_read_status=False
        )

        response = self.client.put(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "chat_type": 3,
                "chat_id": 300,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        all_ids = [a.activity_id for a in (*with_status, *without_status)]
        rows = ActivityReadStatus.objects.filter(user=self.user, activity__activity_id__in=all_ids)
        self.assertEqual(rows.count(), 4, "Expected exactly one row per activity.")
        self.assertEqual(rows.filter(is_read=False).count(), 0)

    def test_mark_all_is_idempotent(self):
        """Calling the endpoint twice should not duplicate rows (the
        unique `(user, activity_id)` constraint plus `ignore_conflicts`
        should make the second call a no-op)."""
        self.authenticate()
        activities = self._seed_unread_statuses(
            2, chat_type=1, chat_id=100, with_read_status=False
        )
        body = {
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.id),
            "chat_type": 1,
            "chat_id": 100,
        }

        first = self.client.put(self.URL, body, format="json")
        second = self.client.put(self.URL, body, format="json")
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.assertEqual(second.status_code, status.HTTP_200_OK)

        rows = ActivityReadStatus.objects.filter(
            user=self.user,
            activity__activity_id__in=[a.activity_id for a in activities],
        )
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.filter(is_read=True).count(), 2)

    def test_mark_all_includes_thread_activities(self):
        """Thread-message activities for the same chat are included in the bulk update."""
        self.authenticate()
        inline = self._seed_unread_statuses(2, chat_type=1, chat_id=100, id_prefix="inline")
        threaded = self._seed_unread_statuses(
            2,
            chat_type=1,
            chat_id=100,
            is_thread=True,
            thread_id=42,
            id_prefix="thread",
        )

        response = self.client.put(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "chat_type": 1,
                "chat_id": 100,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        all_ids = [a.activity_id for a in (*inline, *threaded)]
        unread = ActivityReadStatus.objects.filter(
            activity__activity_id__in=all_ids, is_read=False
        ).count()
        self.assertEqual(unread, 0)

    def test_mark_all_no_unread(self):
        self.authenticate()
        response = self.client.put(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "chat_type": 1,
                "chat_id": 100,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class UnauthorizedAccessTests(TestCase):
    """Unauthenticated requests should return 401."""

    def setUp(self):
        self.client = APIClient()

    def test_activity_put_401(self):
        response = self.client.put("/api/v2/chat/activity/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_activity_delete_401(self):
        response = self.client.delete("/api/v2/chat/activity/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_activity_history_401(self):
        response = self.client.get("/api/v2/chat/activity/history/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_read_status_put_401(self):
        response = self.client.put("/api/v2/chat/read/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_activity_read_status_put_401(self):
        response = self.client.put("/api/v2/chat/activity/read/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_mark_all_read_401(self):
        response = self.client.put("/api/v2/chat/activity/read/all/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class ValidationFailureTests(BaseAPITestCase):
    """Missing required fields should return 400."""

    def test_activity_delete_missing_team_id(self):
        self.authenticate()
        response = self.client.delete(
            "/api/v2/chat/activity/",
            {"activity_id": "1-1-100-500"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_activity_delete_missing_activity_id(self):
        self.authenticate()
        response = self.client.delete(
            "/api/v2/chat/activity/",
            {"team_id": str(self.team.team_id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_read_status_missing_chat_id(self):
        self.authenticate()
        response = self.client.put(
            "/api/v2/chat/read/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "chat_type": 1,
                "is_thread": False,
                "thread_id": 0,
                "last_read_message_id": 50,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_activity_read_status_missing_activity_id(self):
        self.authenticate()
        response = self.client.put(
            "/api/v2/chat/activity/read/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "is_read": True,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mark_all_read_missing_team_id(self):
        self.authenticate()
        response = self.client.put(
            "/api/v2/chat/activity/read/all/",
            {"user_id": str(self.user.id), "chat_type": 1, "chat_id": 100},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mark_all_read_missing_user_id(self):
        self.authenticate()
        response = self.client.put(
            "/api/v2/chat/activity/read/all/",
            {"team_id": str(self.team.team_id), "chat_type": 1, "chat_id": 100},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mark_all_read_missing_chat_type(self):
        self.authenticate()
        response = self.client.put(
            "/api/v2/chat/activity/read/all/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "chat_id": 100,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_mark_all_read_missing_chat_id(self):
        self.authenticate()
        response = self.client.put(
            "/api/v2/chat/activity/read/all/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "chat_type": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
