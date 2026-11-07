"""Retention of scheduled reports in the Inbox (MIP-1159): the rule, the command, the cron."""

import re
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.utils import timezone

from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.common.ops_models import CronHeartbeat
from origin.models.common.schedule_models import MemberSchedule
from origin.services import ops_alerts, schedule_delivery
from origin.services import schedule_report_retention as retention
from origin.tests.test_schedules import ScheduleBase

DAY = timedelta(days=1)
REPO = Path(__file__).resolve().parents[2]


class RetentionBase(ScheduleBase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def report(
        self,
        age=timedelta(hours=1),
        *,
        receiver=None,
        schedule_id="sched-a",
        read=True,
        deleted=False,
        item_type=ITEM_TYPE_GENOS_REPORT,
    ):
        item = InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=receiver or self.user,
            item_type=item_type,
            item_body={"title": "t", "text": "s", "sections": []},
            item_optionals={"kind": "schedule_report", "schedule_id": schedule_id},
            request_status="",
            is_read=read,
            is_deleted=deleted,
        )
        # `ts_created_at` is auto_now_add: backdate with an UPDATE.
        InboxItems.objects.filter(pk=item.pk).update(ts_created_at=self.now - age)
        return item

    def ids(self, **filters):
        return set(InboxItems.objects.filter(**filters).values_list("item_id", flat=True))

    def prune(self, **kw):
        return retention.prune(now=self.now, **kw)


class ConstantsTests(RetentionBase):
    def test_the_documented_numbers(self):
        # docs: operations/SCHEDULER.md "Retention". Change both together.
        self.assertEqual(retention.REPORT_RETENTION_DAYS, 30)
        self.assertEqual(retention.MAX_REPORTS_PER_SCHEDULE, 60)


class AgeTests(RetentionBase):
    def test_old_removed_recent_kept(self):
        old = self.report(31 * DAY)
        recent = self.report(29 * DAY)
        result = self.prune()
        self.assertFalse(InboxItems.objects.filter(pk=old.pk).exists())
        self.assertTrue(InboxItems.objects.filter(pk=recent.pk).exists())
        self.assertEqual((result.expired, result.over_cap, result.deleted), (1, 0, 1))

    def test_exactly_the_window_old_is_kept_and_a_second_past_it_is_not(self):
        edge = self.report(retention.REPORT_RETENTION_DAYS * DAY)
        past = self.report(retention.REPORT_RETENTION_DAYS * DAY + timedelta(seconds=1))
        self.prune()
        self.assertTrue(InboxItems.objects.filter(pk=edge.pk).exists())
        self.assertFalse(InboxItems.objects.filter(pk=past.pk).exists())

    def test_an_old_report_goes_whether_read_or_not(self):
        unread = self.report(45 * DAY, read=False)
        read = self.report(45 * DAY, read=True)
        self.prune()
        self.assertFalse(InboxItems.objects.filter(pk__in=[unread.pk, read.pk]).exists())

    def test_a_deleted_old_report_goes_too(self):
        gone = self.report(45 * DAY, deleted=True)
        self.prune()
        self.assertFalse(InboxItems.objects.filter(pk=gone.pk).exists())


class CapTests(RetentionBase):
    def many(self, n, **kw):
        # Newest first by construction: index 0 is the newest, one hour apart.
        return [self.report(timedelta(hours=i + 1), **kw) for i in range(n)]

    def test_exactly_the_cap_is_kept(self):
        rows = self.many(retention.MAX_REPORTS_PER_SCHEDULE)
        result = self.prune()
        self.assertEqual(self.ids(), {r.pk for r in rows})
        self.assertEqual(result.deleted, 0)

    def test_the_one_beyond_the_cap_goes_and_it_is_the_oldest(self):
        rows = self.many(retention.MAX_REPORTS_PER_SCHEDULE + 1)
        result = self.prune()
        self.assertEqual(self.ids(), {r.pk for r in rows[:-1]})
        self.assertEqual((result.expired, result.over_cap, result.deleted), (0, 1, 1))

    def test_the_cap_is_per_schedule(self):
        a = self.many(retention.MAX_REPORTS_PER_SCHEDULE, schedule_id="sched-a")
        b = self.many(retention.MAX_REPORTS_PER_SCHEDULE, schedule_id="sched-b")
        self.prune()
        self.assertEqual(self.ids(), {r.pk for r in a + b})
        extra = self.report(timedelta(days=10), schedule_id="sched-b")
        self.prune()
        self.assertEqual(self.ids(), {r.pk for r in a + b})
        self.assertFalse(InboxItems.objects.filter(pk=extra.pk).exists())

    def test_another_members_reports_of_the_same_schedule_id_do_not_count(self):
        mine = self.many(retention.MAX_REPORTS_PER_SCHEDULE, schedule_id="shared-id")
        theirs = self.many(30, schedule_id="shared-id", receiver=self.user2)
        self.prune()
        self.assertEqual(self.ids(), {r.pk for r in mine + theirs})

    def test_each_member_is_capped_on_their_own(self):
        mine = self.many(retention.MAX_REPORTS_PER_SCHEDULE + 2, schedule_id="shared-id")
        theirs = self.many(
            retention.MAX_REPORTS_PER_SCHEDULE + 1, schedule_id="shared-id", receiver=self.user2
        )
        self.prune()
        self.assertEqual(self.ids(receiver=self.user), {r.pk for r in mine[:-2]})
        self.assertEqual(self.ids(receiver=self.user2), {r.pk for r in theirs[:-1]})

    def test_member_deleted_rows_still_count_for_the_rank(self):
        # Ranking over every row keeps the newest 60 whatever their state, so a repeated run
        # (schedule_delivery's dedupe) always finds the newest row.
        deleted_newest = self.report(timedelta(minutes=1), deleted=True)
        rest = self.many(retention.MAX_REPORTS_PER_SCHEDULE)
        self.prune()
        self.assertTrue(InboxItems.objects.filter(pk=deleted_newest.pk).exists())
        self.assertFalse(InboxItems.objects.filter(pk=rest[-1].pk).exists())

    def test_the_newest_report_is_never_pruned_by_the_count(self):
        rows = self.many(retention.MAX_REPORTS_PER_SCHEDULE + 20)
        self.prune()
        self.assertTrue(InboxItems.objects.filter(pk=rows[0].pk).exists())


