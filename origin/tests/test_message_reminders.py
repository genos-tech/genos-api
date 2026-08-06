"""Message reminders — "remind me about this message in N hours".

Three properties carry the feature, and each test class covers one:

  - `MessageReminderViewTests`: setting a reminder FLAGS the message, and
    re-setting REPLACES rather than stacking.
  - `ReminderFlagCouplingTests`: finishing with the flag (unflag, or mark
    done) retires the reminder. A reminder that arrives pointing at a
    bookmark the user already closed is the noise that gets reminders
    turned off.
  - `MessageReminderTickTests`: the tick fires each reminder EXACTLY once,
    files it in the inbox, and stays quiet about reminders that have become
    moot.

Push is patched out where it is asserted: the real dispatcher is a no-op
without VAPID configured (so it would pass vacuously), and what matters
here is that the tick asks for the right notification.
"""

from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Flag,
    Message,
    MessageReminder,
)
from origin.models.common.inbox_models import InboxItems
from origin.services import message_reminders
from origin.tests.test_base import BaseAPITestCase

ITEM_TYPE_MESSAGE_REMINDER = message_reminders.ITEM_TYPE_MESSAGE_REMINDER


class _ReminderMixin:
    """A GM channel with one message in it, plus reminder helpers."""

    def _make_gm(self, *, members=("user",), owner="user"):
        owner_obj = getattr(self, owner)
        channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Reminders Test GM",
            owner=owner_obj,
        )
        for name in members:
            ChannelMember.objects.create(
                channel=channel,
                user=getattr(self, name),
                role="owner" if name == owner else "member",
            )
        return channel

    def _make_message(self, channel, *, sender=None, seq=1, text="ship the thing", **kwargs):
        return Message.objects.create(
            channel=channel,
            sender=sender if sender is not None else self.user2,
            seq=seq,
            body={"text": text},
            body_text=text,
            **kwargs,
        )

    def _pending(self, user=None, message=None):
        qs = MessageReminder.objects.filter(fired_at__isnull=True, cancelled_at__isnull=True)
        if user is not None:
            qs = qs.filter(user=user)
        if message is not None:
            qs = qs.filter(message=message)
        return qs


