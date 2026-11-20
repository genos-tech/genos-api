"""The notices 5 days and 1 day before the first month of Core ends (MIP-1764).

What is pinned, and why each is its own test:

  * Each running trial is told once at 5 days and once at 1 day, never twice (the stamp is the claim),
    and a trial that is already inside the last day is told only the 1-day notice.
  * Not told: someone the trial no longer helps (a payer, a paying team's member, an ended trial,
    an inactive account); that stage is stamped so the row is not re-judged every minute, and the
    OTHER stage is judged at its own moment.
  * The offer's switch is not consulted: a running trial is announced with the offer off.
  * The notice reaches the person: an inbox item the inbox endpoint returns for the team, a push and
    an email through the existing inbox path (so their own switches apply), in their own language
    and zone.
  * A crash after the claim rolls the stamp back with the item, and the next pass delivers.
  * The pass is two indexed queries (one per stage) when nothing is due, and it lives in the existing reminder tick.
"""

from __future__ import annotations

import os
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from origin.models.common.inbox_models import InboxItems
from origin.models.common.notification_models import EmailNotificationEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.trial_models import CoreTrial
from origin.models.common.user_models import CustomUser
from origin.services import first_month_trial
from origin.services import first_month_trial_notices as notices
from origin.tests.test_base import BaseAPITestCase

INBOX = "/api/v2/inbox/"


class NoticeBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # The push transport is off in tests (no VAPID keys); patch the two seams the notice uses.
        vapid = mock.patch("origin.services.webpush_dispatch.vapid_configured", return_value=True)
        queue = mock.patch("origin.services.webpush_dispatch._queue_push")
        vapid.start()
        self.push = queue.start()
        self.addCleanup(mock.patch.stopall)

    def trial(self, *, left: timedelta, user: CustomUser | None = None) -> CoreTrial:
        """A running trial with `left` to go (the tick reads the real clock)."""
        user = user or self.user
        now = timezone.now()
        return CoreTrial.objects.create(
            user=user, started_at=now - timedelta(days=30) + left, ends_at=now + left
        )

    def run_pass(self, **kwargs) -> dict:
        """One tick pass, with the transaction's on-commit work (push, email) run."""
        with self.captureOnCommitCallbacks(execute=True):
            return notices.fire_due(**kwargs)

    def items(self, user=None):
        return InboxItems.objects.filter(
            receiver=user or self.user, item_optionals__kind=notices.KIND
        )


