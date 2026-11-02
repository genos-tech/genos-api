"""The agent can reschedule, remind about and delete a to-do (MIP-884).

Before this, `update_todo_item` could not change a to-do's day, no tool could
set a reminder although people can, and nothing deleted a to-do. The three
have to leave exactly what the UI's own endpoints leave, or a to-do is "on
Friday" for one surface and not another, or a reminder fires at the wrong
moment, or a delete leaves a stale search chunk behind. So the core tests
drive the same change through the REST endpoint and through the tool on twin
rows and compare where each ends up.

The twins are separate rows: each sits on its own source day, so moving one
empties its own day and the two days can be compared afterwards.

Where the tool departs from the endpoint on purpose, a test pins both sides:

* a `remind_at` with no offset is a time in the user's zone (the endpoint
  refuses it: the browser always sends an instant);
* a time that has already passed is refused outright (the endpoint allows two
  minutes of slack for a browser clock that is behind the server's).
"""

from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.todo_models import ToDoGroup, ToDoItem, ToDoItemReminder
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.create_todo_item import CREATE_TODO_ITEM
from origin.search_engine.agent.tools.delete_todo_item import DELETE_TODO_ITEM
from origin.search_engine.agent.tools.list_today_todos import LIST_TODAY_TODOS
from origin.search_engine.agent.tools.list_uncompleted_todos import LIST_UNCOMPLETED_TODOS
from origin.search_engine.agent.tools.update_todo_item import UPDATE_TODO_ITEM
from origin.services import todo_reminders

from .test_base import BaseAPITestCase

TOKYO = ZoneInfo("Asia/Tokyo")
PURGE = "origin.services.todo_items.purge_todo_item"


class TodoToolBase(BaseAPITestCase):
    """A Tokyo user with a few days of to-dos, and the REST client for the
    twin side. Dates are relative to the real clock so they are always in
    the future or the past they claim to be."""

    def setUp(self):
        super().setUp()
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.today = timezone.now().astimezone(TOKYO).date()
        self.authenticate()

    # ---- fixtures ------------------------------------------------------

    def day(self, offset: int) -> date:
        return self.today + timedelta(days=offset)

    def group(self, local_date: date, *, user=None, team=None) -> ToDoGroup:
        group, _ = ToDoGroup.objects.get_or_create(
            team=team or self.team, user=user or self.user, local_date=local_date
        )
        return group

    def todo(self, title="Call the lawyer", on=None, *, parent=None, user=None, team=None, **kw):
        on = on if on is not None else self.day(0)
        return ToDoItem.objects.create(
            group=self.group(on, user=user, team=team), title=title, parent_item=parent, **kw
        )

    def pending(self, item):
        return ToDoItemReminder.objects.filter(
            item=item, fired_at__isnull=True, cancelled_at__isnull=True
        )

    def remind(self, item, hours=5):
        return todo_reminders.set_reminder(
            item=item, remind_at=timezone.now() + timedelta(hours=hours)
        )

    # ---- the two sides -------------------------------------------------

    def update(self, item, **args):
        return UPDATE_TODO_ITEM.run({"item_id": item.item_id, **args}, self.ctx)

    def ui_patch(self, item, expect=200, **body):
        res = self.client.patch(f"/api/v2/todo/items/{item.item_id}/", body, format="json")
        self.assertEqual(res.status_code, expect, res.data)
        return res

    def ui_remind(self, item, at, expect=201):
        res = self.client.post(
            f"/api/v2/todo/items/{item.item_id}/reminder/",
            {"remindAt": at.isoformat()},
            format="json",
        )
        self.assertEqual(res.status_code, expect, res.data)
        return res

    def local_at(self, offset_days: int, hour: int = 9) -> datetime:
        """`hour` o'clock Tokyo time, `offset_days` from today, as an instant."""
        return datetime.combine(self.day(offset_days), datetime.min.time(), tzinfo=TOKYO).replace(
            hour=hour
        )


