"""MIP-1502: chat access leftovers that are plain access fixes.

  1. `POST /api/v3/channels/<id>/join/` answered 400 "Only group messages support self-join." for
     a real PM / DM / MDM id and 404 for a missing one, to any teammate: an existence oracle for
     chats you are not in. A chat you are not in now answers 404 like a missing one; a member of
     a non-group chat still reads the 400.
  2. `GET /api/v3/flags/` kept listing a flag after its owner lost the chat (and listed a stray
     channel-member row's flag on a project chat). It lists only the flags on messages of chats the
     caller is in now (`chat_member_q`, the rule the message endpoints apply).
  3. A due message reminder delivered the message's preview (push + inbox card) to someone who is
     no longer in the chat. It is retired instead.

Not done here (asked, or needing a decision): the roster for a project member whose mirror row is
missing, the activity feed's message rows, and `create_task_assign_activity` (MIP-1495's family).
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Flag,
    Message,
    MessageReminder,
)
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services import message_reminders
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
MISSING = "00000000-0000-4000-8000-000000000000"


class Base(BaseAPITestCase):
    """`self.user` and `self.user2` are on the team; `self.tm` is a third teammate."""

    def setUp(self):
        super().setUp()
        self.tm = User.objects.create_user(username="tm", email="tm@example.com", password="x")
        TeamMembers.objects.create(team=self.team, attendee=self.tm)

    def project_chat(self, *members):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="LFT",
            owner=self.user,
            project_system_user=self.user,
        )
        for m in dict.fromkeys((self.user, *members)):
            ProjectMembers.objects.create(team=self.team, project=project, attendee=m)
        return project, Channel.objects.get(kind=ChannelKind.PM, project=project)

    def gm(self, *members):
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="gm")
        for m in members:
            ChannelMember.objects.create(channel=channel, user=m)
        return channel

    def message(self, channel, text="the message text"):
        return Message.objects.create(
            channel=channel,
            sender=self.user,
            seq=(Message.objects.filter(channel=channel).count() + 1),
            body=[],
            body_text=text,
        )


class JoinDoesNotTellRealFromMissing(Base):
    def join(self, user, channel_id):
        self.authenticate(user)
        return self.client.post(f"/api/v3/channels/{channel_id}/join/", {}, format="json")

    def test_a_pm_dm_or_mdm_you_are_not_in_answers_like_a_missing_chat(self):
        _project, pm = self.project_chat()
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="dm")
        ChannelMember.objects.create(channel=dm, user=self.user)
        mdm = Channel.objects.create(team=self.team, kind=ChannelKind.MDM, title="mdm")
        ChannelMember.objects.create(channel=mdm, user=self.user)
        missing = self.join(self.user2, MISSING)
        self.assertEqual(missing.status_code, 404)
        for channel in (pm, dm, mdm):
            with self.subTest(kind=channel.kind):
                res = self.join(self.user2, channel.id)
                self.assertEqual(res.status_code, 404, res.content)
                self.assertEqual(res.json(), missing.json())

    def test_a_member_of_a_non_group_chat_still_reads_the_400(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="dm")
        ChannelMember.objects.create(channel=dm, user=self.user)
        self.assertEqual(self.join(self.user, dm.id).status_code, 400)

    def test_a_stray_row_on_a_project_chat_is_not_membership(self):
        _project, pm = self.project_chat()
        ChannelMember.objects.update_or_create(
            channel=pm, user=self.tm, defaults={"is_deleted": False}
        )
        self.assertEqual(self.join(self.tm, pm.id).status_code, 404)

    def test_joining_a_public_group_chat_is_unchanged(self):
        gm = self.gm(self.user)
        res = self.join(self.user2, gm.id)
        self.assertEqual(res.status_code, 200, res.content)

    def test_a_private_group_chat_still_says_403(self):
        """Left alone: the app's request-access prompt for a private group reads this 403."""
        gm = self.gm(self.user)
        gm.is_private = True
        gm.save(update_fields=["is_private"])
        self.assertEqual(self.join(self.user2, gm.id).status_code, 403)


class FlagListFollowsMembership(Base):
    def flags(self, user):
        self.authenticate(user)
        res = self.client.get("/api/v3/flags/", {"status": "active"})
        self.assertEqual(res.status_code, 200, res.content)
        return {f["messageId"] for f in res.json()["flags"]}

    def test_a_flag_on_a_chat_you_left_stops_listing(self):
        gm = self.gm(self.user, self.user2)
        m = self.message(gm)
        Flag.objects.create(user=self.user2, message=m)
        self.assertEqual(self.flags(self.user2), {str(m.id)})
        ChannelMember.objects.filter(channel=gm, user=self.user2).update(is_deleted=True)
        self.assertEqual(self.flags(self.user2), set())

    def test_a_flag_on_a_project_chat_you_were_taken_off_stops_listing(self):
        project, pm = self.project_chat(self.user2)
        m = self.message(pm)
        Flag.objects.create(user=self.user2, message=m)
        ProjectMembers.objects.filter(project=project, attendee=self.user2).delete()
        # a stray channel-member row opens nothing (MIP-1480)
        ChannelMember.objects.filter(channel=pm, user=self.user2).update(is_deleted=False)
        self.assertEqual(self.flags(self.user2), set())

    def test_the_completed_list_follows_it_too(self):
        gm = self.gm(self.user, self.user2)
        m = self.message(gm)
        Flag.objects.create(user=self.user2, message=m, completed_at=timezone.now())
        ChannelMember.objects.filter(channel=gm, user=self.user2).update(is_deleted=True)
        self.authenticate(self.user2)
        res = self.client.get("/api/v3/flags/", {"status": "completed"})
        self.assertEqual(res.json()["flags"], [])

    def test_flags_on_chats_you_are_in_are_unchanged(self):
        gm = self.gm(self.user, self.user2)
        project, pm = self.project_chat(self.user2)
        a, b = self.message(gm), self.message(pm)
        Flag.objects.create(user=self.user2, message=a)
        Flag.objects.create(user=self.user2, message=b)
        self.assertEqual(self.flags(self.user2), {str(a.id), str(b.id)})


class DueReminderFollowsMembership(Base):
    def reminder(self, user, message):
        Flag.objects.create(user=user, message=message)
        return MessageReminder.objects.create(
            user=user, message=message, remind_at=timezone.now() - timedelta(minutes=1)
        )

    def fire(self, reminder):
        with mock.patch("origin.services.message_reminders.schedule_push_for_reminder") as push:
            return message_reminders.fire(reminder), push

    def test_a_reminder_for_a_chat_you_left_is_retired_not_delivered(self):
        from origin.models.common.inbox_models import InboxItems

        gm = self.gm(self.user, self.user2)
        reminder = self.reminder(self.user2, self.message(gm, "secret preview"))
        ChannelMember.objects.filter(channel=gm, user=self.user2).update(is_deleted=True)
        fired, push = self.fire(reminder)
        self.assertFalse(fired)
        push.assert_not_called()
        self.assertFalse(InboxItems.objects.filter(receiver=self.user2).exists())
        reminder.refresh_from_db()
        self.assertIsNotNone(reminder.cancelled_at)
        self.assertIsNone(reminder.fired_at)

    def test_a_reminder_for_a_chat_you_are_in_still_fires(self):
        gm = self.gm(self.user, self.user2)
        reminder = self.reminder(self.user2, self.message(gm))
        fired, push = self.fire(reminder)
        self.assertTrue(fired)
        push.assert_called_once()
