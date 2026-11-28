"""A to-do day with nothing left on it is deleted (MIP-2574).

The app shows a day that exists, so an emptied future day stayed on screen as a list nobody asked
for. Every path that takes a to-do off a day is covered: the app's delete and move, the in-app
agent's `delete_todo_item` and `update_todo_item`, the same two over MCP, and the one-off
`prune_empty_todo_days` for the days left over from before. The owner's today is the exception.
"""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.common.team_models import TeamMembers
from origin.search_engine.agent.tools.create_todo_item import CREATE_TODO_ITEM
from origin.services import todo_items
from origin.views.public.mcp.tools import SCOPE_WRITE

from .test_agent_tool_todo_writes import DELETE_TODO_ITEM, PURGE, TodoToolBase
from .test_mcp_todo_writes import McpTodoBase

User = get_user_model()


def exists(group) -> bool:
    return ToDoGroup.objects.filter(pk=group.pk).exists()


class EmptyDayBase(TodoToolBase):
    def setUp(self):
        super().setUp()
        patcher = patch(PURGE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ui_delete(self, item):
        res = self.client.delete(f"/api/v2/todo/items/{item.item_id}/")
        self.assertEqual(res.status_code, 204)

    def tool_delete(self, item):
        DELETE_TODO_ITEM.run({"item_id": item.item_id}, self.ctx)

    def deletes(self):
        return (("app", self.ui_delete), ("tool", self.tool_delete))

    def moves(self):
        return (
            ("app", lambda item, to: self.ui_patch(item, local_date=to.isoformat())),
            ("tool", lambda item, to: self.update(item, target_date=to.isoformat())),
        )


class RefusedCreateTests(EmptyDayBase):
    """A create the app refuses leaves no day behind (MIP-2576)."""

    def ui_create(self, on, **body):
        return self.client.post(
            "/api/v2/todo/items/",
            {"team_id": str(self.team.team_id), "local_date": on.isoformat(), "title": "x", **body},
            format="json",
        )

    def test_a_refused_create_leaves_no_new_day(self):
        elsewhere = self.todo(on=self.day(1))
        refusals = (
            ({"parent_item_id": 999999}, "parent_item_id not found in this group."),
            ({"parent_item_id": elsewhere.item_id}, "parent_item_id not found in this group."),
            ({"category_id": 999999}, "category_id not found for this user."),
        )
        for body, error in refusals:
            with self.subTest(body=body):
                res = self.ui_create(self.day(5), **body)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data, {"error": error})
                self.assertFalse(ToDoGroup.objects.filter(local_date=self.day(5)).exists())

    def test_a_refused_create_keeps_a_day_that_was_already_there(self):
        parent = self.todo(on=self.day(2))
        kid = self.todo("kid", on=self.day(2), parent=parent)
        res = self.ui_create(self.day(2), parent_item_id=kid.item_id)
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data, {"error": "Only one level of nesting is supported."})
        self.assertTrue(exists(parent.group))
        self.assertEqual(ToDoItem.objects.filter(group=parent.group).count(), 2)

    def test_a_tool_create_that_fails_part_way_leaves_no_new_day(self):
        with (
            patch("origin.services.todo_reminders.set_reminder", side_effect=RuntimeError("boom")),
            self.assertRaises(RuntimeError),
        ):
            CREATE_TODO_ITEM.run(
                {"title": "x", "target_date": self.day(5).isoformat(), "remind_in_minutes": 5},
                self.ctx,
            )
        self.assertFalse(ToDoGroup.objects.filter(local_date=self.day(5)).exists())


class DeleteTests(EmptyDayBase):
    def test_deleting_the_last_to_do_of_a_future_or_past_day_deletes_the_day(self):
        for offset in (1, 12, -1):
            for surface, delete in self.deletes():
                with self.subTest(offset=offset, surface=surface):
                    item = self.todo(on=self.day(offset))
                    group = item.group
                    delete(item)
                    self.assertFalse(exists(group))

    def test_a_parent_takes_its_sub_items_and_the_day(self):
        for surface, delete in self.deletes():
            with self.subTest(surface=surface):
                parent = self.todo(on=self.day(2))
                self.todo("kid", on=self.day(2), parent=parent)
                group = parent.group
                delete(parent)
                self.assertFalse(exists(group))

    def test_a_day_with_another_to_do_stays(self):
        for surface, delete in self.deletes():
            with self.subTest(surface=surface):
                item = self.todo(on=self.day(3))
                other = self.todo("other", on=self.day(3), is_completed=True)
                delete(item)
                self.assertTrue(exists(other.group))
                other.group.refresh_from_db()
                self.assertTrue(other.group.is_completed)
                other.delete()

    def test_deleting_the_only_sub_item_keeps_the_parents_day(self):
        parent = self.todo(on=self.day(3))
        kid = self.todo("kid", on=self.day(3), parent=parent)
        self.ui_delete(kid)
        self.assertTrue(exists(parent.group))

    def test_today_stays(self):
        for surface, delete in self.deletes():
            with self.subTest(surface=surface):
                item = self.todo(on=self.day(0))
                group = item.group
                delete(item)
                self.assertTrue(exists(group))

    def test_today_is_the_owners_not_the_servers(self):
        # 20:00 UTC on the 10th is already the 11th in Tokyo.
        now = datetime(2026, 11, 10, 20, 0, tzinfo=dt_timezone.utc)
        server_day = self.group(now.date())
        owner_day = self.group(now.date() + timedelta(days=1))
        with patch("django.utils.timezone.now", return_value=now):
            self.assertTrue(todo_items.delete_day_if_empty(server_day.group_id))
            self.assertFalse(todo_items.delete_day_if_empty(owner_day.group_id))

    def test_the_day_is_locked_before_its_items_are_counted_and_before_an_item_joins(self):
        group = self.group(self.day(4))
        for call in (
            lambda: todo_items.get_or_create_group(self.team.team_id, self.user.id, self.day(4)),
            lambda: todo_items.delete_day_if_empty(group.group_id),
        ):
            with CaptureQueriesContext(connection) as queries:
                call()
            first = next(q["sql"] for q in queries.captured_queries if "SELECT" in q["sql"])
            self.assertIn("FOR UPDATE", first)