def place(item):
    """Everything that says where a to-do sits."""
    item.refresh_from_db()
    return (item.group.local_date, item.group.team_id, item.group.user_id, item.is_completed)


# --------------------------------------------------------------------------
# rescheduling
# --------------------------------------------------------------------------


class RescheduleTests(TodoToolBase):
    def test_a_move_lands_where_the_uis_patch_lands_it(self):
        """Same move, same target day, twin rows each alone on its own day:
        the row, its sub-item, both source days' completion, the target day
        and the search chunks that get dropped all come out the same."""
        by_tool, by_ui = self.todo("by tool", self.day(-3)), self.todo("by ui", self.day(-2))
        kid_tool = self.todo("kid", self.day(-3), parent=by_tool)
        kid_ui = self.todo("kid", self.day(-2), parent=by_ui)
        target = self.day(1)

        with patch(PURGE) as purged_by_tool:
            out = self.update(by_tool, target_date=target.isoformat())
        with patch(PURGE) as purged_by_ui:
            self.ui_patch(by_ui, local_date=target.isoformat())

        self.assertEqual(place(by_tool), place(by_ui))
        self.assertEqual(place(by_tool)[0], target)
        self.assertEqual(place(kid_tool), place(kid_ui))
        self.assertEqual(place(kid_tool)[0], target, "the sub-item follows its parent")
        self.assertEqual(out["local_date"], target.isoformat())
        # The row and its child leave a chunk under the old date each.
        self.assertEqual(
            [c.args[0] for c in purged_by_tool.call_args_list],
            [by_tool.item_id, kid_tool.item_id],
        )
        self.assertEqual(
            [c.args[0] for c in purged_by_ui.call_args_list], [by_ui.item_id, kid_ui.item_id]
        )
        # Each source day was emptied, so each is now a completed day.
        for source in (self.day(-3), self.day(-2)):
            self.assertTrue(self.group(source).is_completed, source)
        # Exactly one group was made for the target, by whichever came first.
        self.assertEqual(ToDoGroup.objects.filter(user=self.user, local_date=target).count(), 1)

    def test_the_moved_children_are_marked_updated_for_the_reindex(self):
        parent = self.todo("parent", self.day(-1))
        kid = self.todo("kid", self.day(-1), parent=parent)
        ToDoItem.objects.filter(pk=kid.pk).update(ts_updated_at=timezone.now() - timedelta(days=3))
        before = timezone.now()

        with patch(PURGE):
            self.update(parent, target_date=self.day(2).isoformat())

        kid.refresh_from_db()
        self.assertGreaterEqual(kid.ts_updated_at, before)

    def test_the_day_a_to_do_left_is_recomputed(self):
        """Another open to-do stays behind, so that day stays open; and an
        open to-do moved into a day that was all done reopens it."""
        leaving, staying = self.todo("leaving", self.day(-1)), self.todo("staying", self.day(-1))
        done_day = self.group(self.day(1))
        ToDoItem.objects.create(group=done_day, title="done", is_completed=True)
        ToDoGroup.objects.filter(pk=done_day.pk).update(is_completed=True)

        with patch(PURGE):
            self.update(leaving, target_date=self.day(1).isoformat())

        self.assertFalse(self.group(self.day(-1)).is_completed)
        done_day.refresh_from_db()
        self.assertFalse(done_day.is_completed, "an open to-do reopens a finished day")
        self.assertEqual(place(staying)[0], self.day(-1))

    def test_moving_into_an_existing_day_reuses_it(self):
        existing = self.group(self.day(2))
        item = self.todo("x", self.day(0))
        with patch(PURGE):
            self.update(item, target_date=self.day(2).isoformat())
        item.refresh_from_db()
        self.assertEqual(item.group_id, existing.group_id)

    def test_the_same_day_is_a_no_op(self):
        item = self.todo("x", self.day(0))
        with patch(PURGE) as purged:
            out = self.update(item, target_date=self.day(0).isoformat())
        purged.assert_not_called()
        self.assertEqual(out["local_date"], self.day(0).isoformat())

    def test_moving_a_subitem_alone_is_refused_as_the_ui_refuses_it(self):
        parent = self.todo("parent", self.day(0))
        kid_tool = self.todo("kid", self.day(0), parent=parent)
        kid_ui = self.todo("kid", self.day(0), parent=parent)

        with self.assertRaises(ToolError) as caught:
            self.update(kid_tool, target_date=self.day(3).isoformat())
        res = self.ui_patch(kid_ui, expect=400, local_date=self.day(3).isoformat())

        self.assertEqual(str(caught.exception), res.data["error"])
        self.assertEqual(place(kid_tool)[0], self.day(0))
        self.assertEqual(ToDoGroup.objects.filter(local_date=self.day(3)).count(), 0)

    def test_a_malformed_date_changes_nothing(self):
        item = self.todo("x", self.day(0))
        with self.assertRaises(ToolError) as caught:
            self.update(item, target_date="next friday", title="renamed")
        self.assertIn("YYYY-MM-DD", str(caught.exception))
        item.refresh_from_db()
        self.assertEqual(item.title, "x")
        self.assertEqual(place(item)[0], self.day(0))

    def test_a_move_rides_along_with_a_content_edit(self):
        item = self.todo("old title", self.day(0))
        with patch(PURGE):
            out = self.update(item, target_date=self.day(1).isoformat(), title="new title")
        item.refresh_from_db()
        self.assertEqual((item.title, item.group.local_date), ("new title", self.day(1)))
        self.assertIn("moved to", out["__summary__"])

    def test_a_pending_reminder_survives_a_move_and_fires_at_the_new_day(self):
        """The UI doesn't touch the reminder when a to-do moves, and the
        deep link is built when it fires, so it points at the new day."""
        item = self.todo("x", self.day(0))
        reminder = self.remind(item)
        with patch(PURGE):
            self.update(item, target_date=self.day(2).isoformat())

        self.assertEqual(list(self.pending(item).values_list("id", flat=True)), [reminder.id])
        with (
            patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=6)),
            patch.object(todo_reminders, "schedule_push_for_reminder"),
        ):
            todo_reminders.fire_due()
        card = InboxItems.objects.get(receiver=self.user)
        self.assertIn(self.day(2).isoformat(), card.item_optionals["href"])


