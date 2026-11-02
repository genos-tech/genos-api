from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem, ToDoSchedule
from origin.models.common.team_models import TeamMaster, TeamMembers

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


class TestToDoCategoryColors(TestCase):
    """MIP-759: a To Do tag carries a chip colour. Existing tags and the
    agent's by-name create take the neutral default; writes are validated;
    a rename onto an existing name is a 400, not a 500."""

    URL = "/api/v2/todo/categories/"

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="catuser", email="cat@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        self.team = TeamMaster.objects.create(
            team_name="Test Team", team_email="team@test.com", owner=self.user
        )

    def _make(self, name, **kw):
        return ToDoCategory.objects.create(team=self.team, user=self.user, name=name, **kw)

    def _post(self, **body):
        body.setdefault("team_id", str(self.team.team_id))
        return self.client.post(self.URL, body, format="json")

    def _patch(self, category, **body):
        return self.client.patch(f"{self.URL}{category.category_id}/", body, format="json")

    def test_a_tag_created_by_name_gets_the_neutral_default(self):
        res = self._post(name="Work")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["color"], "#808080")
        self.assertEqual(res.data["textColor"], "white")

    def test_create_takes_a_colour(self):
        res = self._post(name="Work", color="#1DC200", text_color="black")
        self.assertEqual(res.status_code, 201)
        self.assertEqual((res.data["color"], res.data["textColor"]), ("#1dc200", "black"))
        self.assertEqual(ToDoCategory.objects.get(name="Work").color, "#1dc200")

    def test_the_list_carries_the_colour(self):
        self._make("Home", color="#0044c2", text_color="white")
        res = self.client.get(self.URL, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.data[0]["color"], "#0044c2")
        self.assertEqual(res.data[0]["textColor"], "white")

    def test_create_refuses_a_bad_colour(self):
        for body in (
            {"color": "red"},
            {"color": "#12345"},
            {"color": "javascript:alert(1)"},
            {"color": 5},
            {"text_color": "purple"},
            {"text_color": None},
        ):
            with self.subTest(body=body):
                res = self._post(name="Bad", **body)
                self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(ToDoCategory.objects.filter(name="Bad").exists())

    def test_create_refuses_a_name_over_the_column_limit(self):
        self.assertEqual(self._post(name="x" * 65).status_code, 400)

    def test_posting_an_existing_name_keeps_its_colour(self):
        self._make("Work", color="#0044c2")
        res = self._post(name="Work", color="#ff2323")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["color"], "#0044c2")

    def test_patch_recolours_and_renames_together(self):
        cat = self._make("Work")
        res = self._patch(cat, name="Deep work", color="#8e23ff", text_color="white")
        self.assertEqual(res.status_code, 200)
        cat.refresh_from_db()
        self.assertEqual((cat.name, cat.color, cat.text_color), ("Deep work", "#8e23ff", "white"))

    def test_patch_without_colour_leaves_it(self):
        cat = self._make("Work", color="#8e23ff", text_color="white")
        self.assertEqual(self._patch(cat, sort_order=4).status_code, 200)
        cat.refresh_from_db()
        self.assertEqual(cat.color, "#8e23ff")

    def test_patch_refuses_a_bad_colour_and_changes_nothing(self):
        cat = self._make("Work")
        res = self._patch(cat, name="Renamed", color="nope")
        self.assertEqual(res.status_code, 400)
        cat.refresh_from_db()
        self.assertEqual((cat.name, cat.color), ("Work", "#808080"))

    def test_renaming_onto_an_existing_name_is_a_400_not_a_500(self):
        self._make("Home")
        cat = self._make("Work")
        res = self._patch(cat, name="Home")
        self.assertEqual(res.status_code, 400)
        cat.refresh_from_db()
        self.assertEqual(cat.name, "Work")

    def test_renaming_to_its_own_name_is_fine(self):
        cat = self._make("Work")
        self.assertEqual(self._patch(cat, name="Work", color="#ff2323").status_code, 200)

    def test_another_users_tag_with_the_same_name_is_no_clash(self):
        other = User.objects.create_user(
            username="other", email="other@test.com", password="testpass123"
        )
        ToDoCategory.objects.create(team=self.team, user=other, name="Home")
        cat = self._make("Work")
        self.assertEqual(self._patch(cat, name="Home").status_code, 200)

    def test_an_empty_name_is_refused(self):
        cat = self._make("Work")
        self.assertEqual(self._patch(cat, name="   ").status_code, 400)

    def test_a_tag_is_personal(self):
        other = User.objects.create_user(
            username="other2", email="other2@test.com", password="testpass123"
        )
        theirs = ToDoCategory.objects.create(team=self.team, user=other, name="Theirs")
        self.assertEqual(self._patch(theirs, color="#ff2323").status_code, 403)
        theirs.refresh_from_db()
        self.assertEqual(theirs.color, "#808080")


