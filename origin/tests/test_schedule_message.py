"""A scheduled message (MIP-2482): the member's own words, sent as them, on a schedule.

* targets are group chats (public too) and project chats the owner is a member of: never a DM, an
  archived chat or project, another team's, or a chat the owner has left; asked at save AND at
  every run;
* the run posts a message that is the same as a hand send: the owner is its sender, mentions of
  people in the chat ring them, no Inbox item, no AI;
* one scheduled run posts once; "Run now" posts every time;
* the message is checked (shape, size, no files) and a connected AI app cannot make, change or run
  such a schedule, nor read its words.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageMention,
    ReadCursor,
)
from origin.models.common.inbox_models import InboxItems
from origin.models.common.schedule_models import MemberSchedule
from origin.search_engine.agent.tools import ToolError
from origin.services import schedule_audience as audience
from origin.services import schedule_message, schedule_text, schedules
from origin.tests.test_schedule_chat_delivery import LIST, ChatBase
from origin.views.public.mcp.secretary._schedule_args import APP_ONLY
from origin.views.public.mcp.secretary.create_my_schedule import CREATE_MY_SCHEDULE
from origin.views.public.mcp.secretary.list_my_schedules import LIST_MY_SCHEDULES
from origin.views.public.mcp.secretary.run_my_schedule import RUN_MY_SCHEDULE
from origin.views.public.mcp.secretary.update_my_schedule import UPDATE_MY_SCHEDULE

JOB = "scheduled_message"


def paragraph(*inline, block_id="b1", children=()):
    content = [
        {"type": "text", "text": i, "styles": {}} if isinstance(i, str) else i for i in inline
    ]
    return {
        "id": block_id,
        "type": "paragraph",
        "props": {},
        "content": content,
        "children": list(children),
    }


def mention(user):
    return {"type": "mention", "props": {"userId": str(user.id), "userName": user.username}}


class MessageBase(ChatBase):
    def setUp(self):
        super().setUp()
        self.chat = self.gm(self.user, self.user2)

    def body(self):
        return [paragraph("Stand-up in five ", mention(self.user2)), paragraph(block_id="b2")]

    def make(self, channel=None, body=None, name="Stand-up", **over):
        kw = {"frequency": "daily", "time_of_day": "09:30"}
        kw.update(over)
        channel = channel if channel is not None else self.chat
        return schedules.create(
            self.ctx,
            name=name,
            job=JOB,
            params={"body": body if body is not None else self.body()},
            deliver_channel_id=channel.id,
            **kw,
        )

    def messages(self, channel=None):
        return Message.objects.filter(channel=channel or self.chat).order_by("seq")


class TestTargets(MessageBase):
    def test_group_chats_public_ones_too_and_project_chats(self):
        public = self.gm(self.user, self.user2, title="Everyone", is_private=False)
        for i, channel in enumerate((self.chat, public, self.pm)):
            sched = self.make(channel, name=f"m{i}")
            self.assertEqual(sched.deliver_channel_id, channel.id)

    def test_what_is_refused_at_save(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        ChannelMember.objects.create(channel=dm, user=self.user)
        ChannelMember.objects.create(channel=dm, user=self.user2)
        mdm = Channel.objects.create(team=self.team, kind=ChannelKind.MDM, owner=self.user)
        ChannelMember.objects.create(channel=mdm, user=self.user)
        archived = self.gm(self.user, title="Old", archived_at=timezone.now())
        external = self.gm(self.user, title="Shared", is_external=True)
        not_mine = self.gm(self.user2, title="Theirs")
        public_not_joined = self.gm(self.user2, title="Open", is_private=False)
        for channel, said in (
            (dm, audience.MESSAGE_KIND),
            (mdm, audience.MESSAGE_KIND),
            (archived, audience.MESSAGE_ARCHIVED),
            (external, audience.MESSAGE_EXTERNAL),
            (not_mine, audience.NOT_FOUND),
            (public_not_joined, audience.NOT_FOUND),
        ):
            with self.assertRaises(schedules.ScheduleError) as cm:
                self.make(channel)
            self.assertEqual(str(cm.exception), said, channel.title)
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_an_archived_project_chat_is_refused(self):
        self.project.archived_at = timezone.now()
        self.project.save(update_fields=["archived_at"])
        with self.assertRaises(schedules.ScheduleError) as cm:
            self.make(self.pm)
        self.assertEqual(str(cm.exception), audience.MESSAGE_ARCHIVED)

    def test_the_inbox_is_not_a_target_at_save_or_edit(self):
        with self.assertRaises(schedules.ScheduleError) as cm:
            schedules.create(
                self.ctx,
                name="x",
                job=JOB,
                params={"body": self.body()},
                frequency="daily",
                time_of_day="09:30",
            )
        self.assertEqual(str(cm.exception), audience.MESSAGE_NEEDS_CHAT)
        sched = self.make()
        with self.assertRaises(schedules.ScheduleError):
            schedules.update(self.ctx, sched.pk, deliver_channel_id=None)

    def test_the_picker_list(self):
        public = self.gm(self.user, title="Everyone", is_private=False)
        self.gm(self.user, title="Old", archived_at=timezone.now())
        self.gm(self.user2, title="Theirs")
        ids = {t["id"] for t in audience.message_targets(self.user.id, self.team.team_id)}
        self.assertEqual(ids, {str(self.chat.id), str(public.id), str(self.pm.id)})
        # The report picker is unchanged: no public group chat.
        reports = {t["id"] for t in audience.chat_targets(self.user.id, self.team.team_id)}
        self.assertNotIn(str(public.id), reports)
        self.project.archived_at = timezone.now()
        self.project.save(update_fields=["archived_at"])
        ids = {t["id"] for t in audience.message_targets(self.user.id, self.team.team_id)}
        self.assertNotIn(str(self.pm.id), ids)


class TestTheMessageIsChecked(MessageBase):
    def refused(self, body) -> str:
        with self.assertRaises(schedules.ScheduleError) as cm:
            self.make(body=body)
        return str(cm.exception)

    def test_nothing_to_send(self):
        self.assertEqual(self.refused([]), schedule_message.EMPTY)
        self.assertEqual(self.refused([paragraph("   ")]), schedule_message.EMPTY)

    def test_files_anywhere_are_refused(self):
        image = {"type": "image", "props": {"url": "https://x.test/a.png"}, "children": []}
        for body in ([image], [paragraph("hi", children=[image])]):
            self.assertEqual(self.refused(body), schedule_message.FILES)

    def test_shapes_and_sizes(self):
        self.assertEqual(self.refused(["text"]), schedule_message.SHAPE)
        deep = paragraph("x")
        for _ in range(schedule_message.MAX_DEPTH + 1):
            deep = paragraph("x", children=[deep])
        self.assertEqual(self.refused([deep]), schedule_message.TOO_LONG)
        self.assertEqual(
            self.refused([paragraph("x" * schedule_message.MAX_BODY_BYTES)]),
            schedule_message.TOO_LONG,
        )

    def test_a_long_message_within_the_limit_is_kept_whole(self):
        body = [paragraph("y" * 30_000)]
        sched = self.make(body=body)
        self.assertEqual(sched.params, {"body": body})

    def test_a_non_array_body_is_refused(self):
        with self.assertRaises(schedules.ScheduleError):
            self.make(body="hello")


class TestTheRun(MessageBase):
    def test_it_is_a_message_from_the_owner_like_a_hand_send(self):
        sched = self.make()
        with (
            mock.patch("origin.services.realtime_push.push_message") as push,
            mock.patch("origin.services.realtime_push.push_activities") as push_acts,
        ):
            status, error = self.run_it(sched, run_at=timezone.now())
        self.assertEqual((status, error), ("ok", ""))
        message = self.messages().get()
        self.assertEqual(message.sender_id, self.user.id)
        self.assertEqual(message.metadata, {})
        self.assertEqual(message.body_text, f"Stand-up in five @{self.user2.username}")
        self.assertEqual(message.body[0]["content"], self.body()[0]["content"])
        self.assertNotEqual(message.body[0]["id"], "b1")  # fresh block ids
        # The mention rings the member of the chat, as typed by hand.
        self.assertEqual(
            list(MessageMention.objects.values_list("mentioned_user_id", flat=True)),
            [self.user2.id],
        )
        self.assertTrue(Activity.objects.filter(recipient=self.user2, message=message).exists())
        push.assert_called_once()
        self.assertTrue(push_acts.call_args.args[0])
        self.assertEqual(InboxItems.objects.count(), 0)
        sched.refresh_from_db()
        self.assertEqual(sched.last_result["items"], 1)

    def test_a_mentioned_person_who_left_the_chat_is_not_told(self):
        sched = self.make()
        ChannelMember.objects.filter(channel=self.chat, user=self.user2).delete()
        self.assertEqual(self.run_it(sched)[0], "ok")
        self.assertEqual(MessageMention.objects.count(), 0)
        self.assertFalse(Activity.objects.filter(recipient=self.user2).exists())

    def test_a_scheduled_run_posts_once_and_run_now_every_time(self):
        sched = self.make()
        at = timezone.now()
        self.run_it(sched, run_at=at)
        self.run_it(sched, run_at=at)
        self.assertEqual(self.messages().count(), 1)
        self.run_it(sched)
        self.run_it(sched)
        self.assertEqual(self.messages().count(), 3)

    def test_the_owner_is_asked_again_at_every_run(self):
        sched = self.make()
        ChannelMember.objects.filter(channel=self.chat, user=self.user).delete()
        status, error = self.run_it(sched)
        self.assertEqual(status, "failed")
        self.assertEqual(
            error, "That chat is no longer available to you, so the message was not sent."
        )
        self.assertEqual(self.messages().count(), 0)

    def test_an_archived_chat_stops_the_run(self):
        sched = self.make()
        Channel.objects.filter(pk=self.chat.pk).update(archived_at=timezone.now())
        self.assertEqual(self.run_it(sched), ("failed", audience.MESSAGE_ARCHIVED))
        self.assertEqual(self.messages().count(), 0)

    def test_the_owner_does_not_find_their_own_message_unread(self):
        sched = self.make()
        self.run_it(sched)
        first = self.messages().get()
        cursor = ReadCursor.objects.get(user=self.user, channel=self.chat, thread_root=None)
        self.assertEqual(cursor.last_read_message_id, first.id)

    def test_messages_the_owner_has_not_read_stay_unread(self):
        Message.objects.create(
            channel=self.chat, sender=self.user2, seq=1, body=self.body(), body_text="hi"
        )
        self.run_it(self.make())
        self.assertFalse(ReadCursor.objects.filter(user=self.user, channel=self.chat).exists())

    def test_a_tick_runs_it_at_its_time(self):
        sched = self.make(frequency="once", run_date=(timezone.now() + timedelta(days=2)).date())
        due = timezone.now() - timedelta(minutes=1)
        MemberSchedule.objects.filter(pk=sched.pk).update(next_run_at=due, tz_used=sched.tz_used)
        summary = schedules.run_due()
        self.assertEqual((summary.ran, summary.failed), (1, 0))
        self.assertEqual(self.messages().count(), 1)

    def test_run_now_answers_with_the_first_line(self):
        out = schedules.run_now(self.ctx, self.make().pk)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["title"], f"Stand-up in five @{self.user2.username}")
        self.assertEqual(out["items"], 1)


class TestTheApi(MessageBase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_the_form_is_told_and_can_save_and_read_back(self):
        public = self.gm(self.user, title="Everyone", is_private=False)
        data = self.client.get(f"{LIST}?team_id={self.team.team_id}").json()
        job = next(j for j in data["jobs"] if j["job"] == JOB)
        self.assertTrue(job["postsAsOwner"])
        self.assertEqual(job["params"], {})
        self.assertFalse(job["usesAi"])
        self.assertIn(str(public.id), [t["id"] for t in data["messageChatTargets"]])
        made = self.client.post(
            LIST,
            {
                "team_id": str(self.team.team_id),
                "name": "Friday",
                "job": JOB,
                "params": {"body": self.body()},
                "frequency": "weekly",
                "weekdays": [4],
                "timeOfDay": "17:00",
                "deliverChannelId": str(public.id),
            },
            format="json",
        )
        self.assertEqual(made.status_code, 201, made.content)
        row = made.json()["schedule"]
        self.assertEqual(row["params"], {"body": self.body()})
        self.assertEqual(row["deliverChannelId"], str(public.id))


class TestConnectedAiApps(MessageBase):
    def test_it_is_not_offered_and_its_words_are_not_listed(self):
        self.make()
        out = LIST_MY_SCHEDULES.run({}, self.ctx)
        self.assertNotIn(JOB, [j["job"] for j in out["jobs"]])
        self.assertNotIn(JOB, [j["job"] for j in schedule_text.job_catalog()])
        self.assertEqual(out["schedules"][0]["params"], {})

    def test_it_cannot_be_made_changed_or_run_only_paused(self):
        with self.assertRaises(ToolError) as cm:
            CREATE_MY_SCHEDULE.run(
                {"job": JOB, "name": "x", "frequency": "daily", "time_of_day": "09:00"}, self.ctx
            )
        self.assertEqual(str(cm.exception), APP_ONLY)
        sched = self.make()
        sid = str(sched.pk)
        for args in (
            {"time_of_day": "10:00"},
            {"deliver_chat_id": str(self.pm.id)},
            {"params": {"body": []}},
            {"job": "what_to_do_today"},
            {"enabled": True},
        ):
            with self.assertRaises(ToolError):
                UPDATE_MY_SCHEDULE.run({"schedule_id": sid, **args}, self.ctx)
        with self.assertRaises(ToolError):
            RUN_MY_SCHEDULE.run({"schedule_id": sid}, self.ctx)
        self.assertEqual(self.messages().count(), 0)
        out = UPDATE_MY_SCHEDULE.run({"schedule_id": sid, "enabled": False}, self.ctx)
        self.assertEqual(out["schedule"]["params"], {})
        sched.refresh_from_db()
        self.assertFalse(sched.enabled)
        self.assertEqual(sched.deliver_channel_id, self.chat.id)

    def test_another_schedule_cannot_be_turned_into_one(self):
        other = schedules.create(
            self.ctx, name="Mine", job="what_to_do_today", frequency="daily", time_of_day="09:00"
        )
        with self.assertRaises(ToolError):
            UPDATE_MY_SCHEDULE.run({"schedule_id": str(other.pk), "job": JOB}, self.ctx)
