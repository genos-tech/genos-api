"""A schedule's report posted into a chat instead of the Inbox (MIP-1420).

What matters, each with a test that fails when it is removed:

* a report goes into a chat ONLY when everyone who reads the chat may read it: a project report
  into a chat of project members (or the project's own chat), a report about the owner never into
  a chat other people read, an AI report never into a chat;
* the rule is asked when the schedule is saved AND at every run (a person added to the chat since
  then stops the report, in words, and nothing is posted);
* the message has no author, a marker and no push; a repeat of one scheduled run posts once and
  "Run now" posts every time; nothing goes to the Inbox;
* a refusal never names who is not allowed;
* the REST page can set and show the target, lists the chats it can point at, and the connected-AI
  tools' view of a schedule is unchanged.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.common.schedule_models import MemberSchedule, MemberScheduleSeed
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services import schedule_audience as audience
from origin.services import schedule_delivery, schedule_text, schedules
from origin.services.account_export import build_export
from origin.services.schedule_jobs import JOBS, SHARED_KEY
from origin.services.user_time import user_zone
from origin.tests.test_schedule_jobs import JobBase

LIST = "/api/v2/schedules/"


class ChatBase(JobBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        MemberScheduleSeed.objects.create(user=self.user, team=self.team)
        # Someone on the team who is NOT on the project.
        self.outsider = CustomUser.objects.create_user(
            username="outsider", email="outsider@example.com", password="pw-outsider-1"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.outsider)
        self.pm = Channel.objects.get_or_create(
            team=self.team,
            kind=ChannelKind.PM,
            project=self.project,
            defaults={"title": "", "owner": self.user, "legacy_chat_id": self.project.project_id},
        )[0]

    def gm(self, *members, title="Squad", **extra) -> Channel:
        extra.setdefault("is_private", True)  # a public chat is refused as a target
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title=title, owner=self.user, **extra
        )
        for user in members:
            ChannelMember.objects.create(channel=channel, user=user)
        return channel

    def save(self, channel, job="project_updates", name="Weekly", **over):
        kw = {"frequency": "daily", "time_of_day": "09:30"}
        if job == "project_updates":
            kw["params"] = {"project_id": self.project.project_id}
        kw.update(over)
        return schedules.create(
            self.ctx,
            name=name,
            job=job,
            deliver_channel_id=channel.id if channel is not None else None,
            **kw,
        )

    def refusal(self, channel, **kw) -> str:
        with self.assertRaises(schedules.ScheduleError) as cm:
            self.save(channel, **kw)
        return str(cm.exception)

    def posted(self, channel=None):
        return Message.objects.filter(channel=channel or self.pm).order_by("seq")

    def run_it(self, sched, run_at=None):
        return schedules._execute(sched, run_at=run_at)


class TestWhoMayReadIt(ChatBase):
    def test_a_project_report_goes_to_a_chat_of_project_members(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat)
        self.assertEqual(sched.deliver_channel_id, chat.id)

    def test_a_project_report_goes_to_the_projects_own_chat(self):
        self.assertEqual(self.save(self.pm).deliver_channel_id, self.pm.id)

    def test_a_chat_with_someone_off_the_project_is_refused_without_naming_them(self):
        chat = self.gm(self.user, self.user2, self.outsider)
        said = self.refusal(chat)
        self.assertEqual(said, audience.NOT_EVERYONE)
        self.assertNotIn("outsider", said)
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_a_milestone_report_follows_the_milestones_project(self):
        from origin.models.task.milestone_models import MilestoneMaster

        milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Launch",
            reporter=self.user,
            status="Open",
        )
        ok = self.gm(self.user, self.user2, title="Members")
        bad = self.gm(self.user, self.outsider, title="Mixed")
        params = {"milestone_id": milestone.milestone_id}
        self.assertTrue(self.save(ok, job="milestone_updates", params=params, name="m1"))
        self.assertEqual(
            self.refusal(bad, job="milestone_updates", params=params, name="m2"),
            audience.NOT_EVERYONE,
        )

    def test_a_report_about_the_owner_never_goes_to_a_chat_others_read(self):
        chat = self.gm(self.user, self.user2)
        self.assertEqual(self.refusal(chat, job="what_to_do_today"), audience.ABOUT_YOU)
        self.assertEqual(self.refusal(self.pm, job="what_you_did"), audience.ABOUT_YOU)

    def test_any_report_may_go_to_a_chat_only_the_owner_is_in(self):
        alone = self.gm(self.user, title="Me")
        self.assertTrue(self.save(alone, job="what_to_do_today", name="Mine"))

    def test_a_report_with_an_ai_part_follows_the_rule_of_its_own_kind(self):
        """MIP-2351: AI neither widens nor narrows who may read a report."""
        ai = {"ai_sentences": True}
        project = {"project_id": self.project.project_id, "ai_sentences": True}
        alone = self.gm(self.user, title="Me")
        shared = self.gm(self.user, self.user2, title="Members")
        mixed = self.gm(self.user, self.user2, self.outsider, title="Mixed")
        # A report about the owner: a chat only they are in, never a chat others read.
        self.assertTrue(self.save(alone, job="what_to_do_today", params=ai, name="Mine"))
        self.assertEqual(
            self.refusal(shared, job="what_to_do_today", params=ai, name="Mine 2"),
            audience.ABOUT_YOU,
        )
        # A project report with AI reaches the project's people, and still nobody else.
        self.assertTrue(self.save(shared, job="project_updates", params=project, name="Team"))
        self.assertEqual(
            self.refusal(mixed, job="project_updates", params=project, name="Team 2"),
            audience.NOT_EVERYONE,
        )

    def test_a_chat_with_a_guest_or_a_public_chat_is_refused_for_an_ai_report_too(self):
        project = {"project_id": self.project.project_id, "ai_sentences": True}
        guest = CustomUser.objects.create_user(
            username="guest", email="guest@example.com", password="pw-guest-1"
        )
        chat = self.gm(self.user, self.user2, guest, title="With a guest")
        public = self.gm(self.user, self.user2, title="Open", is_private=False)
        self.assertEqual(
            self.refusal(chat, job="project_updates", params=project), audience.NOT_EVERYONE
        )
        self.assertEqual(
            self.refusal(public, job="project_updates", params=project), audience.PUBLIC
        )

    def test_only_a_chat_the_member_can_read_in_this_team_counts(self):
        elsewhere = self.gm(self.user2, title="Not mine")  # the owner is not in it
        direct = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        ChannelMember.objects.create(channel=direct, user=self.user)
        gone = self.gm(self.user, title="Gone", is_deleted=True)
        self.assertEqual(self.refusal(elsewhere), audience.NOT_FOUND)
        self.assertEqual(self.refusal(gone), audience.NOT_FOUND)
        self.assertEqual(self.refusal(direct), audience.KIND)
        with self.assertRaises(schedules.ScheduleError):
            schedules.create(
                self.ctx,
                name="x",
                job="project_updates",
                params={"project_id": self.project.project_id},
                frequency="daily",
                time_of_day="09:30",
                deliver_channel_id="not-an-id",
            )

    def test_an_archived_chat_and_a_chat_shared_with_other_teams_are_refused(self):
        archived = self.gm(self.user, self.user2, title="Old", archived_at=timezone.now())
        shared = self.gm(self.user, self.user2, title="Shared", is_private=True, is_external=True)
        self.assertEqual(self.refusal(archived), audience.ARCHIVED)
        self.assertEqual(self.refusal(shared), audience.EXTERNAL)

    def test_a_public_chat_is_refused_because_anyone_could_join_and_read_the_history(self):
        public = self.gm(self.user, self.user2, title="Open", is_private=False)
        alone = self.gm(self.user, title="Open and alone", is_private=False)
        self.assertEqual(self.refusal(public), audience.PUBLIC)
        self.assertEqual(self.refusal(alone, job="what_to_do_today"), audience.PUBLIC)
        self.assertNotIn(
            "Open", {t["title"] for t in audience.chat_targets(self.user.id, self.team.team_id)}
        )

    def test_the_project_owner_who_left_the_team_does_not_count_as_a_reader_who_may_read(self):
        # The owner of the project reads it only while on the team (`active_project_owners`).
        ProjectMembers.objects.filter(project=self.project, attendee=self.user).delete()
        left = CustomUser.objects.create_user(
            username="left", email="left@example.com", password="pw-left-123"
        )
        ProjectMaster.objects.filter(pk=self.project.pk).update(owner=left)
        chat = self.gm(self.user, left, title="With the old owner")
        # `left` is not on the team: they are not an allowed reader just for owning the project.
        self.assertEqual(self.refusal(chat), audience.NOT_EVERYONE)

    def test_the_picker_lists_only_chats_the_member_can_use(self):
        mine = self.gm(self.user, self.user2, title="Squad")
        self.gm(self.user2, title="Not mine")
        self.gm(self.user, title="Old", archived_at=timezone.now())
        self.gm(self.user, title="Shared", is_private=True, is_external=True)
        direct = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        ChannelMember.objects.create(channel=direct, user=self.user)
        got = audience.chat_targets(self.user.id, self.team.team_id)
        self.assertEqual(
            sorted((t["title"], t["kind"]) for t in got),
            sorted([("Squad", "group"), (self.project.project_name, "project")]),
        )
        self.assertIn(str(mine.id), {t["id"] for t in got})
        self.assertEqual(
            [t["projectId"] for t in got if t["kind"] == "project"], [self.project.project_id]
        )


class TestSavingAndEditing(ChatBase):
    def test_the_same_report_at_the_same_time_for_two_chats_is_two_schedules(self):
        one = self.gm(self.user, self.user2, title="One")
        two = self.gm(self.user, self.user2, title="Two")
        a = self.save(one, name="A")
        b = self.save(two, name="B")
        self.assertNotEqual(a.pk, b.pk)
        with self.assertRaises(schedules.ScheduleError):
            self.save(one, name="C")  # the same chat, report and time is the same schedule

    def test_the_inbox_is_the_default_and_a_target_can_be_cleared(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat)
        sched = schedules.update(self.ctx, sched.id, deliver_channel_id=None)
        self.assertIsNone(sched.deliver_channel_id)
        self.assertIsNone(self.save(None, name="Inbox one", time_of_day="10:00").deliver_channel_id)

    def test_changing_the_report_or_the_chat_asks_the_rule_again(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat)
        with self.assertRaises(schedules.ScheduleError) as cm:
            schedules.update(self.ctx, sched.id, job="what_you_did", params={})
        self.assertEqual(str(cm.exception), audience.ABOUT_YOU)
        mixed = self.gm(self.user, self.outsider, title="Mixed")
        with self.assertRaises(schedules.ScheduleError):
            schedules.update(self.ctx, sched.id, deliver_channel_id=mixed.id)
        sched.refresh_from_db()
        self.assertEqual(sched.deliver_channel_id, chat.id)
        # A rename leaves the target alone and does not ask.
        self.assertEqual(
            schedules.update(self.ctx, sched.id, name="Renamed").deliver_channel_id, chat.id
        )

    def test_the_plans_schedule_count_still_applies(self):
        chat = self.gm(self.user, self.user2)
        with mock.patch("origin.services.schedules.schedule_limit", return_value=1):
            self.save(chat, name="One")
            with self.assertRaises(schedules.ScheduleLimitReached):
                self.save(chat, name="Two", time_of_day="11:00")

    def test_the_connected_ai_tools_see_no_new_field(self):
        sched = self.save(self.gm(self.user, self.user2))
        shown = schedule_text.present(sched, user_zone(self.user))
        extra = [k for k in shown if k != "deliver_push" and ("deliver" in k or "channel" in k)]
        self.assertFalse(extra, shown)
        for row in schedule_text.job_catalog():
            self.assertNotIn("shareable", row)


class TestRunning(ChatBase):
    def test_the_report_is_posted_as_one_message_with_no_author_and_nothing_in_the_inbox(self):
        sched = self.save(self.pm)
        self.make_task("Ship the page", status="Closed")
        task = self.make_task("Write the copy")
        self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=2))
        status, error = self.run_it(sched)
        self.assertEqual((status, error), ("ok", ""))
        (message,) = self.posted()
        self.assertIsNone(message.sender_id)
        self.assertEqual(message.metadata["scheduleReport"]["scheduleId"], str(sched.id))
        self.assertEqual(message.metadata["scheduleReport"]["job"], "project_updates")
        self.assertEqual(message.body_text, f"Project: {self.project.project_name}")
        self.assertEqual(message.body[0]["type"], "heading")
        self.assertIn("bulletListItem", {b["type"] for b in message.body})
        self.assertFalse(InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT).exists())
        sched.refresh_from_db()
        # The connected-AI tools read `last_result`: it has the shape it always had.
        self.assertEqual(set(sched.last_result), {"title", "items"})
        self.assertEqual(sched.last_result["title"], f"Project: {self.project.project_name}")

    def test_it_rings_nobody_and_makes_no_activity(self):
        from origin.models.chat.unified_models import Activity

        sched = self.save(self.pm)
        with mock.patch("origin.services.schedule_delivery.dispatch_push_for_inbox_item") as push:
            self.run_it(sched)
        push.assert_not_called()
        self.assertEqual(Activity.objects.count(), 0)

    def test_the_open_chat_is_told(self):
        sched = self.save(self.pm)
        with mock.patch("origin.services.schedule_delivery.realtime_push.push_message") as push:
            self.run_it(sched)
        self.assertEqual(push.call_count, 1)

    def test_a_repeat_of_one_scheduled_run_posts_once_and_run_now_posts_every_time(self):
        # Something to say: a blocked task. A project with nothing to report posts nothing on a
        # scheduled run (MIP-1417, test_report_outputs_mip1417).
        self.make_task("Waiting on the vendor", status="Blocked")
        sched = self.save(self.pm)
        at = timezone.now().replace(microsecond=0)
        self.assertEqual(self.run_it(sched, run_at=at)[0], "ok")
        self.assertEqual(self.run_it(sched, run_at=at)[0], "ok")
        self.assertEqual(self.posted().count(), 1)
        self.run_it(sched, run_at=at + timedelta(days=1))
        self.assertEqual(self.posted().count(), 2)
        self.run_it(sched)
        self.run_it(sched)
        self.assertEqual(self.posted().count(), 4)

    def test_the_run_is_told_it_is_for_everyone(self):
        sched = self.save(self.pm)
        job = JOBS["project_updates"]
        seen = []
        real = job.run

        def spy(ctx, params):
            seen.append(dict(params))
            return real(ctx, params)

        with mock.patch.dict(JOBS, {"project_updates": dataclasses.replace(job, run=spy)}):
            self.run_it(sched)
        self.assertTrue(seen[0][SHARED_KEY])
        inbox = self.save(None, name="Inbox", time_of_day="10:00")
        self.assertNotIn(SHARED_KEY, schedules._run_params(inbox, job))

    def test_a_person_added_to_the_chat_since_stops_the_report_in_words(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat)
        self.assertEqual(self.run_it(sched)[0], "ok")
        ChannelMember.objects.create(channel=chat, user=self.outsider)
        status, error = self.run_it(sched)
        self.assertEqual(status, "failed")
        self.assertEqual(error, audience.NOT_EVERYONE)
        self.assertNotIn("outsider", error)
        self.assertEqual(self.posted(chat).count(), 1)

    def test_someone_leaving_the_project_stops_the_project_chats_report_too(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat)
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        self.assertEqual(self.run_it(sched), ("failed", audience.NOT_EVERYONE))
        self.assertEqual(self.posted(chat).count(), 0)

    def test_a_chat_that_is_archived_or_deleted_fails_the_run_and_posts_nothing(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat)
        Channel.objects.filter(pk=chat.pk).update(archived_at=timezone.now())
        self.assertEqual(self.run_it(sched), ("failed", audience.ARCHIVED))
        Channel.objects.filter(pk=chat.pk).update(is_deleted=True)
        self.assertEqual(self.run_it(sched)[0], "failed")
        self.assertEqual(self.posted(chat).count(), 0)

    def test_the_owner_leaving_the_chat_stops_it(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat)
        ChannelMember.objects.filter(channel=chat, user=self.user).update(is_deleted=True)
        self.assertEqual(self.run_it(sched)[0], "failed")
        self.assertEqual(self.posted(chat).count(), 0)

    def test_three_failures_pause_the_schedule_as_for_any_job(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat)
        Channel.objects.filter(pk=chat.pk).update(is_deleted=True)
        now = timezone.now()
        MemberSchedule.objects.filter(pk=sched.pk).update(next_run_at=now - timedelta(minutes=1))
        for _ in range(3):
            schedules.run_due(now=now)
            MemberSchedule.objects.filter(pk=sched.pk, enabled=True).update(
                next_run_at=now - timedelta(minutes=1), claimed_at=None
            )
        sched.refresh_from_db()
        self.assertFalse(sched.enabled)
        self.assertEqual(sched.paused_reason, "failures")

    def test_the_inbox_path_is_untouched(self):
        sched = self.save(None, name="Inbox")
        self.assertEqual(self.run_it(sched), ("ok", ""))
        self.assertEqual(InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT).count(), 1)
        self.assertEqual(Message.objects.filter(metadata__has_key="scheduleReport").count(), 0)


class TestInThirdPerson(ChatBase):
    def test_a_milestone_report_in_a_chat_never_says_you_about_the_owner(self):
        from origin.models.task.milestone_models import MilestoneMaster

        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v1", status="Open"
        )
        task = self.make_task("Wire it up", milestone=milestone)
        self.activity(
            task,
            self.user2,
            "assignee_changed",
            old=str(self.user2.id),
            new=str(self.user.id),
            at=self.ago(hours=1),
        )
        mine = self.run_job("milestone_updates", milestone_id=milestone.milestone_id)
        shared = self.run_job(
            "milestone_updates", milestone_id=milestone.milestone_id, **{SHARED_KEY: True}
        )
        self.assertIn("assigned this to you", " ".join(self.texts(mine)))
        self.assertNotIn("you", " ".join(self.texts(shared)).replace("your", "").split())


class TestOnlyTheProjectsOwnTasksAreNamed(ChatBase):
    """The owner may read tasks of other projects; a chat of this project's people may not."""

    def other_project(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Secret",
            code="SEC",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)
        return other

    def test_a_blocker_in_another_project_is_not_named_in_a_chat(self):
        from origin.models.task.task_models import TaskDependency

        other = self.other_project()
        blocker = self.make_task("Secret blocker", project=other)
        blocked = self.make_task("Ship", status="Blocked")
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=blocked, team=self.team)
        self.activity(
            blocked, self.user2, "status_changed", "Open", "Blocked", at=self.ago(hours=1)
        )
        mine = self.run_job("project_updates", project_id=self.project.project_id)
        shared = self.run_job(
            "project_updates", project_id=self.project.project_id, **{SHARED_KEY: True}
        )
        self.assertIn(blocker.display_id, " ".join(self.texts(mine)))
        self.assertNotIn(blocker.display_id, " ".join(self.texts(shared)))
        self.assertIn(blocked.display_id, " ".join(self.texts(shared)))

    def test_a_milestones_task_in_another_project_is_not_named_in_a_chat(self):
        from origin.models.task.milestone_models import MilestoneMaster

        other = self.other_project()
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v1", status="Open"
        )
        here = self.make_task("Visible work", milestone=milestone)
        away = self.make_task("Hidden work", milestone=milestone, project=other)
        for task in (here, away):
            self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=1))
        mine = " ".join(
            self.texts(self.run_job("milestone_updates", milestone_id=milestone.milestone_id))
        )
        shared = " ".join(
            self.texts(
                self.run_job(
                    "milestone_updates", milestone_id=milestone.milestone_id, **{SHARED_KEY: True}
                )
            )
        )
        self.assertIn(away.display_id, mine)
        self.assertNotIn(away.display_id, shared)
        self.assertIn(here.display_id, shared)


