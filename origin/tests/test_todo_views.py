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


class TestToDoItemMove(TestCase):
    """PATCH `local_date` — "move this to tomorrow", for the leftovers of a
    day. It's the only patch field that changes which group a row belongs
    to, so what's covered here is the re-homing, not the field plumbing."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="mover", email="mover@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        self.team = TeamMaster.objects.create(
            team_name="Move Team", team_email="move@test.com", owner=self.user
        )
        self.today = timezone.localdate()
        self.tomorrow = self.today + timedelta(days=1)
        self.group = ToDoGroup.objects.create(team=self.team, user=self.user, local_date=self.today)

    def _item(self, **kwargs):
        kwargs.setdefault("group", self.group)
        kwargs.setdefault("title", "Leftover")
        return ToDoItem.objects.create(**kwargs)

    def _move(self, item, to, **extra):
        return self.client.patch(
            f"/api/v2/todo/items/{item.item_id}/",
            {"local_date": to.isoformat() if hasattr(to, "isoformat") else to, **extra},
            format="json",
        )

    def test_move_creates_the_target_day_lazily(self):
        """Nothing creates a group but item creation, so the move has to make
        tomorrow's group itself — the common case, since the user is moving a
        leftover before tomorrow exists."""
        item = self._item()
        res = self._move(item, self.tomorrow)

        self.assertEqual(res.status_code, 200)
        target = ToDoGroup.objects.get(team=self.team, user=self.user, local_date=self.tomorrow)
        item.refresh_from_db()
        self.assertEqual(item.group_id, target.group_id)
        self.assertEqual(res.data["groupId"], target.group_id)

    def test_move_reuses_an_existing_target_day(self):
        """`uniq_todo_group_per_day` makes a second group for the same date an
        IntegrityError, so the move must join the group already there."""
        existing = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=self.tomorrow
        )
        item = self._item()

        res = self._move(item, self.tomorrow)

        self.assertEqual(res.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.group_id, existing.group_id)
        self.assertEqual(
            ToDoGroup.objects.filter(user=self.user, local_date=self.tomorrow).count(), 1
        )

    def test_subitems_follow_their_parent(self):
        """A child's day IS its parent's group — create scopes the parent
        lookup to the group — so leaving children behind would orphan them
        from the row they're steps of."""
        parent = self._item()
        child = self._item(parent_item=parent, title="Step 1")

        res = self._move(parent, self.tomorrow)

        self.assertEqual(res.status_code, 200)
        parent.refresh_from_db()
        child.refresh_from_db()
        self.assertEqual(child.group_id, parent.group_id)
        self.assertNotEqual(child.group_id, self.group.group_id)

    def test_moved_child_is_marked_updated_for_reindex(self):
        """The children move by bulk `update()`, which does NOT fire
        `auto_now`. The incremental chunker selects on `ts_updated_at`, so an
        unbumped child stays missing from search until a full reindex."""
        parent = self._item()
        child = self._item(parent_item=parent, title="Step 1")
        ToDoItem.objects.filter(pk=child.pk).update(
            ts_updated_at=timezone.now() - timedelta(days=3)
        )
        before = ToDoItem.objects.get(pk=child.pk).ts_updated_at

        self._move(parent, self.tomorrow)

        self.assertGreater(ToDoItem.objects.get(pk=child.pk).ts_updated_at, before)

    def test_moving_a_subitem_alone_is_refused(self):
        parent = self._item()
        child = self._item(parent_item=parent, title="Step 1")

        res = self._move(child, self.tomorrow)

        self.assertEqual(res.status_code, 400)
        child.refresh_from_db()
        self.assertEqual(child.group_id, self.group.group_id)

    def test_emptying_a_day_recomputes_both_groups(self):
        """The source day loses its only open to-do, which makes it a
        completed day; the target gains one, which makes it open."""
        done = self._item(title="Done", is_completed=True)
        ToDoGroup.objects.filter(pk=self.group.pk).update(is_completed=False)
        leftover = self._item(title="Leftover")
        target = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=self.tomorrow, is_completed=True
        )

        self._move(leftover, self.tomorrow)

        self.group.refresh_from_db()
        target.refresh_from_db()
        self.assertTrue(self.group.is_completed, "source day has only the completed item left")
        self.assertFalse(target.is_completed, "target day now holds an open item")
        self.assertEqual(done.group_id, self.group.group_id)

    def test_a_malformed_date_changes_nothing(self):
        item = self._item()
        res = self._move(item, "next tuesday", title="Renamed")

        self.assertEqual(res.status_code, 400)
        item.refresh_from_db()
        self.assertEqual(item.group_id, self.group.group_id)
        self.assertEqual(item.title, "Leftover", "sibling fields must not half-apply")

    def test_moving_to_the_same_day_is_a_no_op(self):
        item = self._item()
        res = self._move(item, self.today)

        self.assertEqual(res.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.group_id, self.group.group_id)
        self.assertEqual(
            ToDoGroup.objects.filter(user=self.user).count(), 1, "no empty group created"
        )

    def test_another_users_item_cannot_be_moved(self):
        other = User.objects.create_user(
            username="other", email="other@test.com", password="testpass123"
        )
        other_group = ToDoGroup.objects.create(team=self.team, user=other, local_date=self.today)
        item = ToDoItem.objects.create(group=other_group, title="Theirs")

        res = self._move(item, self.tomorrow)

        self.assertEqual(res.status_code, 403)
        item.refresh_from_db()
        self.assertEqual(item.group_id, other_group.group_id)

    def test_move_can_ride_along_with_a_content_patch(self):
        """The client sends one PATCH; `local_date` must not shadow the
        ordinary fields."""
        item = self._item()
        res = self._move(item, self.tomorrow, title="Carried over", is_completed=False)

        self.assertEqual(res.status_code, 200)
        item.refresh_from_db()
        self.assertEqual(item.title, "Carried over")
        self.assertEqual(item.group.local_date, self.tomorrow)