class TodoCompletionTests(TodoToolBase):
    def test_ticking_it_off_retires_the_reminder_as_the_ui_does(self):
        """The tool used to complete a to-do and leave its reminder to fire
        at someone who had already finished it."""
        by_tool, by_ui = self.todo("by tool"), self.todo("by ui")
        self.remind(by_tool)
        self.remind(by_ui)

        self.update(by_tool, is_completed=True)
        self.ui_patch(by_ui, is_completed=True)

        for item in (by_tool, by_ui):
            self.assertEqual(self.pending(item).count(), 0)
            self.assertEqual(ToDoItemReminder.objects.filter(item=item).count(), 1)
            item.refresh_from_db()
            self.assertTrue(item.is_completed)
            self.assertIsNotNone(item.ts_completed_at)

    def test_reopening_does_not_bring_the_reminder_back(self):
        item = self.todo()
        self.remind(item)
        self.update(item, is_completed=True)
        self.update(item, is_completed=False)
        self.assertEqual(self.pending(item).count(), 0)

    def test_ticking_off_the_last_open_item_completes_the_day(self):
        item = self.todo()
        self.update(item, is_completed=True)
        self.assertTrue(self.group(self.day(0)).is_completed)


# --------------------------------------------------------------------------
# reminders
# --------------------------------------------------------------------------


