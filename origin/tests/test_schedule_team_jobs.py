"""The built-in team and group-chat report jobs (MIP-2061): `team_activity` and `chat_catch_up`.

What matters, each with a test that fails when it is removed:

* both jobs are in the catalog with checked parameters and count as shareable;
* a team report goes only to a chat whose every reader is on the team (a guest or an external
  member is refused, in words that name nobody), and a public chat is refused;
* a reader who cannot read something never sees it: a project somebody in the chat is not on is
  not named, nor are its tasks counted; a group chat somebody in the chat is not in is not named;
* the run asks again, so a guest added to the chat since the schedule was saved gets nothing;
* no model: the jobs have no AI part and write nothing to the workspace.
"""

from __future__ import annotations

from datetime import timedelta

from origin.models.chat.unified_models import Channel, ChannelMember, Message
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services import schedule_audience as audience
from origin.services import schedules
from origin.services.schedule_jobs import EMOJI, JOBS
from origin.tests.test_schedule_chat_delivery import ChatBase


class TeamJobsBase(ChatBase):
    def setUp(self):
        super().setUp()
        # A second project only the owner is on.
        self.secret = ProjectMaster.objects.create(
            team=self.team, project_name="Secret skunkworks", owner=self.user, code="SEC"
        )
        ProjectMembers.objects.create(team=self.team, project=self.secret, attendee=self.user)
        # Someone who is NOT on the team: a guest of one project.
        self.guest = CustomUser.objects.create_user(
            username="guest", email="guest@example.com", password="pw-guest-1"
        )

    def save_job(self, channel, job, **over):
        return self.save(
            channel, job=job, name=f"{job} {channel.id if channel else 'inbox'}", **over
        )

    def message(self, channel, sender, *, ago=timedelta(hours=1), seq=None):
        row = Message.objects.create(
            channel=channel,
            sender=sender,
            seq=seq if seq is not None else Message.objects.filter(channel=channel).count() + 1,
            body=[],
            body_text="private words",
        )
        Message.objects.filter(pk=row.pk).update(ts_sent_at=self.now - ago)
        return row


class TestCatalog(TeamJobsBase):
    def test_both_jobs_are_in_the_catalog_as_data_only_shareable_jobs(self):
        for key in ("team_activity", "chat_catch_up"):
            job = JOBS[key]
            self.assertFalse(job.uses_ai)
            self.assertTrue(job.shareable)
            self.assertTrue(job.title and job.description)
            self.assertIn("hours", job.params_schema["properties"])
        self.assertFalse(JOBS["what_to_do_today"].shareable)
        self.assertTrue(JOBS["project_updates"].shareable)

    def test_parameters_are_checked(self):
        for job, hours in (("team_activity", 0), ("team_activity", 721), ("chat_catch_up", 169)):
            with self.assertRaises(schedules.ScheduleError, msg=f"{job} {hours}"):
                self.save(None, job=job, params={"hours": hours}, name=f"x{job}{hours}")
        with self.assertRaises(schedules.ScheduleError):
            self.save(None, job="team_activity", params={"project_id": 1}, name="typo")
        self.assertTrue(self.save(None, job="team_activity", params={"hours": 48}, name="ok"))


class TestWhoMayReceiveATeamReport(TeamJobsBase):
    def test_a_chat_of_team_members_takes_it_even_when_they_share_no_project(self):
        chat = self.gm(self.user, self.user2, self.outsider)
        self.assertEqual(self.save_job(chat, "team_activity").deliver_channel_id, chat.id)

    def test_a_guest_among_the_readers_is_refused_without_naming_them(self):
        chat = self.gm(self.user, self.user2, self.guest)
        with self.assertRaises(schedules.ScheduleError) as cm:
            self.save_job(chat, "team_activity")
        self.assertEqual(str(cm.exception), audience.NOT_TEAM)
        self.assertNotIn("guest", str(cm.exception))

    def test_a_member_who_left_the_team_counts_as_not_on_it(self):
        chat = self.gm(self.user, self.outsider)
        TeamMembers.objects.filter(attendee=self.outsider).update(is_deleted=True)
        with self.assertRaises(schedules.ScheduleError):
            self.save_job(chat, "team_activity")

    def test_a_public_chat_is_refused_for_both_jobs(self):
        public = self.gm(self.user, self.user2, title="general", is_private=False)
        for job in ("team_activity", "chat_catch_up"):
            with self.assertRaises(schedules.ScheduleError) as cm:
                self.save_job(public, job)
            self.assertEqual(str(cm.exception), audience.PUBLIC)

    def test_the_run_asks_again_so_a_guest_added_since_gets_nothing(self):
        chat = self.gm(self.user, self.user2)
        sched = self.save_job(chat, "team_activity")
        self.assertEqual(self.run_it(sched)[0], "ok")
        ChannelMember.objects.create(channel=chat, user=self.guest)
        status, error = self.run_it(sched)
        self.assertEqual(status, "failed")
        self.assertNotIn("guest", error)
        self.assertEqual(self.posted(chat).count(), 1)


