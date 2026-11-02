"""What the approval card says about a to-do being moved, reminded or deleted (MIP-884).

The card shows the model's proposed arguments. For a move or a reminder that
is only half a sentence: "Friday" means little without the day it is on now,
and "09:00" means little without the zone it will be read in. So the card
carries the to-do's present value under `current` (old -> new, the shape
`update_task` uses), and for a delete it carries what else goes with the row.
As everywhere on the card, a value is printed only for a to-do on the
caller's own list in the caller's team.
"""

from datetime import date, timedelta

from django.utils import timezone

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.services import todo_reminders

from .test_agent_approval_card_label_acl import ApprovalCardAclBase


class TodoCardBase(ApprovalCardAclBase):
    def setUp(self):
        super().setUp()
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        self.group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=date(2026, 11, 1)
        )
        self.item = ToDoItem.objects.create(group=self.group, title="Call the lawyer")

    def card(self, tool, user=None, team=None, **args):
        return self._card({"item_id": self.item.item_id, **args}, tool, user or self.user, team)


class UpdateTodoCardTests(TodoCardBase):
    def test_a_move_shows_the_day_it_is_on_now(self):
        out = self.card("update_todo_item", target_date="2026-11-06")
        self.assertEqual(out["item_id"], "Call the lawyer")
        self.assertEqual(out["target_date"], "2026-11-06")
        self.assertEqual(out["current"], {"target_date": "2026-11-01"})

    def test_a_reminder_shows_the_present_one_in_the_users_zone_and_the_zone(self):
        at = timezone.now() + timedelta(hours=5)
        todo_reminders.set_reminder(item=self.item, remind_at=at)

        out = self.card("update_todo_item", remind_at="2026-11-06T09:00")

        self.assertEqual(
            out["current"],
            {
                "reminder_at": at.astimezone(timezone.get_fixed_timezone(9 * 60)).isoformat(
                    timespec="seconds"
                )
            },
        )
        self.assertEqual(out["time_zone"], "Asia/Tokyo")

    def test_no_reminder_now_is_none_not_missing(self):
        out = self.card("update_todo_item", remind_in_minutes=30)
        self.assertEqual(out["current"], {"reminder_at": None})
        self.assertNotIn("time_zone", out, "minutes need no zone")

    def test_cancelling_shows_what_is_being_cancelled(self):
        todo_reminders.set_reminder(item=self.item, remind_at=timezone.now() + timedelta(hours=5))
        out = self.card("update_todo_item", cancel_reminder=True)
        self.assertIsNotNone(out["current"]["reminder_at"])

    def test_a_plain_edit_adds_nothing(self):
        out = self.card("update_todo_item", title="Call the other lawyer")
        self.assertNotIn("current", out)
        self.assertNotIn("time_zone", out)

    def test_a_teammate_sees_neither_the_title_nor_the_snapshot(self):
        todo_reminders.set_reminder(item=self.item, remind_at=timezone.now() + timedelta(hours=5))
        out = self.card(
            "update_todo_item", user=self.user2, target_date="2026-11-06", remind_at="2026-11-07"
        )
        self.assertEqual(out["item_id"], self.item.item_id)
        self.assertNotIn("current", out)
        self.assertNotIn("time_zone", out)

    def test_another_team_sees_neither(self):
        out = self.card("update_todo_item", team=self.other_team, target_date="2026-11-06")
        self.assertEqual(out["item_id"], self.item.item_id)
        self.assertNotIn("current", out)


class CreateTodoCardTests(TodoCardBase):
    def test_a_reminder_names_the_zone_it_will_be_read_in(self):
        out = self._card({"title": "x", "remind_at": "2026-11-06"}, "create_todo_item", self.user)
        self.assertEqual(out["time_zone"], "Asia/Tokyo")

    def test_without_a_reminder_nothing_is_added(self):
        out = self._card({"title": "x"}, "create_todo_item", self.user)
        self.assertNotIn("time_zone", out)


class DeleteTodoCardTests(TodoCardBase):
    def test_the_card_names_the_to_do_and_what_goes_with_it(self):
        ToDoItem.objects.create(group=self.group, title="kid", parent_item=self.item)
        ToDoItem.objects.create(group=self.group, title="kid 2", parent_item=self.item)
        at = timezone.now() + timedelta(hours=5)
        todo_reminders.set_reminder(item=self.item, remind_at=at)

        out = self.card("delete_todo_item")

        self.assertEqual(out["item_id"], "Call the lawyer")
        self.assertEqual(out["current"]["target_date"], "2026-11-01")
        self.assertEqual(out["current"]["sub_items"], 2)
        self.assertTrue(out["current"]["reminder_at"].endswith("+09:00"))

    def test_a_to_do_with_nothing_attached(self):
        out = self.card("delete_todo_item")
        self.assertEqual(out["current"]["sub_items"], 0)
        self.assertIsNone(out["current"]["reminder_at"])

    def test_a_teammate_gets_the_raw_id_and_no_snapshot(self):
        ToDoItem.objects.create(group=self.group, title="kid", parent_item=self.item)
        out = self.card("delete_todo_item", user=self.user2)
        self.assertEqual(out["item_id"], self.item.item_id)
        self.assertNotIn("current", out)

    def test_another_team_gets_the_raw_id_and_no_snapshot(self):
        out = self.card("delete_todo_item", team=self.other_team)
        self.assertEqual(out["item_id"], self.item.item_id)
        self.assertNotIn("current", out)
