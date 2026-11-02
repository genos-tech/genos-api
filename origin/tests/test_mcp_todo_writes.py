"""Over MCP, to-dos can be rescheduled, reminded about and deleted (MIP-884).

The tool-level rules (twin tests against the REST endpoints, the reminder
window, ownership) are in `test_agent_tool_todo_writes`. This is what the
endpoint adds: the arguments are published, the delete needs the key's
destructive opt-in, and the descriptions are written for a caller who has no
approval card.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.utils import timezone

from origin.models.chat.todo_models import ToDoGroup, ToDoItem, ToDoItemReminder
from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.services import todo_reminders
from origin.tests.test_mcp_endpoint import McpBase

TOKYO = ZoneInfo("Asia/Tokyo")
PURGE = "origin.services.todo_items.purge_todo_item"


class McpTodoBase(McpBase):
    def setUp(self):
        super().setUp()
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        self.today = timezone.now().astimezone(TOKYO).date()
        self.group = ToDoGroup.objects.create(team=self.team, user=self.user, local_date=self.today)
        self.item = ToDoItem.objects.create(group=self.group, title="Call the lawyer")

    def _listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}

    def _text(self, name, arguments):
        return self._tool_text(self._call(name, arguments))

    def _pending(self, item=None):
        return ToDoItemReminder.objects.filter(
            item=item or self.item, fired_at__isnull=True, cancelled_at__isnull=True
        )


class TestPublishedArguments(McpTodoBase):
    def test_update_publishes_the_day_and_the_reminder_arguments(self):
        self._key(scope=SCOPE_WRITE)
        props = self._listed()["update_todo_item"]["inputSchema"]["properties"]
        for name in ("target_date", "remind_at", "remind_in_minutes", "cancel_reminder"):
            self.assertIn(name, props)
        self.assertEqual(props["remind_in_minutes"]["type"], "integer")
        self.assertEqual(props["cancel_reminder"]["type"], "boolean")

    def test_create_publishes_the_reminder_arguments(self):
        self._key(scope=SCOPE_WRITE)
        props = self._listed()["create_todo_item"]["inputSchema"]["properties"]
        self.assertIn("remind_at", props)
        self.assertIn("remind_in_minutes", props)

    def test_nothing_published_promises_an_approval_card_this_surface_lacks(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        listed = self._listed()
        for name in ("create_todo_item", "update_todo_item", "delete_todo_item"):
            tool = listed[name]
            texts = [tool["description"]] + [
                p.get("description", "") for p in tool["inputSchema"]["properties"].values()
            ]
            for text in texts:
                self.assertNotIn("approv", text.lower(), f"{name}: {text}")
                self.assertNotIn("APPROVAL", text, f"{name}: {text}")

    def test_the_reads_say_a_reminder_is_shown(self):
        self._key(scope=SCOPE_READ)
        listed = self._listed()
        for name in ("list_today_todos", "list_uncompleted_todos"):
            self.assertIn("reminder_at", listed[name]["description"])


class TestDeleteNeedsTheDestructiveOptIn(McpTodoBase):
    def test_a_write_key_is_not_shown_the_delete(self):
        self._key(scope=SCOPE_WRITE)
        self.assertNotIn("delete_todo_item", self._listed())

    def test_a_write_key_is_refused_the_delete_it_asks_for_anyway(self):
        self._key(scope=SCOPE_WRITE)
        with patch(PURGE) as purged:
            text, is_error = self._text("delete_todo_item", {"item_id": self.item.item_id})
        self.assertTrue(is_error)
        self.assertIn("can't be undone", text)
        self.assertTrue(ToDoItem.objects.filter(pk=self.item.pk).exists())
        purged.assert_not_called()

    def test_an_opted_in_key_deletes_and_is_told_it_is_final(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        tool = self._listed()["delete_todo_item"]
        self.assertIs(tool["annotations"]["destructiveHint"], True)
        self.assertIn("not reversible", tool["description"])

        with patch(PURGE) as purged:
            text, is_error = self._text("delete_todo_item", {"item_id": self.item.item_id})

        self.assertFalse(is_error, text)
        self.assertIn("Deleted todo", text)
        self.assertFalse(ToDoItem.objects.filter(pk=self.item.pk).exists())
        purged.assert_called_once_with(self.item.item_id)

    def test_an_opted_in_read_key_still_cannot_delete(self):
        self._key(scope=SCOPE_READ, allow_destructive=True)
        self.assertNotIn("delete_todo_item", self._listed())
        text, is_error = self._text("delete_todo_item", {"item_id": self.item.item_id})
        self.assertTrue(is_error)
        self.assertTrue(ToDoItem.objects.filter(pk=self.item.pk).exists())

    def test_someone_elses_to_do_is_refused_even_with_the_opt_in(self):
        theirs = ToDoItem.objects.create(
            group=ToDoGroup.objects.create(team=self.team, user=self.user2, local_date=self.today),
            title="theirs",
        )
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        text, is_error = self._text("delete_todo_item", {"item_id": theirs.item_id})
        self.assertTrue(is_error)
        self.assertIn(f"Todo item {theirs.item_id} not found.", text)
        self.assertNotIn("Not authorized", text)
        self.assertTrue(ToDoItem.objects.filter(pk=theirs.pk).exists())


class TestRescheduleAndRemindOverMcp(McpTodoBase):
    def test_a_to_do_is_moved_and_reminded_in_one_call(self):
        self._key(scope=SCOPE_WRITE)
        friday = self.today + timedelta(days=4)
        with patch(PURGE):
            text, is_error = self._text(
                "update_todo_item",
                {
                    "item_id": self.item.item_id,
                    "target_date": friday.isoformat(),
                    "remind_at": f"{friday.isoformat()}T08:15",
                },
            )
        self.assertFalse(is_error, text)
        self.item.refresh_from_db()
        self.assertEqual(self.item.group.local_date, friday)
        self.assertEqual(
            self._pending().get().remind_at.astimezone(TOKYO).strftime("%Y-%m-%d %H:%M"),
            f"{friday.isoformat()} 08:15",
        )
        self.assertIn("+09:00", text)

    def test_the_reminder_can_be_cancelled(self):
        self._key(scope=SCOPE_WRITE)
        todo_reminders.set_reminder(item=self.item, remind_at=timezone.now() + timedelta(hours=3))
        text, is_error = self._text(
            "update_todo_item", {"item_id": self.item.item_id, "cancel_reminder": True}
        )
        self.assertFalse(is_error, text)
        self.assertEqual(self._pending().count(), 0)

    def test_a_time_in_the_past_comes_back_as_an_error_that_says_what_time_it_is(self):
        self._key(scope=SCOPE_WRITE)
        yesterday = self.today - timedelta(days=1)
        text, is_error = self._text(
            "update_todo_item",
            {"item_id": self.item.item_id, "remind_at": f"{yesterday.isoformat()}T09:00"},
        )
        self.assertTrue(is_error)
        self.assertIn("already passed", text)
        self.assertIn("It is now", text)
        self.assertEqual(self._pending().count(), 0)

    def test_the_reads_show_the_pending_reminder(self):
        self._key(scope=SCOPE_READ)
        at = timezone.now() + timedelta(hours=3)
        todo_reminders.set_reminder(item=self.item, remind_at=at)
        for name in ("list_today_todos", "list_uncompleted_todos"):
            text, is_error = self._text(name, {})
            self.assertFalse(is_error, text)
            self.assertIn(at.astimezone(TOKYO).isoformat(timespec="seconds"), text)

    def test_create_with_a_reminder_over_mcp(self):
        self._key(scope=SCOPE_WRITE)
        friday = self.today + timedelta(days=4)
        text, is_error = self._text(
            "create_todo_item",
            {
                "title": "Follow up with Sam",
                "target_date": friday.isoformat(),
                "remind_at": friday.isoformat(),
            },
        )
        self.assertFalse(is_error, text)
        created = ToDoItem.objects.get(title="Follow up with Sam")
        self.assertEqual(created.group.local_date, friday)
        self.assertEqual(self._pending(created).get().remind_at.astimezone(TOKYO).hour, 9)


class TestPublishedDescriptions(McpTodoBase):
    """MIP-909: the text a client reads says what the code does."""

    def test_the_backlog_says_oldest_first_and_which_end_is_cut(self):
        self._key(scope=SCOPE_READ)
        text = self._listed()["list_uncompleted_todos"]["description"]
        self.assertIn("oldest day first", text)
        self.assertNotIn("newest first", text)
        self.assertIn("truncated", text)

    def test_create_says_whose_today_it_defaults_to_and_that_a_category_is_a_name(self):
        self._key(scope=SCOPE_WRITE)
        text = self._listed()["create_todo_item"]["description"]
        self.assertIn("own today", text)
        self.assertIn("category", text)
        schema = self._listed()["create_todo_item"]["inputSchema"]["properties"]["target_date"]
        self.assertNotIn("server-local", schema["description"])

    def test_update_says_a_subitem_takes_its_parents_category(self):
        self._key(scope=SCOPE_WRITE)
        text = self._listed()["update_todo_item"]["description"]
        self.assertIn("take their parent's", text)
        self.assertIn("refused", text)