class MessageReminderViewTests(_ReminderMixin, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = self._make_gm(members=("user", "user2"))
        self.message = self._make_message(self.channel, seq=1)
        self.url = reverse("v3_message_reminder", args=[self.message.id])
        self.later = timezone.now() + timedelta(hours=3)

    def _post(self, when=None):
        return self.client.post(
            self.url, {"remindAt": (when or self.later).isoformat()}, format="json"
        )

    def test_unauthenticated_returns_401(self):
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_set_reminder_also_flags_the_message(self):
        """The reminder's whole promise is handing the message back, and the
        flagged list is where the user will look for it."""
        self.authenticate()
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["reminder"]["messageId"], str(self.message.id))
        self.assertEqual(resp.data["flag"]["messageId"], str(self.message.id))
        self.assertIsNone(resp.data["flag"]["completedAt"])
        self.assertEqual(self._pending(self.user, self.message).count(), 1)
        self.assertTrue(
            Flag.objects.filter(
                user=self.user, message=self.message, completed_at__isnull=True
            ).exists()
        )

    def test_resetting_replaces_rather_than_stacks(self):
        """ "remind me in 1 hour" after "remind me tomorrow" is a change of
        mind, not a second nudge."""
        self.authenticate()
        self._post(timezone.now() + timedelta(days=1))
        sooner = timezone.now() + timedelta(hours=1)
        self._post(sooner)

        pending = self._pending(self.user, self.message)
        self.assertEqual(pending.count(), 1)
        self.assertAlmostEqual(pending.first().remind_at.timestamp(), sooner.timestamp(), delta=1)
        # The superseded row is kept as cancelled, not deleted.
        self.assertEqual(
            MessageReminder.objects.filter(
                user=self.user, message=self.message, cancelled_at__isnull=False
            ).count(),
            1,
        )

    def test_reminder_on_completed_flag_reopens_it(self):
        self.authenticate()
        Flag.objects.create(user=self.user, message=self.message, completed_at=timezone.now())
        resp = self._post()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIsNone(Flag.objects.get(user=self.user, message=self.message).completed_at)

    def test_past_and_far_future_are_rejected(self):
        self.authenticate()
        for when in (
            timezone.now() - timedelta(hours=1),
            timezone.now() + timedelta(days=400),
        ):
            resp = self._post(when)
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, when)
        self.assertFalse(self._pending(self.user).exists())
        # A rejected reminder must not leave a flag behind either.
        self.assertFalse(Flag.objects.filter(user=self.user, message=self.message).exists())

    def test_unparseable_or_naive_remind_at_returns_400(self):
        self.authenticate()
        for payload in ({}, {"remindAt": "tomorrow"}, {"remindAt": "2030-01-01T09:00:00"}):
            resp = self.client.post(self.url, payload, format="json")
            self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST, payload)
        self.assertFalse(MessageReminder.objects.exists())

    def test_non_member_gets_404_and_creates_nothing(self):
        """Same existence-hiding 404 as the flag endpoint it rides on."""
        other_channel = self._make_gm(members=("user2",), owner="user2")
        other_message = self._make_message(other_channel, sender=self.user2, seq=1)
        self.authenticate()
        resp = self.client.post(
            reverse("v3_message_reminder", args=[other_message.id]),
            {"remindAt": self.later.isoformat()},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(MessageReminder.objects.exists())
        self.assertFalse(Flag.objects.exists())

    def test_delete_cancels_reminder_but_keeps_the_flag(self):
        """ "Stop nagging me" is not "forget about this"."""
        self.authenticate()
        self._post()
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(self._pending(self.user, self.message).exists())
        self.assertTrue(Flag.objects.filter(user=self.user, message=self.message).exists())

    def test_delete_without_a_reminder_still_204(self):
        self.authenticate()
        self.assertEqual(self.client.delete(self.url).status_code, status.HTTP_204_NO_CONTENT)

    def test_list_returns_only_own_pending_soonest_first(self):
        m2 = self._make_message(self.channel, seq=2, text="second")
        soon = timezone.now() + timedelta(minutes=20)
        MessageReminder.objects.create(
            user=self.user, message=self.message, remind_at=timezone.now() + timedelta(days=1)
        )
        MessageReminder.objects.create(user=self.user, message=m2, remind_at=soon)
        # Noise that must not show up: someone else's, one already fired,
        # and one cancelled.
        MessageReminder.objects.create(user=self.user2, message=self.message, remind_at=soon)
        MessageReminder.objects.create(
            user=self.user, message=m2, remind_at=soon, fired_at=timezone.now()
        )
        MessageReminder.objects.create(
            user=self.user, message=m2, remind_at=soon, cancelled_at=timezone.now()
        )

        self.authenticate()
        resp = self.client.get(reverse("v3_reminder_list"))
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        returned = [r["messageId"] for r in resp.data["reminders"]]
        self.assertEqual(returned, [str(m2.id), str(self.message.id)])


class ReminderFlagCouplingTests(_ReminderMixin, BaseAPITestCase):
    """Both ways of finishing with a flag retire its reminder."""

    def setUp(self):
        super().setUp()
        self.channel = self._make_gm(members=("user",))
        self.message = self._make_message(self.channel, sender=self.user, seq=1)
        self.flag_url = reverse("v3_message_flag", args=[self.message.id])
        self.authenticate()
        self.client.post(
            reverse("v3_message_reminder", args=[self.message.id]),
            {"remindAt": (timezone.now() + timedelta(hours=2)).isoformat()},
            format="json",
        )

    def test_unflagging_cancels_the_reminder(self):
        self.assertEqual(self.client.delete(self.flag_url).status_code, 204)
        self.assertFalse(self._pending(self.user, self.message).exists())

    def test_marking_done_cancels_the_reminder(self):
        resp = self.client.patch(self.flag_url, {"completed": True}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(self._pending(self.user, self.message).exists())

    def test_reopening_does_not_resurrect_it(self):
        """By the time a flag is reopened the chosen moment has usually
        passed; the user sets a new reminder instead of inheriting a stale
        one that would fire immediately."""
        self.client.patch(self.flag_url, {"completed": True}, format="json")
        self.client.patch(self.flag_url, {"completed": False}, format="json")
        self.assertFalse(self._pending(self.user, self.message).exists())

    def test_another_users_reminder_survives_my_unflag(self):
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        theirs = MessageReminder.objects.create(
            user=self.user2, message=self.message, remind_at=timezone.now() + timedelta(hours=2)
        )
        self.client.delete(self.flag_url)
        theirs.refresh_from_db()
        self.assertIsNone(theirs.cancelled_at)


class MessageReminderTickTests(_ReminderMixin, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = self._make_gm(members=("user",))
        self.message = self._make_message(self.channel, sender=self.user2, seq=1)
        Flag.objects.create(user=self.user, message=self.message)
        self.reminder = MessageReminder.objects.create(
            user=self.user,
            message=self.message,
            remind_at=timezone.now() - timedelta(minutes=1),
        )

    def test_due_reminder_files_an_inbox_activity_and_pushes(self):
        with patch.object(message_reminders, "schedule_push_for_reminder") as push:
            counts = message_reminders.fire_due()

        self.assertEqual(counts["fired"], 1)
        self.reminder.refresh_from_db()
        self.assertIsNotNone(self.reminder.fired_at)

        item = InboxItems.objects.get(receiver=self.user)
        self.assertEqual(item.item_type, ITEM_TYPE_MESSAGE_REMINDER)
        # Not a request: nothing to approve, so no pending status that would
        # render Approve/Reject buttons on it.
        self.assertEqual(item.request_status, "")
        self.assertEqual(item.item_optionals["kind"], "message_reminder")
        self.assertEqual(item.item_optionals["message_id"], str(self.message.id))
        self.assertEqual(item.item_optionals["preview"], "ship the thing")
        self.assertEqual(item.item_body["text"], "ship the thing")

        push.assert_called_once()
        kwargs = push.call_args.kwargs
        self.assertEqual(str(kwargs["recipient_id"]), str(self.user.id))
        self.assertEqual(
            kwargs["url"], f"/workspace/chat/gm/{self.channel.id}/message/{self.message.seq}"
        )
        self.assertEqual(kwargs["tag"], f"message_reminder:{self.reminder.id}")

    def test_the_link_names_the_message_so_the_client_can_focus_it(self):
        """A link that stops at the channel drops the reader into the chat
        with no way to tell which message the reminder was about."""
        with patch.object(message_reminders, "schedule_push_for_reminder"):
            message_reminders.fire_due()
        item = InboxItems.objects.get(receiver=self.user)
        self.assertEqual(
            item.item_optionals["href"],
            f"/workspace/chat/gm/{self.channel.id}/message/{self.message.seq}",
        )

    def test_thread_reply_links_to_the_thread(self):
        """Opening the parent chat leaves a thread reply out of sight."""
        root = self._make_message(self.channel, seq=2, text="root")
        reply = self._make_message(
            self.channel,
            seq=3,
            text="in-thread",
            parent=root,
            thread_root=root,
            is_thread_reply=True,
        )
        Flag.objects.create(user=self.user, message=reply)
        MessageReminder.objects.create(
            user=self.user, message=reply, remind_at=timezone.now() - timedelta(minutes=1)
        )
        with patch.object(message_reminders, "schedule_push_for_reminder"):
            message_reminders.fire_due()
        item = InboxItems.objects.get(item_optionals__message_id=str(reply.id))
        # Thread AND message: the thread segment opens the right pane, the
        # message segment picks the reply out of it.
        self.assertEqual(
            item.item_optionals["href"],
            f"/workspace/chat/gm/{self.channel.id}/thread/{root.id}/message/{reply.seq}",
        )

    def test_fires_exactly_once_across_passes(self):
        """`fired_at` is the claim as well as the stamp, so a second pass
        (or an overlapping one) delivers nothing."""
        with patch.object(message_reminders, "schedule_push_for_reminder"):
            first = message_reminders.fire_due()
            second = message_reminders.fire_due()
        self.assertEqual((first["fired"], second["fired"]), (1, 0))
        self.assertEqual(InboxItems.objects.filter(receiver=self.user).count(), 1)

    def test_not_yet_due_is_left_alone(self):
        MessageReminder.objects.filter(pk=self.reminder.pk).update(
            remind_at=timezone.now() + timedelta(hours=1)
        )
        with patch.object(message_reminders, "schedule_push_for_reminder") as push:
            counts = message_reminders.fire_due()
        self.assertEqual(counts["fired"], 0)
        push.assert_not_called()
        self.assertFalse(InboxItems.objects.exists())

    def test_deleted_message_retires_the_reminder_quietly(self):
        Message.objects.filter(pk=self.message.pk).update(deleted_at=timezone.now())
        with patch.object(message_reminders, "schedule_push_for_reminder") as push:
            counts = message_reminders.fire_due()
        self.assertEqual((counts["fired"], counts["skipped"]), (0, 1))
        push.assert_not_called()
        self.assertFalse(InboxItems.objects.exists())
        self.reminder.refresh_from_db()
        self.assertIsNotNone(self.reminder.cancelled_at)
        self.assertIsNone(self.reminder.fired_at)

    def test_completed_flag_retires_the_reminder(self):
        """Belt and braces: the flag endpoints already cancel reminders, so
        this only fires if a row reaches the tick some other way."""
        Flag.objects.filter(user=self.user, message=self.message).update(
            completed_at=timezone.now()
        )
        with patch.object(message_reminders, "schedule_push_for_reminder") as push:
            counts = message_reminders.fire_due()
        self.assertEqual(counts["fired"], 0)
        push.assert_not_called()
        self.reminder.refresh_from_db()
        self.assertIsNotNone(self.reminder.cancelled_at)

    def test_command_reports_what_it_did(self):
        with patch.object(message_reminders, "schedule_push_for_reminder"):
            call_command("message_reminder_tick")
        self.reminder.refresh_from_db()
        self.assertIsNotNone(self.reminder.fired_at)

    def test_dry_run_fires_nothing(self):
        call_command("message_reminder_tick", dry_run=True)
        self.reminder.refresh_from_db()
        self.assertIsNone(self.reminder.fired_at)
        self.assertFalse(InboxItems.objects.exists())


class OverdueWarningTests(_ReminderMixin, BaseAPITestCase):
    """A tick that never runs produces no push, no inbox item and no log
    line of its own. The client's boot fetch is the one thing that still
    sees the evidence — a missing cron service reached production behind
    exactly that silence."""

    def setUp(self):
        super().setUp()
        self.channel = self._make_gm(members=("user",))
        self.message = self._make_message(self.channel, sender=self.user2, seq=1)
        Flag.objects.create(user=self.user, message=self.message)

    def _pending(self, *, minutes_ago):
        MessageReminder.objects.create(
            user=self.user,
            message=self.message,
            remind_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        return list(message_reminders.pending_for_user(self.user))

    def test_long_overdue_reminders_warn(self):
        pending = self._pending(minutes_ago=45)
        with self.assertLogs("origin.services.message_reminders", level="WARNING") as logs:
            late = message_reminders.warn_if_overdue(pending)
        self.assertGreater(late, 40 * 60)
        self.assertIn("message_reminder_tick", logs.output[0])

    def test_a_tick_running_slightly_behind_is_not_news(self):
        pending = self._pending(minutes_ago=1)
        with patch.object(message_reminders.logger, "warning") as warn:
            message_reminders.warn_if_overdue(pending)
        warn.assert_not_called()

    def test_nothing_pending_says_nothing(self):
        with patch.object(message_reminders.logger, "warning") as warn:
            self.assertEqual(message_reminders.warn_if_overdue([]), 0)
        warn.assert_not_called()