class SetReminderTests(TodoToolBase):
    def test_remind_at_with_an_offset_stores_the_instant_the_uis_post_stores(self):
        by_tool, by_ui = self.todo("by tool"), self.todo("by ui")
        at = timezone.now().astimezone(TOKYO).replace(microsecond=0) + timedelta(hours=26)

        out = self.update(by_tool, remind_at=at.isoformat())
        self.ui_remind(by_ui, at)

        self.assertEqual(self.pending(by_tool).get().remind_at, at)
        self.assertEqual(self.pending(by_tool).get().remind_at, self.pending(by_ui).get().remind_at)
        self.assertEqual(out["reminder_at"], at.isoformat())
        self.assertTrue(out["reminder_at"].endswith("+09:00"))

    def test_a_new_reminder_replaces_the_old_as_the_ui_does(self):
        by_tool, by_ui = self.todo("by tool"), self.todo("by ui")
        for item in (by_tool, by_ui):
            self.remind(item, hours=3)
        later = timezone.now() + timedelta(hours=30)

        self.update(by_tool, remind_at=later.isoformat())
        self.ui_remind(by_ui, later)

        for item in (by_tool, by_ui):
            self.assertEqual(self.pending(item).count(), 1)
            self.assertEqual(ToDoItemReminder.objects.filter(item=item).count(), 2)
            self.assertEqual(
                ToDoItemReminder.objects.filter(item=item, cancelled_at__isnull=False).count(), 1
            )

    def test_a_time_without_an_offset_is_the_users_local_time(self):
        item = self.todo()
        tomorrow = self.day(1)
        out = self.update(item, remind_at=f"{tomorrow.isoformat()}T09:30")
        expected = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, 30, tzinfo=TOKYO)
        self.assertEqual(self.pending(item).get().remind_at, expected)
        self.assertEqual(out["reminder_at"], "%sT09:30:00+09:00" % tomorrow.isoformat())
        self.assertEqual(out["reminder_time_zone"], "Asia/Tokyo")

    def test_a_user_with_no_timezone_gets_the_server_zone_and_is_told(self):
        self.user.timezone = None
        self.user.save(update_fields=["timezone"])
        item = self.todo()
        tomorrow = self.day(1)
        out = self.update(item, remind_at=f"{tomorrow.isoformat()}T09:00")
        self.assertEqual(
            self.pending(item).get().remind_at,
            datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(out["reminder_time_zone"], "UTC")
        self.assertTrue(out["reminder_at"].endswith("+00:00"))

    def test_a_bare_date_means_nine_in_the_morning(self):
        item = self.todo()
        self.update(item, remind_at=self.day(3).isoformat())
        self.assertEqual(self.pending(item).get().remind_at, self.local_at(3, hour=9))

    def test_minutes_are_counted_from_the_server_clock(self):
        item = self.todo()
        before = timezone.now()
        self.update(item, remind_in_minutes=90)
        after = timezone.now()
        at = self.pending(item).get().remind_at
        self.assertGreaterEqual(at, before + timedelta(minutes=90))
        self.assertLessEqual(at, after + timedelta(minutes=90))

    def test_cancelling_matches_the_uis_delete(self):
        by_tool, by_ui = self.todo("by tool"), self.todo("by ui")
        for item in (by_tool, by_ui):
            self.remind(item)

        out = self.update(by_tool, cancel_reminder=True)
        res = self.client.delete(f"/api/v2/todo/items/{by_ui.item_id}/reminder/")
        self.assertEqual(res.status_code, 204)

        for item in (by_tool, by_ui):
            self.assertEqual(self.pending(item).count(), 0)
            self.assertEqual(
                ToDoItemReminder.objects.filter(item=item, cancelled_at__isnull=False).count(), 1
            )
        self.assertIsNone(out["reminder_at"])

    def test_cancelling_when_there_is_none_is_not_an_error(self):
        item = self.todo()
        out = self.update(item, cancel_reminder=True)
        res = self.client.delete(f"/api/v2/todo/items/{item.item_id}/reminder/")
        self.assertEqual(res.status_code, 204)
        self.assertIsNone(out["reminder_at"])

    # ---- what is refused -------------------------------------------------

    def _refused(self, item, fragment, **args):
        """The call is refused with a message saying why, and a valid edit
        carried in the same call is not applied."""
        with self.assertRaises(ToolError) as caught:
            self.update(item, title="renamed", target_date=self.day(4).isoformat(), **args)
        self.assertIn(fragment, str(caught.exception))
        item.refresh_from_db()
        self.assertEqual(item.title, "Call the lawyer")
        self.assertEqual(place(item)[0], self.day(0))
        self.assertEqual(ToDoGroup.objects.filter(local_date=self.day(4)).count(), 0)

    def test_a_time_that_has_passed_is_refused_and_says_what_time_it_is(self):
        item = self.todo()
        for past in (f"{self.day(-1).isoformat()}T09:00", self.day(-2).isoformat()):
            self._refused(item, "already passed", remind_at=past)
        # The error carries the clock the model does not have.
        with self.assertRaises(ToolError) as caught:
            self.update(item, remind_at=f"{self.day(-1).isoformat()}T09:00")
        self.assertIn("It is now", str(caught.exception))
        self.assertIn("Asia/Tokyo", str(caught.exception))

    def test_a_minute_ago_is_refused_though_the_endpoint_would_allow_it(self):
        """The endpoint forgives two minutes for a browser clock that is a
        little behind. The server's own clock has no such excuse."""
        item = self.todo()
        a_minute_ago = timezone.now() - timedelta(minutes=1)
        self._refused(item, "already passed", remind_at=a_minute_ago.isoformat())
        self.ui_remind(item, a_minute_ago)

    def test_a_time_more_than_a_year_out_is_refused_as_the_ui_refuses_it(self):
        item = self.todo()
        far = timezone.now() + timedelta(days=400)
        self._refused(item, "within a year", remind_at=far.isoformat())
        res = self.ui_remind(item, far, expect=400)
        self.assertIn("within a year", res.data["error"])
        self._refused(item, "at most", remind_in_minutes=600_000)

    def test_a_number_of_minutes_that_is_not_a_positive_whole_number_is_refused(self):
        item = self.todo()
        for bad in (0, -5, 1.5, True, "soon", float("nan")):
            self._refused(item, "remind_in_minutes", remind_in_minutes=bad)

    def test_text_that_is_not_a_time_is_refused(self):
        self._refused(self.todo(), "remind_at", remind_at="tomorrow morning")

    def test_both_ways_of_asking_at_once_is_refused(self):
        self._refused(
            self.todo(), "not both", remind_at=self.day(2).isoformat(), remind_in_minutes=5
        )

    def test_cancelling_and_setting_in_one_call_is_refused_and_keeps_the_old_one(self):
        item = self.todo()
        old = self.remind(item)
        self._refused(item, "can't be combined", cancel_reminder=True, remind_in_minutes=30)
        self.assertEqual([r.id for r in self.pending(item)], [old.id])

    def test_a_completed_to_do_takes_no_reminder_as_the_ui_refuses(self):
        done = self.todo(is_completed=True)
        with self.assertRaises(ToolError) as caught:
            self.update(done, remind_in_minutes=30)
        res = self.ui_remind(done, timezone.now() + timedelta(hours=1), expect=400)
        self.assertIn(res.data["error"], str(caught.exception))
        self.assertEqual(self.pending(done).count(), 0)

    def test_ticking_off_and_reminding_in_one_call_is_refused(self):
        item = self.todo()
        with self.assertRaises(ToolError):
            self.update(item, is_completed=True, remind_in_minutes=30)
        item.refresh_from_db()
        self.assertFalse(item.is_completed)

    def test_reopening_and_reminding_in_one_call_is_allowed(self):
        done = self.todo(is_completed=True)
        self.update(done, is_completed=False, remind_in_minutes=30)
        done.refresh_from_db()
        self.assertFalse(done.is_completed)
        self.assertEqual(self.pending(done).count(), 1)

    def test_a_reminder_can_ride_along_with_a_move(self):
        item = self.todo()
        with patch(PURGE):
            self.update(
                item, target_date=self.day(5).isoformat(), remind_at=self.day(5).isoformat()
            )
        self.assertEqual(self.pending(item).get().remind_at, self.local_at(5, 9))
        self.assertEqual(place(item)[0], self.day(5))


class CreateWithReminderTests(TodoToolBase):
    def create(self, **args):
        return CREATE_TODO_ITEM.run({"title": "Follow up with Sam", **args}, self.ctx)

    def test_remind_me_friday_is_one_call(self):
        friday = self.day(4)
        out = self.create(target_date=friday.isoformat(), remind_at=friday.isoformat())

        item = ToDoItem.objects.get(item_id=out["item_id"])
        self.assertEqual(item.group.local_date, friday)
        self.assertEqual(self.pending(item).get().remind_at, self.local_at(4, 9))
        self.assertEqual(out["reminder_at"], self.local_at(4, 9).isoformat())
        self.assertIn("reminder set", out["__summary__"])

    def test_it_matches_creating_through_the_ui_and_then_setting_the_reminder(self):
        friday = self.day(4)
        at = self.local_at(4, 9)
        out = self.create(target_date=friday.isoformat(), remind_at=at.isoformat())
        res = self.client.post(
            "/api/v2/todo/items/",
            {
                "team_id": str(self.team.team_id),
                "local_date": friday.isoformat(),
                "title": "Follow up with Sam",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        twin = ToDoItem.objects.get(item_id=res.data["itemId"])
        self.ui_remind(twin, at)

        by_tool = ToDoItem.objects.get(item_id=out["item_id"])
        self.assertEqual(place(by_tool), place(twin))
        self.assertEqual(self.pending(by_tool).get().remind_at, self.pending(twin).get().remind_at)

    def test_without_a_reminder_the_result_is_what_it_was(self):
        out = self.create()
        self.assertNotIn("reminder_at", out)
        self.assertEqual(ToDoItemReminder.objects.count(), 0)

    def test_a_refused_reminder_leaves_no_to_do_behind(self):
        before = (ToDoItem.objects.count(), ToDoGroup.objects.count())
        for bad in (
            {"remind_at": f"{self.day(-1).isoformat()}T09:00"},
            {"remind_in_minutes": 0},
            {"remind_at": self.day(2).isoformat(), "remind_in_minutes": 5},
        ):
            with self.assertRaises(ToolError):
                self.create(target_date=self.day(9).isoformat(), **bad)
        self.assertEqual((ToDoItem.objects.count(), ToDoGroup.objects.count()), before)


class ReadsShowReminders(TodoToolBase):
    def test_both_reads_carry_reminder_at_in_the_users_zone(self):
        reminded = self.todo("reminded", self.day(0))
        plain = self.todo("plain", self.day(0))
        past = self.todo("older", self.day(-2))
        at = self.remind(reminded, hours=30).remind_at
        self.remind(past, hours=2)

        today = {i["item_id"]: i for i in LIST_TODAY_TODOS.run({}, self.ctx)["items"]}
        self.assertEqual(
            today[reminded.item_id]["reminder_at"],
            at.astimezone(TOKYO).isoformat(timespec="seconds"),
        )
        self.assertTrue(today[reminded.item_id]["reminder_at"].endswith("+09:00"))
        self.assertNotIn("reminder_at", today[plain.item_id])

        backlog = {i["item_id"]: i for i in LIST_UNCOMPLETED_TODOS.run({}, self.ctx)["items"]}
        self.assertIn("reminder_at", backlog[reminded.item_id])
        self.assertIn("reminder_at", backlog[past.item_id])
        self.assertNotIn("reminder_at", backlog[plain.item_id])

    def test_a_cancelled_or_fired_reminder_is_not_shown(self):
        cancelled, fired = self.todo("cancelled"), self.todo("fired")
        self.remind(cancelled)
        todo_reminders.cancel_pending(item=cancelled)
        ToDoItemReminder.objects.create(
            item=fired, remind_at=timezone.now() - timedelta(hours=1), fired_at=timezone.now()
        )
        rows = {i["item_id"]: i for i in LIST_TODAY_TODOS.run({}, self.ctx)["items"]}
        self.assertNotIn("reminder_at", rows[cancelled.item_id])
        self.assertNotIn("reminder_at", rows[fired.item_id])

    def test_the_reads_do_not_query_per_item(self):
        def queries(read):
            with CaptureQueriesContext(connection) as ctx:
                read.run({}, self.ctx)
            return len(ctx)

        self.remind(self.todo("first"))
        one = (queries(LIST_TODAY_TODOS), queries(LIST_UNCOMPLETED_TODOS))
        for n in range(6):
            self.remind(self.todo(f"t{n}"))
        self.assertEqual((queries(LIST_TODAY_TODOS), queries(LIST_UNCOMPLETED_TODOS)), one)


# --------------------------------------------------------------------------
# deleting
# --------------------------------------------------------------------------


class DeleteTests(TodoToolBase):
    def delete(self, item):
        return DELETE_TODO_ITEM.run({"item_id": item.item_id}, self.ctx)

    def ui_delete(self, item, expect=204):
        res = self.client.delete(f"/api/v2/todo/items/{item.item_id}/")
        self.assertEqual(res.status_code, expect)

    def test_a_delete_leaves_what_the_uis_delete_leaves(self):
        """Row, sub-items and every reminder gone, the day recomputed, the
        search chunk purged. The twin days each held one open to-do, so
        deleting it completes the day."""
        by_tool, by_ui = self.todo("by tool", self.day(-2)), self.todo("by ui", self.day(-1))
        kids = [
            self.todo("kid", self.day(-2), parent=by_tool),
            self.todo("kid", self.day(-1), parent=by_ui),
        ]
        for item in (by_tool, by_ui, *kids):
            self.remind(item)
        ids = {"tool": (by_tool.item_id, kids[0].item_id), "ui": (by_ui.item_id, kids[1].item_id)}

        with patch(PURGE) as tool_purge:
            out = self.delete(by_tool)
        with patch(PURGE) as ui_purge:
            self.ui_delete(by_ui)

        for side in ids.values():
            self.assertFalse(ToDoItem.objects.filter(item_id__in=side).exists())
            self.assertFalse(ToDoItemReminder.objects.filter(item_id__in=side).exists())
        self.assertEqual([c.args[0] for c in tool_purge.call_args_list], [ids["tool"][0]])
        self.assertEqual([c.args[0] for c in ui_purge.call_args_list], [ids["ui"][0]])
        self.assertTrue(self.group(self.day(-2)).is_completed)
        self.assertTrue(self.group(self.day(-1)).is_completed)
        self.assertTrue(out["deleted"])
        self.assertEqual(out["deleted_sub_items"], 1)

    def test_deleting_one_of_two_open_items_leaves_the_day_open(self):
        goes, stays = self.todo("goes"), self.todo("stays")
        self.delete(goes)
        self.assertFalse(self.group(self.day(0)).is_completed)
        self.assertTrue(ToDoItem.objects.filter(pk=stays.pk).exists())

    def test_deleting_the_only_open_item_completes_a_day_with_done_ones(self):
        self.todo("done", is_completed=True)
        open_one = self.todo("open")
        self.delete(open_one)
        self.assertTrue(self.group(self.day(0)).is_completed)

    def test_deleting_a_subitem_leaves_its_parent(self):
        parent = self.todo("parent")
        kid = self.todo("kid", parent=parent)
        out = self.delete(kid)
        self.assertTrue(ToDoItem.objects.filter(pk=parent.pk).exists())
        self.assertEqual(out["deleted_sub_items"], 0)

    def test_the_result_gives_the_model_nothing_to_link_to(self):
        item = self.todo()
        out = self.delete(item)
        self.assertNotIn("local_date", out)
        self.assertEqual(controller._ui_sources_from_tool_result("delete_todo_item", out), [])

    # ---- whose to-do it is -------------------------------------------------

    def test_another_users_to_do_is_refused_and_untouched(self):
        theirs = self.todo("theirs", user=self.user2)
        self.remind(theirs)
        with self.assertRaises(ToolForbidden):
            self.delete(theirs)
        self.ui_delete(theirs, expect=403)
        self.assertTrue(ToDoItem.objects.filter(pk=theirs.pk).exists())
        self.assertEqual(self.pending(theirs).count(), 1)

    def test_a_to_do_in_another_team_is_refused_and_untouched(self):
        other = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        elsewhere = self.todo("elsewhere", team=other)
        with self.assertRaises(ToolForbidden):
            self.delete(elsewhere)
        self.assertTrue(ToDoItem.objects.filter(pk=elsewhere.pk).exists())

    def test_a_missing_or_malformed_id_is_a_plain_error(self):
        with self.assertRaises(ToolError) as caught:
            DELETE_TODO_ITEM.run({"item_id": 999_999}, self.ctx)
        self.assertNotIsInstance(caught.exception, ToolForbidden)
        with self.assertRaises(ToolError):
            DELETE_TODO_ITEM.run({"item_id": "abc"}, self.ctx)
        with self.assertRaises(ToolError):
            DELETE_TODO_ITEM.run({}, self.ctx)


class WhoMayChangeATodo(TodoToolBase):
    """The same owner and team guard sits in front of every change."""

    def test_another_users_to_do_cannot_be_moved_or_reminded(self):
        theirs = self.todo("theirs", self.day(0), user=self.user2)
        for args in (
            {"target_date": self.day(3).isoformat()},
            {"remind_in_minutes": 30},
            {"cancel_reminder": True},
            {"title": "mine now"},
        ):
            with self.assertRaises(ToolForbidden, msg=args):
                self.update(theirs, **args)
        theirs.refresh_from_db()
        self.assertEqual((theirs.title, theirs.group.local_date), ("theirs", self.day(0)))
        self.assertEqual(self.pending(theirs).count(), 0)
        # The UI agrees.
        self.ui_patch(theirs, expect=403, local_date=self.day(3).isoformat())
        self.ui_remind(theirs, timezone.now() + timedelta(hours=1), expect=403)

    def test_a_to_do_in_another_team_cannot_be_moved(self):
        other = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        elsewhere = self.todo("elsewhere", self.day(0), team=other)
        with self.assertRaises(ToolForbidden):
            self.update(elsewhere, target_date=self.day(3).isoformat())
        self.assertEqual(place(elsewhere)[0], self.day(0))

    def test_someone_elses_to_do_looks_exactly_like_a_missing_one(self):
        """A caller probing ids can't tell which exist: another user's to-do,
        another team's, and one that isn't there all get the same words, from
        the update and the delete alike. The first two are still a
        `ToolForbidden` so the MCP refusal-burst alert counts them."""
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user)
        theirs = self.todo("theirs", user=self.user2)
        elsewhere = self.todo("elsewhere", team=other_team)
        missing_id = ToDoItem.objects.order_by("-item_id").first().item_id + 1000

        def refusal(run, item_id):
            with self.assertRaises(ToolError) as caught:
                run({"item_id": item_id}, self.ctx)
            return caught.exception

        for run in (UPDATE_TODO_ITEM.run, DELETE_TODO_ITEM.run):
            gone = refusal(run, missing_id)
            self.assertEqual(str(gone), f"Todo item {missing_id} not found.")
            for item in (theirs, elsewhere):
                refused = refusal(run, item.item_id)
                self.assertIsInstance(refused, ToolForbidden)
                self.assertEqual(str(refused), f"Todo item {item.item_id} not found.")
                self.assertEqual(
                    str(refused).replace(str(item.item_id), "N"),
                    str(gone).replace(str(missing_id), "N"),
                )
        self.assertNotIsInstance(refusal(UPDATE_TODO_ITEM.run, missing_id), ToolForbidden)

    def test_an_edit_with_no_new_arguments_still_works(self):
        item = self.todo("x")
        out = self.update(item, title="y", is_completed=True, category="Calls")
        self.assertEqual((out["title"], out["is_completed"], out["category"]), ("y", True, "Calls"))
        self.assertNotIn("reminder_time_zone", out)
