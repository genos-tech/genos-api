"""How long an AI-app notice is kept (MIP-1541): the rule, the command, the number.

An AI-app notice is an `Activity` row of `ActivityType.AI_APP` (`services/mcp_notice.py`) that
names up to five item titles. Nothing else in the feed is touched.
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType
from origin.services import ai_app_notice_retention as retention
from origin.services.v3_activity import SURFACE_AI_APP
from origin.tests.test_base import BaseAPITestCase

DAY = timedelta(days=1)
WINDOW = retention.AI_APP_NOTICE_RETENTION_DAYS * DAY
SECRET_TITLE = "Quarterly layoff plan"


class RetentionBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.now = timezone.now()

    def row(
        self, age, *, recipient=None, activity_type=ActivityType.AI_APP, surface=SURFACE_AI_APP
    ):
        activity = Activity.objects.create(
            team=self.team,
            recipient=recipient or self.user,
            actor=recipient or self.user,
            activity_type=activity_type,
            surface_type=surface,
            meta={"aiApp": True, "items": [{"title": SECRET_TITLE}]},
        )
        # `ts_created_at` is auto_now_add: backdate with an UPDATE.
        Activity.objects.filter(pk=activity.pk).update(ts_created_at=self.now - age)
        return activity

    def alive(self):
        return set(Activity.objects.values_list("pk", flat=True))


class ConstantTests(RetentionBase):
    def test_the_window_is_one_named_constant_in_whole_days(self):
        days = retention.AI_APP_NOTICE_RETENTION_DAYS
        self.assertIsInstance(days, int)
        self.assertGreater(days, 0)

    def test_the_service_reads_the_constant(self):
        old = self.row(11 * DAY)
        young = self.row(9 * DAY)
        with mock.patch.object(retention, "AI_APP_NOTICE_RETENTION_DAYS", 10):
            retention.prune(self.now, apply=True)
        self.assertEqual(self.alive(), {young.pk})
        self.assertNotIn(old.pk, self.alive())


class AgeTests(RetentionBase):
    def test_older_than_the_window_goes_and_younger_stays(self):
        old = self.row(WINDOW + DAY)
        young = self.row(WINDOW - DAY)
        result = retention.prune(self.now, apply=True)
        self.assertEqual(self.alive(), {young.pk})
        self.assertNotIn(old.pk, self.alive())
        self.assertEqual((result.matched, result.deleted, result.dry_run), (1, 1, False))

    def test_exactly_the_window_old_stays_and_a_second_past_it_goes(self):
        edge = self.row(WINDOW)
        past = self.row(WINDOW + timedelta(seconds=1))
        retention.prune(self.now, apply=True)
        self.assertIn(edge.pk, self.alive())
        self.assertNotIn(past.pk, self.alive())

    def test_read_or_unread_makes_no_difference(self):
        a = self.row(WINDOW + 30 * DAY)
        b = self.row(WINDOW + 30 * DAY)
        Activity.objects.filter(pk=b.pk).update(is_read=True)
        retention.prune(self.now, apply=True)
        self.assertFalse(Activity.objects.filter(pk__in=[a.pk, b.pk]).exists())

    def test_every_member_is_covered_not_only_one(self):
        mine = self.row(WINDOW + 30 * DAY)
        theirs = self.row(WINDOW + 30 * DAY, recipient=self.user2)
        retention.prune(self.now, apply=True)
        self.assertFalse(Activity.objects.filter(pk__in=[mine.pk, theirs.pk]).exists())

    def test_a_second_run_finds_nothing(self):
        self.row(WINDOW + 30 * DAY)
        retention.prune(self.now, apply=True)
        again = retention.prune(self.now, apply=True)
        self.assertEqual((again.matched, again.deleted), (0, 0))

    def test_a_backlog_is_deleted_in_batches(self):
        rows = [self.row(WINDOW + 30 * DAY) for _ in range(5)]
        with mock.patch.object(retention, "_BATCH", 2):
            result = retention.prune(self.now, apply=True)
        self.assertEqual(result.deleted, 5)
        self.assertFalse(Activity.objects.filter(pk__in=[r.pk for r in rows]).exists())


class OnlyAiAppNoticesTests(RetentionBase):
    def test_other_activity_types_of_any_age_are_kept(self):
        kept = [
            self.row(WINDOW + 300 * DAY, activity_type=t, surface=None)
            for t in (
                ActivityType.THREAD_REPLY,
                ActivityType.REACTION,
                ActivityType.MENTION,
                ActivityType.TASK_ASSIGN,
                ActivityType.MESSAGE,
            )
        ]
        # a task-body mention on a surface: same table, not an AI-app notice
        kept.append(self.row(WINDOW + 300 * DAY, activity_type=ActivityType.MENTION, surface=5))
        gone = self.row(WINDOW + 300 * DAY)
        retention.prune(self.now, apply=True)
        self.assertEqual(self.alive(), {a.pk for a in kept})
        self.assertNotIn(gone.pk, self.alive())

    def test_the_expired_set_and_the_dry_run_count_hold_only_ai_app_notices(self):
        gone = self.row(WINDOW + 30 * DAY)
        self.row(WINDOW + 30 * DAY, activity_type=ActivityType.MENTION, surface=None)
        self.row(WINDOW + 30 * DAY, activity_type=ActivityType.MESSAGE, surface=None)
        self.assertEqual(set(retention.expired(self.now).values_list("pk", flat=True)), {gone.pk})
        result = retention.prune(self.now)
        self.assertEqual((result.matched, result.deleted), (1, 0))

    def test_a_young_notice_is_never_taken(self):
        young = self.row(timedelta(minutes=5))
        retention.prune(self.now, apply=True)
        self.assertIn(young.pk, self.alive())


class DryRunTests(RetentionBase):
    def test_without_apply_nothing_is_written(self):
        old = self.row(WINDOW + 30 * DAY)
        result = retention.prune(self.now)
        self.assertEqual((result.matched, result.deleted, result.dry_run), (1, 0, True))
        self.assertIn(old.pk, self.alive())


class CommandTests(RetentionBase):
    def run_command(self, *args):
        out = StringIO()
        call_command("prune_ai_app_notices", *args, stdout=out)
        return out.getvalue()

    def test_the_default_is_a_dry_run(self):
        old = self.row(WINDOW + 30 * DAY)
        text = self.run_command()
        self.assertIn("DRY RUN", text)
        self.assertIn("would delete 1", text)
        self.assertIn(f"older than {retention.AI_APP_NOTICE_RETENTION_DAYS} days", text)
        self.assertIn("--apply", text)
        self.assertIn(old.pk, self.alive())

    def test_apply_deletes(self):
        old = self.row(WINDOW + 30 * DAY)
        young = self.row(DAY)
        text = self.run_command("--apply")
        self.assertIn("APPLY", text)
        self.assertIn("deleted 1", text)
        self.assertIn(f"older than {retention.AI_APP_NOTICE_RETENTION_DAYS} days", text)
        self.assertEqual(self.alive(), {young.pk})
        self.assertNotIn(old.pk, self.alive())

    def test_output_is_counts_only(self):
        for args in ((), ("--apply",)):
            row = self.row(WINDOW + 30 * DAY)
            text = self.run_command(*args)
            self.assertNotIn(SECRET_TITLE, text)
            self.assertNotIn(self.user.email, text)
            self.assertNotIn(str(row.pk), text)
            self.assertNotIn(str(self.user.id), text)

    def test_nothing_to_do_says_so(self):
        text = self.run_command("--apply")
        self.assertIn("deleted 0", text)
