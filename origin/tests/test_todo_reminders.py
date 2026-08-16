"""To-do reminders — "remind me about this to-do at 3pm".

Four things are worth pinning, and each of them is a way the feature can
lie to the user rather than merely break:

* `ToDoItemReminderViewTests` — the endpoints, and the acceptance window.
  A reminder the server silently stores at the wrong instant, or refuses
  without saying why, is worse than no reminder.
* `ToDoReminderCompletionTests` — ticking the to-do off retires the nudge
  (rule 1 in `services/todo_reminders`), and re-opening it does not bring a
  stale one back to fire immediately.
* `ToDoReminderTickTests` — what firing actually produces: an inbox
  Activity card with a to-do-shaped payload, a push with a per-reminder
  tag, and the deep link that makes the card actionable.
* `ToDoReminderTickCommandTests` — the shared cron drains BOTH kinds. This
  is the test that catches somebody "cleaning up" the todo half out of
  `message_reminder_tick`, which would leave setting reminders working and
  delivery silently dead.
"""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.todo_models import ToDoGroup, ToDoItem, ToDoItemReminder
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster
from origin.services import todo_reminders

User = get_user_model()


class _TodoReminderMixin(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="owner", email="owner@test.com", password="pw12345678"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")
        self.team = TeamMaster.objects.create(
            team_name="Test Team", team_email="team@test.com", owner=self.user
        )
        self.local_date = timezone.localdate()
        self.group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=self.local_date
        )
        self.item = ToDoItem.objects.create(group=self.group, title="ship the release notes")

    def _reminder_url(self, item=None):
        return reverse("todo_item_reminder", args=[(item or self.item).item_id])

    def _pending(self, item=None):
        return ToDoItemReminder.objects.filter(
            item=item or self.item, fired_at__isnull=True, cancelled_at__isnull=True
        )

    def _set(self, *, item=None, at=None):
        return self.client.post(
            self._reminder_url(item),
            {"remindAt": (at or (timezone.now() + timedelta(hours=2))).isoformat()},
            format="json",
        )