class TestTheMessage(ChatBase):
    def body(self, **over):
        base = {
            "version": 2,
            "title": "Project: Site",
            "text": "1 done.",
            "sections": [
                {
                    "heading": "Done",
                    "emoji": "🎉",
                    "items": [
                        {"text": "SITE-1 · Ship it", "link": "/workspace/tasks/1"},
                        {"text": "A plain line"},
                    ],
                }
            ],
            "stats": [{"key": "overdue", "count": 2, "emoji": "🔥"}],
        }
        base.update(over)
        return base

    @override_settings(FRONTEND_BASE_URL="https://app.example.com/")
    def test_blocks_hold_the_title_the_counts_the_sections_and_absolute_links(self):
        blocks = schedule_delivery.chat_blocks(self.body())
        kinds = [b["type"] for b in blocks]
        self.assertEqual(
            kinds,
            ["heading", "paragraph", "paragraph", "paragraph", "bulletListItem", "bulletListItem"],
        )
        self.assertEqual(blocks[2]["content"][0]["text"], "🔥 overdue 2")
        self.assertEqual(blocks[3]["content"][0]["styles"], {"bold": True})
        link = blocks[4]["content"][0]
        self.assertEqual(link["type"], "link")
        self.assertEqual(link["href"], "https://app.example.com/workspace/tasks/1")
        self.assertEqual(blocks[5]["content"][0]["text"], "A plain line")

    def test_a_link_the_report_did_not_keep_is_never_in_the_message(self):
        report = {
            "title": "t",
            "summary": "s",
            "sections": [{"heading": "h", "items": [{"text": "x", "link": "javascript:alert(1)"}]}],
        }
        blocks = schedule_delivery.chat_blocks(schedule_delivery.clean_report(report, "n"))
        self.assertNotIn("link", {c["type"] for b in blocks for c in b["content"]})

    def test_every_message_has_an_idempotency_key_within_the_columns_limit(self):
        sched = self.save(self.pm)
        key = schedule_delivery._chat_key(sched, timezone.now())
        self.assertLessEqual(len(key), 64)