class UnreadYoungTests(RetentionBase):
    def test_an_unread_young_report_beyond_the_cap_is_kept(self):
        n = retention.MAX_REPORTS_PER_SCHEDULE
        rows = [self.report(timedelta(hours=i + 1)) for i in range(n)]
        unread_over = self.report(timedelta(hours=n + 1), read=False)
        read_over = self.report(timedelta(hours=n + 2), read=True)
        result = self.prune()
        self.assertTrue(InboxItems.objects.filter(pk=unread_over.pk).exists())
        self.assertFalse(InboxItems.objects.filter(pk=read_over.pk).exists())
        self.assertEqual(self.ids(), {r.pk for r in rows} | {unread_over.pk})
        self.assertEqual(result.over_cap, 1)

    def test_protecting_one_does_not_shift_the_rank_of_the_others(self):
        # 62 reports; the 61st (rank 61) is unread, the 62nd read: only the 62nd goes, the
        # newest 60 stay. A rank taken over the unprotected rows alone would delete a 61st
        # row more or fewer.
        n = retention.MAX_REPORTS_PER_SCHEDULE
        rows = [self.report(timedelta(hours=i + 1)) for i in range(n)]
        keep = self.report(timedelta(hours=n + 1), read=False)
        drop = self.report(timedelta(hours=n + 2))
        self.prune()
        self.assertEqual(self.ids(), {r.pk for r in rows} | {keep.pk})
        self.assertFalse(InboxItems.objects.filter(pk=drop.pk).exists())

    def test_an_unread_report_the_member_deleted_is_not_protected(self):
        n = retention.MAX_REPORTS_PER_SCHEDULE
        for i in range(n):
            self.report(timedelta(hours=i + 1))
        gone = self.report(timedelta(hours=n + 1), read=False, deleted=True)
        self.prune()
        self.assertFalse(InboxItems.objects.filter(pk=gone.pk).exists())

    def test_an_unread_report_past_the_window_is_not_protected(self):
        old_unread = self.report(31 * DAY, read=False)
        self.prune()
        self.assertFalse(InboxItems.objects.filter(pk=old_unread.pk).exists())

    def test_an_unread_young_report_below_the_cap_is_obviously_kept(self):
        row = self.report(timedelta(days=2), read=False)
        self.prune()
        self.assertTrue(InboxItems.objects.filter(pk=row.pk).exists())


class OtherTypesTests(RetentionBase):
    def test_no_other_item_type_is_touched_even_old_and_with_the_same_optionals(self):
        kept = []
        for item_type in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9):
            for i in range(retention.MAX_REPORTS_PER_SCHEDULE + 2):
                kept.append(self.report(45 * DAY + timedelta(hours=i), item_type=item_type))
        self.prune()
        self.assertEqual(self.ids(), {r.pk for r in kept})

    def test_other_types_are_not_in_the_counts_a_dry_run_reports(self):
        self.report(45 * DAY, item_type=9)
        self.report(45 * DAY, item_type=6)
        self.report(45 * DAY)
        result = self.prune(dry_run=True)
        self.assertEqual((result.expired, result.over_cap, result.matched), (1, 0, 1))

    def test_other_types_do_not_count_toward_a_schedules_cap(self):
        n = retention.MAX_REPORTS_PER_SCHEDULE
        rows = [self.report(timedelta(days=1, hours=i)) for i in range(n)]
        for i in range(10):  # newer than every report
            self.report(timedelta(minutes=i + 1), item_type=9)
        self.prune()
        self.assertEqual(self.ids(item_type=ITEM_TYPE_GENOS_REPORT), {r.pk for r in rows})

    def test_the_schedule_and_its_last_result_are_left_alone(self):
        sched = self.make()
        item = schedule_delivery.deliver(
            sched, {"title": "Daily", "summary": "s", "sections": []}, run_at=self.now
        )
        sched.refresh_from_db()
        before = dict(sched.last_result)
        InboxItems.objects.filter(pk=item.pk).update(ts_created_at=self.now - 40 * DAY)
        self.prune()
        self.assertFalse(InboxItems.objects.filter(pk=item.pk).exists())
        sched.refresh_from_db()
        self.assertEqual(sched.last_result, before)
        self.assertTrue(MemberSchedule.objects.filter(pk=sched.pk).exists())