class ToDoItemReminderViewTests(_TodoReminderMixin):
    def test_setting_a_reminder_returns_it_and_stores_the_instant(self):
        at = timezone.now() + timedelta(hours=3)
        res = self._set(at=at)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["reminder"]["itemId"], self.item.item_id)
        stored = self._pending().get()
        # To the second: the client picked this instant, and drifting it is
        # the one failure the whole browser-computes-it design prevents.
        self.assertLess(abs((stored.remind_at - at).total_seconds()), 1)

    def test_setting_twice_is_a_change_of_mind_not_a_second_nudge(self):
        self._set(at=timezone.now() + timedelta(days=1))
        self._set(at=timezone.now() + timedelta(hours=1))
        self.assertEqual(self._pending().count(), 1)
        # The superseded row is kept, cancelled — what was asked for survives.
        self.assertEqual(ToDoItemReminder.objects.filter(item=self.item).count(), 2)

    def test_the_list_endpoint_returns_pending_soonest_first(self):
        later = ToDoItem.objects.create(group=self.group, title="later")
        self._set(at=timezone.now() + timedelta(days=2))
        self._set(item=later, at=timezone.now() + timedelta(hours=1))
        res = self.client.get(reverse("todo_reminders"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual([r["itemId"] for r in res.data["reminders"]], [later.item_id, self.item.item_id])

    def test_cancelling_is_idempotent(self):
        self._set()
        self.assertEqual(self.client.delete(self._reminder_url()).status_code, 204)
        self.assertFalse(self._pending().exists())
        # Again, with nothing pending — still 204, because the client drops
        # its own copy optimistically and must not be told it failed.
        self.assertEqual(self.client.delete(self._reminder_url()).status_code, 204)

    def test_a_past_instant_is_refused(self):
        res = self._set(at=timezone.now() - timedelta(hours=1))
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self._pending().exists())

    def test_a_slightly_stale_client_clock_is_tolerated(self):
        """A browser a few seconds behind the server must not turn "in 20
        minutes" into an error — it fires on the next tick anyway."""
        res = self._set(at=timezone.now() - timedelta(seconds=30))
        self.assertEqual(res.status_code, 201)

    def test_beyond_a_year_is_refused(self):
        self.assertEqual(self._set(at=timezone.now() + timedelta(days=400)).status_code, 400)

    def test_a_naive_instant_is_refused(self):
        res = self.client.post(
            self._reminder_url(), {"remindAt": "2030-01-01T09:00:00"}, format="json"
        )
        self.assertEqual(res.status_code, 400)

    def test_a_garbage_instant_is_refused(self):
        res = self.client.post(self._reminder_url(), {"remindAt": "soon"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_an_already_completed_todo_is_refused(self):
        """The UI hides the option on a completed row; anything that gets
        past that is asking for a promise `_retire_if_moot` would break."""
        self.item.is_completed = True
        self.item.save(update_fields=["is_completed"])
        self.assertEqual(self._set().status_code, 400)

    def test_someone_elses_todo_is_forbidden(self):
        stranger = User.objects.create_user(
            username="stranger", email="s@test.com", password="pw12345678"
        )
        their_group = ToDoGroup.objects.create(
            team=self.team, user=stranger, local_date=self.local_date
        )
        their_item = ToDoItem.objects.create(group=their_group, title="not yours")
        self.assertEqual(self._set(item=their_item).status_code, 403)
        self.assertEqual(self.client.delete(self._reminder_url(their_item)).status_code, 403)

    def test_the_list_is_only_my_own(self):
        stranger = User.objects.create_user(
            username="stranger2", email="s2@test.com", password="pw12345678"
        )
        their_group = ToDoGroup.objects.create(
            team=self.team, user=stranger, local_date=self.local_date
        )
        their_item = ToDoItem.objects.create(group=their_group, title="not yours")
        ToDoItemReminder.objects.create(
            item=their_item, remind_at=timezone.now() + timedelta(hours=1)
        )
        res = self.client.get(reverse("todo_reminders"))
        self.assertEqual(res.data["reminders"], [])

    def test_a_subitem_can_carry_its_own_reminder(self):
        """A step of a task is still a thing you can forget, so children are
        not gated out the way copy-link and create-task are."""
        child = ToDoItem.objects.create(
            group=self.group, parent_item=self.item, title="draft the changelog"
        )
        self.assertEqual(self._set(item=child).status_code, 201)


class ToDoReminderCompletionTests(_TodoReminderMixin):
    def setUp(self):
        super().setUp()
        self._set()
        self.detail_url = reverse("todo_item_detail", args=[self.item.item_id])

    def test_completing_the_todo_cancels_the_reminder(self):
        res = self.client.patch(self.detail_url, {"is_completed": True}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(self._pending().exists())

    def test_reopening_does_not_resurrect_it(self):
        self.client.patch(self.detail_url, {"is_completed": True}, format="json")
        self.client.patch(self.detail_url, {"is_completed": False}, format="json")
        self.assertFalse(self._pending().exists())

    def test_an_unrelated_patch_leaves_the_reminder_alone(self):
        self.client.patch(self.detail_url, {"title": "renamed"}, format="json")
        self.assertTrue(self._pending().exists())

    def test_deleting_the_todo_takes_the_reminder_with_it(self):
        self.assertEqual(self.client.delete(self.detail_url).status_code, 204)
        self.assertFalse(ToDoItemReminder.objects.filter(item_id=self.item.item_id).exists())

    def test_deleting_a_parent_takes_a_childs_reminder_too(self):
        child = ToDoItem.objects.create(
            group=self.group, parent_item=self.item, title="a step"
        )
        self._set(item=child)
        self.client.delete(self.detail_url)
        self.assertFalse(ToDoItemReminder.objects.filter(item_id=child.item_id).exists())


class ToDoReminderTickTests(_TodoReminderMixin):
    def setUp(self):
        super().setUp()
        self.reminder = ToDoItemReminder.objects.create(
            item=self.item, remind_at=timezone.now() - timedelta(minutes=1)
        )

    def test_due_reminder_files_an_inbox_activity_and_pushes(self):
        with patch.object(todo_reminders, "schedule_push_for_reminder") as push:
            counts = todo_reminders.fire_due()

        self.assertEqual(counts["fired"], 1)
        self.reminder.refresh_from_db()
        self.assertIsNotNone(self.reminder.fired_at)

        item = InboxItems.objects.get(receiver=self.user)
        # Same item_type as a message reminder — from the inbox's point of
        # view it is the same thing (an Activity, nothing to approve) — and
        # `kind` is what tells the card which sentence to compose.
        self.assertEqual(item.item_type, 9)
        self.assertEqual(item.request_status, "")
        self.assertEqual(item.item_optionals["kind"], "todo_reminder")
        self.assertEqual(item.item_optionals["todo_item_id"], str(self.item.item_id))
        self.assertEqual(item.item_optionals["preview"], "ship the release notes")
        self.assertEqual(item.item_body["text"], "ship the release notes")
        # Their own appointment with themselves: no other actor to name.
        self.assertEqual(item.sender_id, self.user.id)

        push.assert_called_once()
        kwargs = push.call_args.kwargs
        self.assertEqual(str(kwargs["recipient_id"]), str(self.user.id))
        self.assertEqual(kwargs["category"], "todo_reminder")
        # Per reminder, not per to-do: two reminders on one item are two
        # moments and must not collapse into a single notification.
        self.assertEqual(kwargs["tag"], f"todo_reminder:{self.reminder.id}")

    def test_the_link_names_the_item_so_the_pane_can_focus_it(self):
        """A link that stops at the date drops the reader on a day's list
        with no way to tell which line the reminder was about."""
        with patch.object(todo_reminders, "schedule_push_for_reminder"):
            todo_reminders.fire_due()
        item = InboxItems.objects.get(receiver=self.user)
        self.assertEqual(
            item.item_optionals["href"],
            f"/workspace/todo/{self.local_date.isoformat()}/item/{self.item.item_id}",
        )

    def test_firing_twice_delivers_once(self):
        """`fired_at` doubles as the claim, so overlapping ticks (or a
        retried pass) cannot nudge the user twice about one moment."""
        with patch.object(todo_reminders, "schedule_push_for_reminder"):
            todo_reminders.fire_due()
            second = todo_reminders.fire_due()
        self.assertEqual(second["fired"], 0)
        self.assertEqual(InboxItems.objects.filter(receiver=self.user).count(), 1)

    def test_a_completed_todo_retires_the_reminder_unfired(self):
        self.item.is_completed = True
        self.item.save(update_fields=["is_completed"])
        with patch.object(todo_reminders, "schedule_push_for_reminder") as push:
            counts = todo_reminders.fire_due()
        self.assertEqual(counts["fired"], 0)
        self.reminder.refresh_from_db()
        self.assertIsNotNone(self.reminder.cancelled_at)
        self.assertFalse(InboxItems.objects.filter(receiver=self.user).exists())
        push.assert_not_called()

    def test_a_future_reminder_is_not_due(self):
        self.reminder.remind_at = timezone.now() + timedelta(hours=1)
        self.reminder.save(update_fields=["remind_at"])
        with patch.object(todo_reminders, "schedule_push_for_reminder"):
            self.assertEqual(todo_reminders.fire_due()["due"], 0)

    def test_an_untitled_todo_still_gets_a_push_body(self):
        """A blank row is a real state ("+ New Todo" creates one), and an
        empty push body renders as a title over a void."""
        self.item.title = ""
        self.item.save(update_fields=["title"])
        with patch.object(todo_reminders, "schedule_push_for_reminder") as push:
            todo_reminders.fire_due()
        self.assertEqual(push.call_args.kwargs["body"], "Untitled to-do")


class ToDoReminderOverdueWarningTests(_TodoReminderMixin):
    """The dead-cron canary, on the to-do side.

    An unfired reminder is completely silent — no push, no inbox card, no
    log line — which is how a missing cron service survived a release once
    already. The warning has to land under THIS module's logger, or an alert
    scoped to `origin.services.todo_reminders` never sees it.
    """

    def test_a_long_overdue_reminder_warns_under_the_todo_logger(self):
        ToDoItemReminder.objects.create(
            item=self.item, remind_at=timezone.now() - timedelta(hours=2)
        )
        with self.assertLogs("origin.services.todo_reminders", level="WARNING") as logs:
            self.client.get(reverse("todo_reminders"))
        self.assertIn("todo-reminders", logs.output[0])

    def test_a_reminder_a_moment_late_does_not_warn(self):
        """A minutely tick is routinely seconds behind; warning on that
        would train everyone to ignore the line that matters."""
        ToDoItemReminder.objects.create(
            item=self.item, remind_at=timezone.now() - timedelta(seconds=30)
        )
        with self.assertNoLogs("origin.services.todo_reminders", level="WARNING"):
            self.client.get(reverse("todo_reminders"))


class ToDoReminderTickCommandTests(_TodoReminderMixin):
    """The shared cron must drain to-do reminders as well as message ones.

    Delivery is entirely the tick's work — no tick means no push, no inbox
    card and no log line — so this is the test that stops a to-do reminder
    from silently never arriving.
    """

    def test_the_command_fires_due_todo_reminders(self):
        ToDoItemReminder.objects.create(
            item=self.item, remind_at=timezone.now() - timedelta(minutes=1)
        )
        out = StringIO()
        with patch.object(todo_reminders, "schedule_push_for_reminder"):
            call_command("message_reminder_tick", stdout=out)
        self.assertIn("todo_reminder: 1 fired", out.getvalue())
        self.assertTrue(InboxItems.objects.filter(receiver=self.user).exists())

    def test_dry_run_reports_both_kinds_and_fires_nothing(self):
        ToDoItemReminder.objects.create(
            item=self.item, remind_at=timezone.now() - timedelta(minutes=1)
        )
        out = StringIO()
        call_command("message_reminder_tick", "--dry-run", stdout=out)
        self.assertIn("1 to-do reminder(s) due", out.getvalue())
        self.assertFalse(InboxItems.objects.filter(receiver=self.user).exists())