class TestExport(ChatBase):
    def test_the_members_export_names_the_chat_a_schedule_posts_in(self):
        chat = self.gm(self.user, self.user2)
        self.save(chat)
        exported = build_export(self.user)["schedules"]
        self.assertEqual(exported[0]["deliver_channel_id"], str(chat.id))


class TestRest(ChatBase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.team_q = f"?team_id={self.team.team_id}"

    def body(self, channel, **over):
        base = {
            "team_id": str(self.team.team_id),
            "name": "Project news",
            "job": "project_updates",
            "params": {"project_id": self.project.project_id},
            "frequency": "daily",
            "timeOfDay": "09:30",
            "deliverChannelId": str(channel.id) if channel is not None else None,
        }
        base.update(over)
        return base

    def test_it_creates_one_and_shows_where_it_posts(self):
        res = self.client.post(LIST, self.body(self.pm), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        row = res.data["schedule"]
        self.assertEqual(row["deliverChannelId"], str(self.pm.id))
        self.assertEqual(
            row["deliverChannel"],
            {"id": str(self.pm.id), "title": self.project.project_name, "kind": "project"},
        )

    def test_an_inbox_schedule_has_no_target_keys(self):
        res = self.client.post(LIST, self.body(None), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertNotIn("deliverChannelId", res.data["schedule"])
        self.assertNotIn("deliverChannel", res.data["schedule"])

    def test_a_refusal_is_a_sentence_and_nothing_is_saved(self):
        mixed = self.gm(self.user, self.outsider, title="Mixed")
        res = self.client.post(LIST, self.body(mixed), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error"], audience.NOT_EVERYONE)
        self.assertEqual(MemberSchedule.objects.count(), 0)

    def test_patch_moves_it_to_another_chat_and_back_to_the_inbox(self):
        created = self.client.post(LIST, self.body(self.pm), format="json").data["schedule"]
        other = self.gm(self.user, self.user2, title="Other")
        url = f"{LIST}{created['id']}/{self.team_q}"
        res = self.client.patch(url, {"deliverChannelId": str(other.id)}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["schedule"]["deliverChannel"]["title"], "Other")
        res = self.client.patch(url, {"deliverChannelId": None}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertNotIn("deliverChannelId", res.data["schedule"])

    def test_the_list_carries_the_chats_and_which_jobs_are_shareable(self):
        self.gm(self.user, self.user2, title="Squad")
        res = self.client.get(f"{LIST}{self.team_q}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            sorted(t["title"] for t in res.data["chatTargets"]),
            sorted(["Squad", self.project.project_name]),
        )
        shareable = {j["job"]: j["shareable"] for j in res.data["jobs"]}
        self.assertTrue(shareable["project_updates"])
        self.assertTrue(shareable["milestone_updates"])
        self.assertFalse(shareable["what_to_do_today"])
        self.assertFalse(shareable["what_you_did"])
        self.assertFalse(shareable["what_is_left"])

    def test_listing_costs_the_same_however_many_schedules_post_in_chats(self):
        def count():
            with CaptureQueriesContext(connection) as q:
                self.assertEqual(self.client.get(f"{LIST}{self.team_q}").status_code, 200)
            return len(q)

        self.client.post(LIST, self.body(self.pm), format="json")
        one = count()
        for i in range(3):
            self.client.post(
                LIST,
                self.body(self.pm, name=f"More {i}", timeOfDay=f"1{i}:15"),
                format="json",
            )
        self.assertEqual(count(), one)
        self.assertEqual(MemberSchedule.objects.count(), 4)

    def test_a_chat_the_member_left_keeps_its_row_but_not_its_title(self):
        chat = self.gm(self.user, self.user2, title="Secret plans")
        created = self.client.post(LIST, self.body(chat), format="json").data["schedule"]
        self.assertEqual(created["deliverChannel"]["title"], "Secret plans")
        ChannelMember.objects.filter(channel=chat, user=self.user).update(is_deleted=True)
        Channel.objects.filter(pk=chat.pk).update(title="Renamed after they left")
        res = self.client.get(f"{LIST}{self.team_q}")
        row = res.data["schedules"][0]
        self.assertEqual(row["deliverChannel"]["title"], "")
        self.assertNotIn("Renamed", str(res.data))

    def test_another_members_chat_is_not_found_not_leaked(self):
        theirs = self.gm(self.user2, title="Theirs")
        res = self.client.post(LIST, self.body(theirs), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error"], audience.NOT_FOUND)


class TestAiReportInASharedChat(ChatBase):
    """MIP-2351: an AI report may be posted in a chat other people read; the message never carries
    a cost, and the owner alone is told what it cost."""

    REQUEST = "11111111-2222-4333-8444-555555555555"

    def setUp(self):
        super().setUp()
        self.params = {"project_id": self.project.project_id, "ai_sentences": True}

    def report(self, **extra):
        out = {
            "title": "Project: Alpha",
            "summary": "Two tasks are done.",
            "sections": [{"heading": "Done", "items": [{"text": "Ship the page"}]}],
            "ai_request_id": self.REQUEST,
        }
        out.update(extra)
        return out

    def deliver(self, chat, run_at=None, **extra):
        sched = self.save(chat, params=self.params)
        schedule_delivery.deliver(sched, self.report(**extra), run_at=run_at)
        sched.refresh_from_db()
        return sched

    def texts(self, message):
        return [part["text"] for b in message.body for part in b["content"] if "text" in part]

    def test_other_readers_get_the_report_without_any_cost(self):
        chat = self.gm(self.user, self.user2)
        self.deliver(chat)
        (message,) = self.posted(chat)
        self.assertNotIn("ai", message.metadata["scheduleReport"])
        blob = json.dumps([message.body, message.body_text, message.metadata])
        for word in ("AI usage", "credit", "cost", "not measured", self.REQUEST):
            self.assertNotIn(word, blob)

    def test_the_owner_alone_is_told_the_cost_on_the_schedule_and_in_one_inbox_item(self):
        chat = self.gm(self.user, self.user2, title="Squad")
        sched = self.deliver(chat)
        self.assertEqual(sched.last_result["ai"]["request_id"], self.REQUEST)
        (item,) = InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT)
        self.assertEqual(item.receiver_id, self.user.pk)
        self.assertEqual(item.item_optionals["ai"]["request_id"], self.REQUEST)
        self.assertEqual(item.item_optionals["schedule_id"], str(sched.id))
        self.assertEqual(item.item_body["sections"], [])
        self.assertIn("Squad", item.item_body["text"])
        self.assertIn("AI usage", item.item_body["text"])
        # Nobody else has an item, and the other reader has none of the cost anywhere.
        self.assertFalse(InboxItems.objects.exclude(receiver_id=self.user.pk).exists())

    def test_a_repeat_of_one_scheduled_run_posts_and_files_the_cost_once(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat, params=self.params)
        at = timezone.now().replace(microsecond=0)
        schedule_delivery.deliver(sched, self.report(), run_at=at)
        schedule_delivery.deliver(sched, self.report(), run_at=at)
        self.assertEqual(self.posted(chat).count(), 1)
        self.assertEqual(InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT).count(), 1)

    def test_a_run_that_used_no_ai_posts_with_no_cost_item(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat, params=self.params)
        report = self.report(ai_skipped="no_credits")
        report.pop("ai_request_id")
        schedule_delivery.deliver(sched, report)
        self.assertEqual(self.posted(chat).count(), 1)
        self.assertFalse(InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT).exists())

    def test_a_person_added_who_may_not_read_it_since_stops_the_post_and_the_report_is_kept(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat, params=self.params)
        ChannelMember.objects.create(channel=chat, user=self.outsider)
        schedule_delivery.deliver(sched, self.report())
        self.assertEqual(self.posted(chat).count(), 0)
        (item,) = InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT)
        self.assertTrue(item.item_body["sections"])  # the full report, in the owner's Inbox

    def test_hand_made_runs_in_a_shared_chat_count_toward_the_daily_limit(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save(chat, params=self.params)
        now = timezone.now()
        for _ in range(schedules.AI_RUN_NOW_PER_DAY):
            schedule_delivery.deliver(sched, self.report())
        with self.assertRaises(schedules.ScheduleError):
            schedules._require_ai_run_now_room(sched, now)