class TestTeamActivityContent(TeamJobsBase):
    def close(self, project, actor, task_title, *, ago=timedelta(hours=2)):
        task = self.make_task(task_title, project=project, assignee=actor, reporter=actor)
        self.activity(task, actor, "status_changed", "Open", "Closed", at=self.now - ago)
        return task

    def test_it_counts_closed_and_new_tasks_names_new_members_and_top_closers(self):
        self.close(self.project, self.user2, "Ship A")
        self.close(self.project, self.user2, "Ship B")
        self.close(self.project, self.user, "Ship C")
        self.close(self.project, self.user, "Too old", ago=timedelta(days=30))
        TeamMembers.objects.filter(attendee=self.outsider).update(ts_joined_at=self.now)
        report = self.run_job("team_activity")
        projects = self.section(report, "Projects")
        self.assertIn(f"{self.project.project_name}: 3 closed", projects[0]["text"])
        self.assertEqual(projects[0]["link"], f"/workspace/tasks/project/{self.project.project_id}")
        self.assertEqual(self.section(report, "New on the team")[0]["text"], "outsider")
        top = self.section(report, "Top contributors")
        self.assertEqual(top[0]["text"], f"{self.user2.username}: 2 closed")
        self.assertEqual(top[1]["text"], f"{self.user.username}: 1 closed")
        self.assertIn("3 tasks closed", report["summary"])
        self.assertTrue({s["key"] for s in report["stats"]} <= set(EMOJI))

    def test_an_empty_window_says_so_and_has_no_sections(self):
        TeamMembers.objects.all().update(ts_joined_at=self.now - timedelta(days=90))
        report = self.run_job("team_activity")
        self.assertEqual(report["sections"], [])
        self.assertIn("Nothing to report", report["summary"])

    def test_in_the_inbox_the_owners_own_projects_are_all_named(self):
        self.close(self.secret, self.user, "Hidden")
        texts = self.texts(self.run_job("team_activity"))
        self.assertTrue(any("Secret skunkworks: 1 closed" in t for t in texts))

    def test_a_project_somebody_in_the_chat_is_not_on_is_not_named_or_counted(self):
        self.close(self.project, self.user2, "Shared work")
        self.close(self.secret, self.user, "Secret work")
        self.close(self.secret, self.user, "More secret work")
        chat = self.gm(self.user, self.user2)
        sched = self.save_job(chat, "team_activity")
        self.assertEqual(self.run_it(sched)[0], "ok")
        (message,) = self.posted(chat)
        text = str(message.body)
        self.assertNotIn("Secret", text)
        self.assertIn(self.project.project_name, text)
        # The secret project's closes are in nobody's ranking and not in the totals.
        self.assertIn("1 tasks closed", text)
        self.assertNotIn(f"{self.user.username}: 3", text)

    def test_per_reader_combination_each_reader_sees_only_projects_they_are_on(self):
        ProjectMembers.objects.create(team=self.team, project=self.secret, attendee=self.user2)
        self.close(self.secret, self.user, "Secret work")
        cases = (
            ((self.user, self.user2), True),  # both on the secret project
            ((self.user, self.user2, self.outsider), False),  # outsider is not
        )
        for readers, named in cases:
            with self.subTest(readers=[u.username for u in readers]):
                params = {"_audience": [str(u.id) for u in readers]}
                texts = self.texts(self.run_job("team_activity", **params))
                self.assertEqual(any("Secret skunkworks" in t for t in texts), named)

    def test_a_reader_who_is_not_on_the_team_fails_the_run_in_words(self):
        from origin.services.schedules import ScheduleRunError

        with self.assertRaises(ScheduleRunError):
            self.run_job("team_activity", _audience=[str(self.guest.id)])

    def test_it_writes_nothing_and_calls_no_model(self):
        before = (Message.objects.count(), Channel.objects.count())
        self.run_job("team_activity")
        self.assertEqual(before, (Message.objects.count(), Channel.objects.count()))