class RunTests(RetentionBase):
    def test_a_second_run_finds_nothing(self):
        for i in range(retention.MAX_REPORTS_PER_SCHEDULE + 5):
            self.report(timedelta(hours=i + 1))
        self.report(40 * DAY)
        first = self.prune()
        self.assertEqual(first.deleted, 6)
        before = self.ids()
        second = self.prune()
        self.assertEqual((second.deleted, second.matched), (0, 0))
        self.assertEqual(self.ids(), before)

    def test_a_dry_run_counts_and_deletes_nothing(self):
        for i in range(retention.MAX_REPORTS_PER_SCHEDULE + 2):
            self.report(timedelta(hours=i + 1))
        self.report(40 * DAY)
        before = self.ids()
        result = self.prune(dry_run=True)
        self.assertEqual((result.expired, result.over_cap, result.deleted), (1, 2, 0))
        self.assertEqual(self.ids(), before)

    def test_it_deletes_in_batches(self):
        for i in range(5):
            self.report(40 * DAY + timedelta(hours=i))
        with mock.patch.object(retention, "_BATCH", 2):
            result = self.prune()
        self.assertEqual(result.deleted, 5)
        self.assertEqual(self.ids(), set())

    def test_nothing_to_do_is_fine(self):
        self.assertEqual(self.prune().deleted, 0)

    def test_an_expired_row_over_the_cap_is_counted_once(self):
        for i in range(retention.MAX_REPORTS_PER_SCHEDULE):
            self.report(timedelta(hours=i + 1))
        self.report(40 * DAY)  # both old and beyond the cap
        result = self.prune()
        self.assertEqual((result.expired, result.over_cap, result.deleted), (1, 0, 1))


class CommandTests(RetentionBase):
    def call(self, *args):
        out = StringIO()
        # `--heartbeat` closes old connections, which would end the test's connection.
        with mock.patch("origin.services.ops_alerts.close_old_connections"):
            call_command("prune_schedule_reports", *args, stdout=out)
        return out.getvalue()

    def test_it_deletes_and_says_how_many(self):
        self.report(40 * DAY)
        keep = self.report(1 * DAY)
        out = self.call()
        self.assertIn("Deleted 1 scheduled reports", out)
        self.assertEqual(self.ids(), {keep.pk})

    def test_dry_run_writes_nothing(self):
        self.report(40 * DAY)
        out = self.call("--dry-run")
        self.assertIn("[dry-run] would delete 1 scheduled reports", out)
        self.assertEqual(len(self.ids()), 1)

    def test_the_heartbeat_records_success(self):
        self.report(40 * DAY)
        self.call("--heartbeat", "schedule-report-prune")
        beat = CronHeartbeat.objects.get(name="schedule-report-prune")
        self.assertIsNotNone(beat.last_succeeded_at)
        self.assertEqual(beat.last_error, "")

    def test_a_fault_fails_the_run_and_the_heartbeat(self):
        with mock.patch.object(retention, "prune", side_effect=RuntimeError("db gone")):
            with self.assertRaises(RuntimeError):
                self.call("--heartbeat", "schedule-report-prune")
        beat = CronHeartbeat.objects.get(name="schedule-report-prune")
        self.assertIsNone(beat.last_succeeded_at)
        self.assertNotEqual(beat.last_error, "")


class CronWiringTests(RetentionBase):
    def test_a_daily_railway_service_beats_under_its_own_name_and_is_watched(self):
        text = (REPO / ".railway" / "railway.ts").read_text()
        block = re.search(r'service\("schedule-report-prune", \{.*?\n  \}\);', text, re.S).group(0)
        self.assertIn('cronSchedule: "15 4 * * *"', block)
        self.assertIn(
            'startCommand: "python /app/manage.py prune_schedule_reports '
            '--heartbeat schedule-report-prune"',
            block,
        )
        self.assertIn('restartPolicyType: "NEVER"', block)
        self.assertEqual(ops_alerts.CRON_SCHEDULES["schedule-report-prune"], timedelta(hours=26))
        # A new service cannot have a toml.
        self.assertFalse((REPO / "railway-schedule-report-prune.toml").exists())