class OnceEachTests(NoticeBase):
    def test_the_five_day_notice_is_filed_once_and_a_second_pass_repeats_nothing(self):
        self.trial(left=timedelta(days=4, hours=23))
        first = self.run_pass()
        second = self.run_pass()
        self.assertEqual((first["sent"], second["sent"], second["due"]), (1, 0, 0))
        item = self.items().get()
        self.assertEqual(item.item_optionals["stage"], notices.STAGE_5D)
        self.assertEqual(self.push.call_count, 1)
        trial = CoreTrial.objects.get(user=self.user)
        self.assertIsNotNone(trial.notified_5d_at)
        self.assertIsNone(trial.notified_1d_at)

    def test_both_notices_one_each_as_the_end_comes_closer(self):
        trial = self.trial(left=timedelta(days=4, hours=23))
        self.run_pass()
        self.run_pass()
        # A day later: 23 hours left.
        CoreTrial.objects.filter(pk=trial.pk).update(ends_at=timezone.now() + timedelta(hours=23))
        self.run_pass()
        self.run_pass()
        stages = sorted(self.items().values_list("item_optionals__stage", flat=True))
        self.assertEqual(stages, [notices.STAGE_1D, notices.STAGE_5D])
        self.assertEqual(self.push.call_count, 2)
        trial.refresh_from_db()
        self.assertIsNotNone(trial.notified_5d_at)
        self.assertIsNotNone(trial.notified_1d_at)

    def test_a_trial_with_more_than_five_days_left_is_not_due(self):
        self.trial(left=timedelta(days=6))
        self.assertEqual(self.run_pass()["due"], 0)
        self.assertFalse(self.items().exists())

    def test_inside_the_last_day_with_nothing_sent_only_the_one_day_notice_goes(self):
        """The cron was down, or the trial began late: "5 days" would now be false."""
        trial = self.trial(left=timedelta(hours=20))
        self.run_pass()
        self.run_pass()
        self.assertEqual(
            list(self.items().values_list("item_optionals__stage", flat=True)), [notices.STAGE_1D]
        )
        trial.refresh_from_db()
        self.assertIsNotNone(trial.notified_5d_at)  # superseded, never sent afterwards
        self.assertIsNotNone(trial.notified_1d_at)

    def test_an_ended_trial_is_not_told(self):
        self.trial(left=-timedelta(minutes=1))
        self.assertEqual(self.run_pass()["due"], 0)
        self.assertFalse(self.items().exists())
        self.push.assert_not_called()

    def test_the_offers_switch_is_not_consulted(self):
        self.trial(left=timedelta(days=4))
        env = {k: v for k, v in os.environ.items() if k != first_month_trial.SWITCH_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(first_month_trial.offer_is_open())
            self.assertEqual(self.run_pass()["sent"], 1)
        self.assertEqual(self.items().count(), 1)

    def test_a_crash_after_the_claim_rolls_the_stamp_back_and_the_next_pass_delivers(self):
        self.trial(left=timedelta(days=4))
        with mock.patch.object(InboxItems.objects, "create", side_effect=RuntimeError("db gone")):
            with self.assertLogs("origin.services.first_month_trial_notices", "ERROR"):
                counts = self.run_pass()
        self.assertEqual(counts["sent"], 0)
        self.assertIsNone(CoreTrial.objects.get(user=self.user).notified_5d_at)
        self.assertFalse(self.items().exists())
        self.assertEqual(self.run_pass()["sent"], 1)
        self.assertEqual(self.items().count(), 1)

    def test_a_pass_that_lost_the_race_files_nothing(self):
        trial = self.trial(left=timedelta(days=4))
        stale = notices.due_trials()[0]
        CoreTrial.objects.filter(pk=trial.pk).update(notified_5d_at=timezone.now())
        self.assertEqual(notices.fire(stale), notices.SKIP_RACE)
        self.assertFalse(self.items().exists())


class WhoIsToldTests(NoticeBase):
    def test_a_person_on_a_paid_plan_is_not_told(self):
        CustomUser.objects.filter(pk=self.user.pk).update(tier="core")
        self.trial(left=timedelta(days=4))
        counts = self.run_pass()
        self.assertEqual((counts["sent"], counts["skipped"]), (0, 1))
        self.assertFalse(self.items().exists())
        self.push.assert_not_called()
        # Handled, so it is not re-judged every minute.
        self.assertEqual(self.run_pass()["due"], 0)

    def test_a_member_of_a_paying_team_is_not_told(self):
        TeamMaster.objects.filter(pk=self.team.pk).update(plan="pro")
        self.trial(left=timedelta(days=4))
        self.assertEqual(self.run_pass()["sent"], 0)
        self.assertFalse(self.items().exists())

    def test_the_other_stage_is_judged_at_its_own_moment(self):
        """Paid at day 25 and lapsed before day 29: the 1-day notice is still true and goes."""
        user = self.user
        trial = self.trial(left=timedelta(days=4))
        CustomUser.objects.filter(pk=user.pk).update(tier="core")
        self.assertEqual(self.run_pass()["sent"], 0)
        CustomUser.objects.filter(pk=user.pk).update(tier="free")
        CoreTrial.objects.filter(pk=trial.pk).update(ends_at=timezone.now() + timedelta(hours=20))
        self.assertEqual(self.run_pass()["sent"], 1)
        self.assertEqual(self.items().get().item_optionals["stage"], notices.STAGE_1D)

    def test_an_inactive_or_deleted_account_is_not_told(self):
        for flag in ("is_active", "is_deleted"):
            CoreTrial.objects.all().delete()
            CustomUser.objects.filter(pk=self.user.pk).update(is_active=True, is_deleted=False)
            CustomUser.objects.filter(pk=self.user.pk).update(**{flag: flag == "is_deleted"})
            self.trial(left=timedelta(days=4))
            self.assertEqual(self.run_pass()["sent"], 0, flag)
        self.assertFalse(self.items().exists())

    def test_a_person_with_no_active_team_has_no_inbox_to_put_it_in(self):
        TeamMembers.objects.filter(attendee=self.user).update(is_deleted=True)
        self.trial(left=timedelta(days=4))
        self.assertEqual(self.run_pass()["sent"], 0)
        self.assertFalse(InboxItems.objects.filter(receiver=self.user).exists())
        self.assertEqual(self.run_pass()["due"], 0)


class DeliveryTests(NoticeBase):
    def test_the_inbox_endpoint_returns_the_notice_for_the_team(self):
        self.trial(left=timedelta(days=4))
        self.run_pass()
        self.authenticate()
        res = self.client.get(
            INBOX, {"team_id": str(self.team.team_id), "user_id": str(self.user.id)}
        )
        self.assertEqual(res.status_code, 200)
        rows = [
            r
            for r in res.json()["data"]["items"]
            if (r.get("itemOptionals") or {}).get("kind") == notices.KIND
        ]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["itemType"], 0)  # an activity the existing card renders
        self.assertEqual(row["requestStatus"], "")
        self.assertFalse(row["isRead"])

    def test_the_card_says_what_the_acceptance_criteria_name(self):
        trial = self.trial(left=timedelta(days=4))
        self.run_pass()
        item = self.items().get()
        text = " ".join(
            part.get("text", "")
            for block in item.item_body
            for part in block["content"]
            if part["type"] == "text"
        )
        self.assertIn(notices.format_date(trial.ends_at, self.user, "en"), text)
        self.assertIn("returns to the Free plan", text)
        self.assertIn("data stays", text)
        self.assertIn("connected apps pause", text)
        link = item.item_body[-1]["content"][0]
        self.assertEqual(link["type"], "link")
        self.assertTrue(link["href"].endswith("/workspace/plans"))
        self.assertEqual(item.item_optionals["href"], "/workspace/plans")
        self.assertIsNone(item.sender_id)

    def test_push_and_email_go_through_the_inbox_path(self):
        self.trial(left=timedelta(days=4))
        with override_settings(EMAIL_NOTIFICATIONS_ENABLED=True):
            self.run_pass()
        kwargs = self.push.call_args.kwargs
        self.assertEqual(kwargs["category"], "inbox")  # the person's own inbox switch decides
        self.assertEqual(str(kwargs["recipient_id"]), str(self.user.id))
        self.assertEqual(kwargs["title"], "Your free month of Core ends in 5 days")
        self.assertIn("ends on", kwargs["body"])
        email = EmailNotificationEvent.objects.get(user_id=self.user.id)
        self.assertEqual(email.category, "inbox")
        self.assertEqual(email.title, "Your free month of Core ends in 5 days")

    def test_nothing_is_pushed_or_emailed_when_the_item_rolled_back(self):
        self.trial(left=timedelta(days=4))
        with override_settings(EMAIL_NOTIFICATIONS_ENABLED=True):
            with mock.patch.object(InboxItems.objects, "create", side_effect=RuntimeError("x")):
                with self.assertLogs("origin.services.first_month_trial_notices", "ERROR"):
                    self.run_pass()
        self.push.assert_not_called()
        self.assertFalse(EmailNotificationEvent.objects.exists())

    def test_one_item_in_each_team_but_one_push_and_one_email(self):
        other = TeamMaster.objects.create(
            team_name="Second", team_email="two@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        self.trial(left=timedelta(days=4))
        with override_settings(EMAIL_NOTIFICATIONS_ENABLED=True):
            self.run_pass()
        self.assertEqual(
            set(self.items().values_list("team_id", flat=True)), {self.team.team_id, other.team_id}
        )
        self.assertEqual(self.push.call_count, 1)
        self.assertEqual(EmailNotificationEvent.objects.filter(user_id=self.user.id).count(), 1)

    def test_a_person_with_push_and_email_off_still_gets_the_inbox_item(self):
        """The item is in the inbox whatever the switches say; they gate only the push and email
        (`should_push` / `should_email` run inside the existing dispatchers, tested there)."""
        from origin.models.common.notification_models import NotificationPreference

        NotificationPreference.objects.update_or_create(
            user=self.user, defaults={"push_enabled": False, "email_enabled": False}
        )
        self.trial(left=timedelta(days=4))
        self.assertEqual(self.run_pass()["sent"], 1)
        self.assertEqual(self.items().count(), 1)


class WordingTests(NoticeBase):
    def test_japanese_in_the_persons_zone(self):
        CustomUser.objects.filter(pk=self.user.pk).update(language="ja", timezone="Asia/Tokyo")
        self.user.refresh_from_db()
        trial = self.trial(left=timedelta(days=4))
        # 23:30 UTC on the 10th is 08:30 on the 11th in Tokyo: the UTC date would be a day off.
        ends = timezone.now().replace(hour=23, minute=30, second=0, microsecond=0) + timedelta(
            days=4
        )
        CoreTrial.objects.filter(pk=trial.pk).update(ends_at=ends)
        self.run_pass()
        item = self.items().get()
        text = " ".join(
            p["text"] for b in item.item_body for p in b["content"] if p["type"] == "text"
        )
        local = ends + timedelta(hours=9)
        self.assertIn(f"{local.year}年{local.month}月{local.day}日", text)
        self.assertIn("無料プランに戻ります", text)
        self.assertEqual(
            self.push.call_args.kwargs["title"], "Core の無料期間はあと5日で終わります"
        )

    def test_an_unknown_language_reads_in_english(self):
        for raw in (None, "", "xx", "EN-GB"):
            self.user.language = raw
            self.assertEqual(notices.locale_for(self.user), "en", raw)
        self.user.language = "ja-JP"
        self.assertEqual(notices.locale_for(self.user), "ja")

    def test_every_frontend_locale_has_the_whole_copy(self):
        self.assertEqual(set(notices.LOCALES), {"en", "ja", "es", "fr", "hi", "zh", "ar"})
        keys = set(notices._COPY["en"])
        for locale, copy in notices._COPY.items():
            self.assertEqual(set(copy), keys, locale)
            self.assertIn("{date}", copy["ends"], locale)
            for key in ("title_5d", "title_1d", "after", "plans"):
                self.assertTrue(copy[key].strip(), (locale, key))
                self.assertNotIn("{", copy[key], (locale, key))
            self.user.language = locale
            title, body, push_body = notices.compose(
                notices.STAGE_1D, self.user, timezone.now() + timedelta(hours=5)
            )
            self.assertNotIn("{", push_body, locale)
            self.assertEqual(title, copy["title_1d"])
            self.assertEqual(len(body), 4)


class TickTests(NoticeBase):
    def test_nothing_due_costs_two_indexed_queries(self):
        with self.assertNumQueries(2):
            self.assertEqual(notices.fire_due(), {"sent": 0, "skipped": 0, "due": 0})

    def test_the_existing_reminder_tick_runs_the_pass_and_reports_it(self):
        self.trial(left=timedelta(days=4))
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("message_reminder_tick", stdout=out)
        self.assertIn("trial_notice: 1 sent, 0 skipped (of 1 due)", out.getvalue())
        self.assertEqual(self.items().count(), 1)

    def test_dry_run_reports_and_files_nothing(self):
        self.trial(left=timedelta(days=4))
        out = StringIO()
        call_command("message_reminder_tick", "--dry-run", stdout=out)
        self.assertIn("1 first-month trial notice(s) due", out.getvalue())
        self.assertFalse(self.items().exists())
        self.assertIsNone(CoreTrial.objects.get(user=self.user).notified_5d_at)

    def test_a_failing_trial_pass_never_stops_the_reminders_before_it(self):
        out = StringIO()
        with mock.patch.object(notices, "fire_due", side_effect=RuntimeError("boom")):
            with self.assertLogs("origin.reminders", "ERROR"):
                try:
                    call_command("message_reminder_tick", stdout=out)
                except Exception:  # noqa: BLE001 - CronCommand turns the logged ERROR into a failed run
                    pass
        # Both reminder passes had already reported before the trial pass ran.
        self.assertIn("message_reminder:", out.getvalue())
        self.assertIn("todo_reminder:", out.getvalue())