class TestToDoCategoryMustBelongToTheTodosTeam(TestCase):
    """MIP-918: a tag is per user AND per team, and the app lists them by team
    (`ToDoCategoryListView`), so it can only ever offer a to-do or a schedule
    the tags of ITS team. The four writes that take a `category_id` (create and
    PATCH, for a to-do and for a schedule) used to check the user and not the
    team, so a person in two teams could hang team A's tag on team B's to-do by
    id. They answer such an id exactly as they answer one that does not exist.

    A tag with no team (`team=None`: legacy, or its team was deleted) is not in
    anyone's list either, and is refused the same way.
    """

    REFUSAL = {"error": "category_id not found for this user."}

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="twoteams", email="two@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        self.team_a = TeamMaster.objects.create(
            team_name="Team A", team_email="a@test.com", owner=self.user
        )
        self.team_b = TeamMaster.objects.create(
            team_name="Team B", team_email="b@test.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team_a, attendee=self.user)
        TeamMembers.objects.create(team=self.team_b, attendee=self.user)
        # Same name in both teams: `uniq_todo_category_per_user` is per team.
        self.cat_a = ToDoCategory.objects.create(team=self.team_a, user=self.user, name="work")
        self.cat_b = ToDoCategory.objects.create(team=self.team_b, user=self.user, name="work")
        self.legacy = ToDoCategory.objects.create(team=None, user=self.user, name="legacy")
        self.today = timezone.localdate()
        self.group_b = ToDoGroup.objects.create(
            team=self.team_b, user=self.user, local_date=self.today
        )
        self.item_b = ToDoItem.objects.create(group=self.group_b, title="in team B")
        self.child_b = ToDoItem.objects.create(
            group=self.group_b, parent_item=self.item_b, title="step of B"
        )
        self.schedule_b = self._schedule(self.team_b)

    def _schedule(self, team, **kw):
        return ToDoSchedule.objects.create(
            team=team,
            user=self.user,
            title="standup",
            rrule="RRULE:FREQ=DAILY",
            start_date=self.today,
            **kw,
        )

    def _create_item(self, team, **body):
        body.setdefault("team_id", team.team_id)
        body.setdefault("local_date", self.today.isoformat())
        body.setdefault("title", "brand new")
        return self.client.post("/api/v2/todo/items/", body, format="json")

    def _patch_item(self, item, **body):
        url = reverse("todo_item_detail", args=[item.item_id])
        return self.client.patch(url, body, format="json")

    def _create_schedule(self, team, **body):
        body.setdefault("team_id", team.team_id)
        body.setdefault("title", "brand new rule")
        body.setdefault("rrule", "RRULE:FREQ=DAILY")
        body.setdefault("start_date", self.today.isoformat())
        return self.client.post("/api/v2/todo/schedules/", body, format="json")

    def _patch_schedule(self, schedule, **body):
        url = reverse("todo_schedule_detail", args=[schedule.schedule_id])
        return self.client.patch(url, body, format="json")

    # -- create a to-do ------------------------------------------------------

    def test_creating_a_todo_refuses_another_teams_tag(self):
        res = self._create_item(self.team_b, category_id=self.cat_a.category_id)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data, self.REFUSAL)
        self.assertFalse(ToDoItem.objects.filter(title="brand new").exists())

    def test_the_refusal_is_the_one_an_unknown_id_gets(self):
        unknown = self._create_item(self.team_b, category_id=999999)
        other_team = self._create_item(self.team_b, category_id=self.cat_a.category_id)
        self.assertEqual(other_team.status_code, unknown.status_code)
        self.assertEqual(other_team.data, unknown.data)

    def test_creating_a_todo_takes_its_own_teams_tag(self):
        res = self._create_item(self.team_b, category_id=self.cat_b.category_id)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(
            ToDoItem.objects.get(title="brand new").category_id, self.cat_b.category_id
        )

    def test_each_team_takes_its_own_tag_of_the_same_name(self):
        res = self._create_item(self.team_a, category_id=self.cat_a.category_id)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(
            ToDoItem.objects.get(title="brand new").category_id, self.cat_a.category_id
        )

    def test_creating_a_todo_refuses_a_tag_with_no_team(self):
        res = self._create_item(self.team_b, category_id=self.legacy.category_id)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data, self.REFUSAL)
        self.assertFalse(ToDoItem.objects.filter(title="brand new").exists())

    # -- PATCH a to-do -------------------------------------------------------

    def test_patching_a_todo_refuses_another_teams_tag_and_changes_nothing(self):
        res = self._patch_item(self.item_b, title="renamed", category_id=self.cat_a.category_id)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data, self.REFUSAL)
        self.item_b.refresh_from_db()
        self.child_b.refresh_from_db()
        self.assertEqual(self.item_b.title, "in team B")
        self.assertIsNone(self.item_b.category_id)
        self.assertIsNone(self.child_b.category_id)

    def test_patching_a_todo_takes_its_own_teams_tag_and_hands_it_to_the_subitems(self):
        res = self._patch_item(self.item_b, category_id=self.cat_b.category_id)
        self.assertEqual(res.status_code, 200)
        self.item_b.refresh_from_db()
        self.child_b.refresh_from_db()
        self.assertEqual(self.item_b.category_id, self.cat_b.category_id)
        self.assertEqual(self.child_b.category_id, self.cat_b.category_id)

    def test_patching_a_todo_refuses_a_tag_with_no_team(self):
        res = self._patch_item(self.item_b, category_id=self.legacy.category_id)
        self.assertEqual(res.status_code, 400)
        self.item_b.refresh_from_db()
        self.assertIsNone(self.item_b.category_id)

    def test_a_refused_tag_leaves_the_tag_the_todo_already_has(self):
        self.item_b.category = self.cat_b
        self.item_b.save(update_fields=["category"])
        res = self._patch_item(self.item_b, category_id=self.cat_a.category_id)
        self.assertEqual(res.status_code, 400)
        self.item_b.refresh_from_db()
        self.assertEqual(self.item_b.category_id, self.cat_b.category_id)

    def test_a_todo_whose_team_is_gone_takes_no_tag_at_all(self):
        """`team=None` on the group is a deleted team. `filter(team_id=None)`
        would be `IS NULL` and match the team-less tag, so it is guarded."""
        orphan = ToDoGroup.objects.create(
            team=None, user=self.user, local_date=self.today - timedelta(days=1)
        )
        item = ToDoItem.objects.create(group=orphan, title="orphaned")
        for category in (self.legacy, self.cat_a, self.cat_b):
            with self.subTest(category=category.name, team=category.team_id):
                res = self._patch_item(item, category_id=category.category_id)
                self.assertEqual(res.status_code, 400)
                item.refresh_from_db()
                self.assertIsNone(item.category_id)

    def test_clearing_the_tag_still_works(self):
        self.item_b.category = self.cat_b
        self.item_b.save(update_fields=["category"])
        res = self._patch_item(self.item_b, category_id=None)
        self.assertEqual(res.status_code, 200)
        self.item_b.refresh_from_db()
        self.assertIsNone(self.item_b.category_id)

    # -- create a schedule ---------------------------------------------------

    def test_creating_a_schedule_refuses_another_teams_tag(self):
        res = self._create_schedule(self.team_b, category_id=self.cat_a.category_id)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data, self.REFUSAL)
        self.assertFalse(ToDoSchedule.objects.filter(title="brand new rule").exists())

    def test_creating_a_schedule_takes_its_own_teams_tag(self):
        res = self._create_schedule(self.team_b, category_id=self.cat_b.category_id)
        self.assertEqual(res.status_code, 201)
        schedule = ToDoSchedule.objects.get(title="brand new rule")
        self.assertEqual(schedule.category_id, self.cat_b.category_id)
        self.assertEqual(schedule.team_id, self.team_b.team_id)

    def test_creating_a_schedule_without_a_tag_still_works(self):
        res = self._create_schedule(self.team_b)
        self.assertEqual(res.status_code, 201)
        self.assertIsNone(ToDoSchedule.objects.get(title="brand new rule").category_id)

    def test_creating_a_schedule_refuses_a_tag_with_no_team(self):
        res = self._create_schedule(self.team_b, category_id=self.legacy.category_id)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(ToDoSchedule.objects.filter(title="brand new rule").exists())

    # -- PATCH a schedule ----------------------------------------------------

    def test_patching_a_schedule_refuses_another_teams_tag_and_changes_nothing(self):
        res = self._patch_schedule(
            self.schedule_b, title="renamed", category_id=self.cat_a.category_id
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data, self.REFUSAL)
        self.schedule_b.refresh_from_db()
        self.assertEqual(self.schedule_b.title, "standup")
        self.assertIsNone(self.schedule_b.category_id)

    def test_patching_a_schedule_takes_its_own_teams_tag(self):
        res = self._patch_schedule(self.schedule_b, category_id=self.cat_b.category_id)
        self.assertEqual(res.status_code, 200)
        self.schedule_b.refresh_from_db()
        self.assertEqual(self.schedule_b.category_id, self.cat_b.category_id)

    def test_patching_a_schedule_refuses_a_tag_with_no_team(self):
        res = self._patch_schedule(self.schedule_b, category_id=self.legacy.category_id)
        self.assertEqual(res.status_code, 400)
        self.schedule_b.refresh_from_db()
        self.assertIsNone(self.schedule_b.category_id)

    def test_a_schedule_whose_team_is_gone_takes_no_tag_at_all(self):
        orphan = self._schedule(None)
        for category in (self.legacy, self.cat_a, self.cat_b):
            with self.subTest(category=category.name, team=category.team_id):
                res = self._patch_schedule(orphan, category_id=category.category_id)
                self.assertEqual(res.status_code, 400)
                orphan.refresh_from_db()
                self.assertIsNone(orphan.category_id)

    def test_clearing_a_schedules_tag_still_works(self):
        self.schedule_b.category = self.cat_b
        self.schedule_b.save(update_fields=["category"])
        res = self._patch_schedule(self.schedule_b, category_id=None)
        self.assertEqual(res.status_code, 200)
        self.schedule_b.refresh_from_db()
        self.assertIsNone(self.schedule_b.category_id)

    def test_a_patch_that_does_not_mention_the_tag_keeps_it(self):
        """The materializer PATCHes `lastMaterializedDate` every day. A body
        without `category_id` must not go near the category check."""
        self.schedule_b.category = self.cat_b
        self.schedule_b.save(update_fields=["category"])
        res = self._patch_schedule(self.schedule_b, is_active=False)
        self.assertEqual(res.status_code, 200)
        self.schedule_b.refresh_from_db()
        self.assertFalse(self.schedule_b.is_active)
        self.assertEqual(self.schedule_b.category_id, self.cat_b.category_id)