class TestChatCatchUp(TeamJobsBase):
    def busy(self, channel, *senders, n=3):
        for i in range(n):
            self.message(channel, senders[i % len(senders)], seq=i + 1)

    def test_it_names_busy_group_chats_with_counts_and_a_link_and_never_the_words(self):
        busy = self.gm(self.user, self.user2, title="Launch room")
        self.busy(busy, self.user, self.user2, n=4)
        quiet = self.gm(self.user, self.user2, title="Quiet room")
        self.busy(quiet, self.user, n=1)
        old = self.gm(self.user, title="Old room")
        for i in range(5):
            self.message(old, self.user, ago=timedelta(days=5), seq=i + 1)
        report = self.run_job("chat_catch_up")
        (row,) = self.section(report, "Busy group chats")
        self.assertEqual(row["text"], "Launch room: 4 messages from 2 people")
        self.assertEqual(row["link"], f"/workspace/chat/gm/{busy.id}")
        self.assertNotIn("private words", str(report))
        self.assertEqual(report["stats"], [{"key": "chats", "count": 1}])

    def test_a_chat_the_owner_is_not_in_is_never_named(self):
        elsewhere = self.gm(self.user2, self.outsider, title="Not yours")
        self.busy(elsewhere, self.user2, self.outsider, n=5)
        report = self.run_job("chat_catch_up")
        self.assertNotIn("Not yours", str(report))
        self.assertEqual(report["sections"], [])

    def test_a_chat_somebody_reading_the_report_is_not_in_is_not_named(self):
        both = self.gm(self.user, self.user2, title="Both here")
        only_owner = self.gm(self.user, title="Owner only")
        with_outsider = self.gm(self.user, self.user2, self.outsider, title="With outsider")
        for chat in (both, only_owner, with_outsider):
            self.busy(chat, self.user, n=3)
        cases = (
            ((self.user,), {"Both here", "Owner only", "With outsider"}),
            ((self.user, self.user2), {"Both here", "With outsider"}),
            ((self.user, self.user2, self.outsider), {"With outsider"}),
        )
        for readers, expected in cases:
            with self.subTest(readers=[u.username for u in readers]):
                params = {"_audience": [str(u.id) for u in readers]}
                report = self.run_job("chat_catch_up", **params)
                named = {i["text"].split(":")[0] for i in self.section(report, "Busy group chats")}
                self.assertEqual(named, expected)

    def test_archived_external_deleted_and_direct_chats_are_left_out(self):
        from django.utils import timezone

        archived = self.gm(self.user, title="Archived", archived_at=timezone.now())
        external = self.gm(self.user, title="External", is_external=True)
        deleted = self.gm(self.user, title="Deleted", is_deleted=True)
        for chat in (archived, external, deleted):
            self.busy(chat, self.user)
        self.busy(self.pm, self.user)  # a project chat is not a group chat
        self.assertEqual(self.run_job("chat_catch_up")["sections"], [])

    def test_the_report_posted_in_a_chat_is_not_counted_as_chat_activity(self):
        chat = self.gm(self.user, self.user2, title="Team room")
        sched = self.save_job(chat, "chat_catch_up")
        self.busy(chat, self.user2, n=3)
        self.assertEqual(self.run_it(sched)[0], "ok")
        self.assertEqual(self.run_it(sched, run_at=self.now + timedelta(days=1))[0], "ok")
        self.assertEqual(self.posted(chat).filter(sender__isnull=True).count(), 2)
        report = self.run_job("chat_catch_up", _audience=[str(self.user2.id)])
        (row,) = self.section(report, "Busy group chats")
        self.assertIn("3 messages", row["text"])

    def test_a_guest_in_the_target_chat_gets_a_report_naming_no_chat(self):
        shared = self.gm(self.user, self.guest, title="With guest")
        self.busy(shared, self.user, n=3)
        mine = self.gm(self.user, title="Mine only")
        self.busy(mine, self.user, n=3)
        target = self.gm(self.user, self.guest, title="Target")
        sched = self.save_job(target, "chat_catch_up")
        self.assertEqual(self.run_it(sched)[0], "ok")
        (message,) = self.posted(target)
        self.assertNotIn("Mine only", str(message.body))
        self.assertIn("With guest", str(message.body))
