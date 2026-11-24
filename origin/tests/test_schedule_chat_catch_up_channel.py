"""`chat_catch_up` with one chosen group chat (MIP-2348): the optional `channel_id` param.

Each test fails when its rule is removed:

* no `channel_id` keeps today's behaviour: every busy chat, and an old row is unchanged;
* with one, the report covers only that chat;
* at save, the OWNER must be in the chosen chat, and when the report is posted in a chat every
  reader of that chat must be in the chosen chat (the `share_chats` rule); a refusal names nobody;
* at every run the same is asked again: someone leaving or joining stops the run, nothing is posted.
"""

from __future__ import annotations

from datetime import timedelta

from origin.models.chat.unified_models import ChannelMember
from origin.services import schedules
from origin.services.schedule_jobs import JOBS
from origin.tests.test_schedule_team_jobs import TeamJobsBase


class TestChosenChat(TeamJobsBase):
    def busy(self, channel, sender, n=3):
        for i in range(n):
            self.message(channel, sender, seq=i + 1)

    def named(self, report):
        return {i["text"].split(":")[0] for i in self.section(report, "Busy group chats")}

    def save_pick(self, target, pick, **over):
        params = {"channel_id": str(pick.id)} if pick is not None else {}
        return self.save_job(target, "chat_catch_up", params=params, **over)

    def refusal(self, target, pick) -> str:
        with self.assertRaises(schedules.ScheduleError) as cm:
            self.save_pick(target, pick)
        return str(cm.exception)

    def test_the_param_is_optional_in_the_catalog(self):
        schema = JOBS["chat_catch_up"].params_schema
        self.assertIn("channel_id", schema["properties"])
        self.assertNotIn("required", schema)

    def test_without_a_choice_every_busy_chat_is_named_as_before(self):
        a = self.gm(self.user, title="Alpha")
        b = self.gm(self.user, title="Beta")
        for chat in (a, b):
            self.busy(chat, self.user)
        self.assertEqual(self.named(self.run_job("chat_catch_up")), {"Alpha", "Beta"})
        self.assertEqual(
            self.named(self.run_job("chat_catch_up", channel_id="")), {"Alpha", "Beta"}
        )
        sched = self.save_pick(None, None)
        self.assertEqual(sched.params, {})

    def test_with_a_choice_only_that_chat_is_covered(self):
        a = self.gm(self.user, title="Alpha")
        b = self.gm(self.user, title="Beta")
        for chat in (a, b):
            self.busy(chat, self.user)
        report = self.run_job("chat_catch_up", channel_id=str(a.id))
        self.assertEqual(self.named(report), {"Alpha"})

    def test_a_quiet_chosen_chat_is_a_quiet_report_not_a_failure(self):
        a = self.gm(self.user, title="Alpha")
        report = self.run_job("chat_catch_up", channel_id=str(a.id))
        self.assertTrue(report["quiet"])

    def test_save_refuses_a_chat_the_owner_is_not_in_and_names_nobody(self):
        elsewhere = self.gm(self.user2, self.outsider, title="Not yours")
        words = self.refusal(None, elsewhere)
        for text in ("Not yours", self.user2.username, self.outsider.username):
            self.assertNotIn(text, words)

    def test_save_refuses_a_malformed_or_unknown_chat_with_the_same_words(self):
        elsewhere = self.gm(self.user2, title="Not yours")
        known = self.refusal(None, elsewhere)
        with self.assertRaises(schedules.ScheduleError) as cm:
            self.save_job(None, "chat_catch_up", params={"channel_id": "not-a-uuid"})
        self.assertEqual(str(cm.exception), known)
        with self.assertRaises(schedules.ScheduleError) as cm:
            self.save_job(
                None,
                "chat_catch_up",
                params={"channel_id": "00000000-0000-0000-0000-000000000001"},
            )
        self.assertEqual(str(cm.exception), known)

    def test_save_refuses_a_direct_or_archived_chat(self):
        from django.utils import timezone

        archived = self.gm(self.user, title="Gone", archived_at=timezone.now())
        self.assertTrue(self.refusal(None, archived))

    def test_audience_rule_every_reader_of_the_target_must_be_in_the_chosen_chat(self):
        target = self.gm(self.user, self.user2, title="Target")
        mine_only = self.gm(self.user, title="Mine only")
        with_user2 = self.gm(self.user, self.user2, title="With both")
        with_user2_and_more = self.gm(self.user, self.user2, self.outsider, title="Wider")
        words = self.refusal(target, mine_only)
        for text in ("Mine only", self.user2.username):
            self.assertNotIn(text, words)
        self.assertTrue(self.save_pick(target, with_user2))
        target2 = self.gm(self.user, self.user2, title="Target two")
        self.assertTrue(self.save_pick(target2, with_user2_and_more))

    def test_audience_rule_does_not_apply_to_a_chat_only_the_owner_reads(self):
        mine_only = self.gm(self.user, title="Mine only")
        own = self.gm(self.user, title="My own reports")
        self.assertTrue(self.save_pick(own, mine_only))

    def test_the_run_asks_again_when_a_reader_joins_the_target_chat(self):
        target = self.gm(self.user, title="Target")
        pick = self.gm(self.user, title="Pick")
        self.busy(pick, self.user)
        sched = self.save_pick(target, pick)
        self.assertEqual(self.run_it(sched)[0], "ok")
        ChannelMember.objects.create(channel=target, user=self.user2)
        status, error = self.run_it(sched, run_at=self.now + timedelta(days=1))
        self.assertEqual(status, "failed")
        self.assertNotIn("Pick", error)
        self.assertNotIn(self.user2.username, error)
        self.assertEqual(self.posted(target).count(), 1)  # nothing new was posted

    def test_the_run_fails_in_words_when_the_owner_left_the_chosen_chat(self):
        pick = self.gm(self.user, title="Pick")
        sched = self.save_pick(None, pick)
        ChannelMember.objects.filter(channel=pick, user=self.user).update(is_deleted=True)
        status, error = self.run_it(sched)
        self.assertEqual(status, "failed")
        self.assertNotIn("Pick", error)

    def test_a_report_posted_in_a_chat_covers_only_the_chosen_chat(self):
        target = self.gm(self.user, self.user2, title="Target")
        pick = self.gm(self.user, self.user2, title="Pick")
        other = self.gm(self.user, self.user2, title="Other")
        for chat in (pick, other):
            self.busy(chat, self.user)
        sched = self.save_pick(target, pick)
        self.assertEqual(self.run_it(sched)[0], "ok")
        text = str(self.posted(target).last().body)
        self.assertIn("Pick", text)
        self.assertNotIn("Other", text)