class MoveTests(EmptyDayBase):
    def test_moving_the_last_to_do_away_deletes_the_day_it_left(self):
        for surface, move in self.moves():
            with self.subTest(surface=surface):
                parent = self.todo(on=self.day(5))
                kid = self.todo("kid", on=self.day(5), parent=parent)
                old = parent.group
                move(parent, self.day(6))
                self.assertFalse(exists(old))
                for item in (parent, kid):
                    item.refresh_from_db()
                    self.assertEqual(item.group.local_date, self.day(6))
                ToDoItem.objects.filter(pk=parent.pk).delete()

    def test_a_day_with_another_to_do_stays(self):
        for surface, move in self.moves():
            with self.subTest(surface=surface):
                item = self.todo(on=self.day(5))
                other = self.todo("other", on=self.day(5))
                move(item, self.day(6))
                self.assertTrue(exists(other.group))

    def test_today_stays_when_its_last_to_do_moves(self):
        for surface, move in self.moves():
            with self.subTest(surface=surface):
                item = self.todo(on=self.day(0))
                old = item.group
                move(item, self.day(1))
                self.assertTrue(exists(old))

    def test_a_move_to_the_same_day_deletes_nothing(self):
        for surface, move in self.moves():
            with self.subTest(surface=surface):
                item = self.todo(on=self.day(7))
                move(item, self.day(7))
                self.assertTrue(exists(item.group))


class McpTests(McpTodoBase):
    def setUp(self):
        super().setUp()
        self.group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=self.today + timedelta(days=2)
        )
        self.item = ToDoItem.objects.create(group=self.group, title="Only one")

    def run_tool(self, name, arguments):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        with patch(PURGE):
            text, is_error = self._text(name, arguments)
        self.assertFalse(is_error, text)

    def test_delete_todo_item_deletes_the_emptied_day(self):
        self.run_tool("delete_todo_item", {"item_id": self.item.item_id})
        self.assertFalse(exists(self.group))

    def test_update_todo_item_deletes_the_day_it_moved_the_last_to_do_off(self):
        target = self.today + timedelta(days=3)
        self.run_tool(
            "update_todo_item", {"item_id": self.item.item_id, "target_date": target.isoformat()}
        )
        self.assertFalse(exists(self.group))


class PruneCommandTests(EmptyDayBase):
    def setUp(self):
        super().setUp()
        other = User.objects.create_user(
            username="other-pruned", email="other-pruned@test.com", password="pw123456!"
        )
        TeamMembers.objects.create(team=self.team, attendee=other)
        # `other` has no time zone: their today is the server's.
        server_today = datetime.now(dt_timezone.utc).date()
        self.doomed = [
            self.group(self.day(1)),
            self.group(self.day(9)),
            self.group(server_today + timedelta(days=2), user=other),
        ]
        self.kept = [
            self.group(self.day(0)),
            self.group(self.day(-4)),
            self.group(server_today, user=other),
            self.todo("full", on=self.day(2)).group,
        ]

    def prune(self, *args):
        out = StringIO()
        call_command("prune_empty_todo_days", *args, stdout=out)
        return out.getvalue()

    def test_the_dry_run_counts_and_writes_nothing(self):
        out = self.prune()
        self.assertIn("DRY RUN", out)
        self.assertIn("would remove 3 empty days of 2 members", out)
        self.assertTrue(all(exists(g) for g in self.doomed + self.kept))

    def test_apply_removes_only_empty_days_after_each_members_today(self):
        out = self.prune("--apply")
        self.assertIn("removed 3 of 3 empty days found (2 members)", out)
        self.assertFalse(any(exists(g) for g in self.doomed))
        self.assertTrue(all(exists(g) for g in self.kept))
        self.assertIn("removed 0 of 0", self.prune("--apply"))
