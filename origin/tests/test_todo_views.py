from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.common.team_models import TeamMaster

User = get_user_model()


class TestToDoGroupListView(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="testuser", email="test@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")

        self.team = TeamMaster.objects.create(
            team_name="Test Team",
            team_email="team@test.com",
            owner=self.user,
        )

    def _group(self, local_date):
        return ToDoGroup.objects.create(team=self.team, user=self.user, local_date=local_date)

    def _list(self, **params):
        params.setdefault("team_id", str(self.team.team_id))
        return self.client.get("/api/v2/todo/groups/", params)

    def test_group_dated_clients_tomorrow_is_returned(self):
        """Regression: the server runs in UTC, but groups are keyed by the
        client's local date, which can be one calendar day ahead of UTC. A
        group dated server-tomorrow (a UTC+9 user's "today") must still come
        back from GET with no explicit `to` — otherwise a freshly created
        todo vanishes on the next fetch. Fails against the old
        `date_to=today` clamp."""
        client_today = self._group(timezone.localdate() + timedelta(days=1))
        ToDoItem.objects.create(group=client_today, title="Untitled todo")

        res = self._list()
        self.assertEqual(res.status_code, 200)
        returned_ids = {g["groupId"] for g in res.data}
        self.assertIn(client_today.group_id, returned_ids)

    def test_todays_group_is_returned(self):
        today_group = self._group(timezone.localdate())
        res = self._list()
        self.assertEqual(res.status_code, 200)
        self.assertIn(today_group.group_id, {g["groupId"] for g in res.data})

    def test_explicit_to_still_clamps_future_groups(self):
        """An explicit `to` must still bound the result so the default-slack
        fix doesn't leak arbitrary future-dated groups when a caller asks for
        a specific upper bound."""
        today = timezone.localdate()
        self._group(today)
        future = self._group(today + timedelta(days=5))

        res = self._list(to=today.isoformat())
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(future.group_id, {g["groupId"] for g in res.data})

    def test_missing_team_id_is_client_error(self):
        res = self.client.get("/api/v2/todo/groups/")
        self.assertEqual(res.status_code, 400)
