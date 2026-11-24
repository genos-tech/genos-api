"""The daily agent-memory sweep (MIP-2251): rows past their window go for every member, not only
for the member who next writes; counts only; dry run by default."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from origin.models.common.item_activity_models import MemberItemDay
from origin.models.common.member_event_models import MemberEvent
from origin.models.common.team_models import TeamMaster
from origin.services import member_memory_sweep

User = get_user_model()


class TestMemberMemorySweep(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="s1", email="s1@example.com", password="x")
        self.team = TeamMaster.objects.create(team_name="Sweep", owner=self.user)
        self.now = timezone.now()

    def event(self, days_old, object_id):
        ts = self.now - timedelta(days=days_old)
        return MemberEvent.objects.create(
            user=self.user,
            team=self.team,
            kind="read",
            object_type="task",
            object_id=object_id,
            ts=ts,
            bucket=int(ts.timestamp() // 600),
        )

    def item_day(self, days_old, task_id):
        return MemberItemDay.objects.create(
            user=self.user,
            team=self.team,
            task_id=task_id,
            day=(self.now - timedelta(days=days_old)).date(),
        )

    def seed(self):
        self.old_event = self.event(181, "1")
        self.fresh_event = self.event(179, "2")
        self.old_day = self.item_day(367, 1)
        self.fresh_day = self.item_day(363, 2)

    def test_a_member_who_never_writes_again_loses_their_old_rows(self):
        self.seed()
        result = member_memory_sweep.sweep(apply=True, now=self.now)
        self.assertEqual((result.events, result.item_days), (1, 1))
        self.assertFalse(MemberEvent.objects.filter(pk=self.old_event.pk).exists())
        self.assertTrue(MemberEvent.objects.filter(pk=self.fresh_event.pk).exists())
        self.assertFalse(MemberItemDay.objects.filter(pk=self.old_day.pk).exists())
        self.assertTrue(MemberItemDay.objects.filter(pk=self.fresh_day.pk).exists())

    def test_a_dry_run_counts_and_deletes_nothing(self):
        self.seed()
        result = member_memory_sweep.sweep(apply=False, now=self.now)
        self.assertEqual((result.events, result.item_days), (1, 1))
        self.assertEqual(MemberEvent.objects.count(), 2)
        self.assertEqual(MemberItemDay.objects.count(), 2)

    def test_more_rows_than_one_batch_all_go(self):
        for i in range(5):
            self.event(200, str(i))
        original = member_memory_sweep.BATCH
        member_memory_sweep.BATCH = 2
        self.addCleanup(setattr, member_memory_sweep, "BATCH", original)
        result = member_memory_sweep.sweep(apply=True, now=self.now)
        self.assertEqual(result.events, 5)
        self.assertEqual(MemberEvent.objects.count(), 0)

    def test_the_command_is_a_dry_run_unless_applied_and_prints_counts_only(self):
        self.seed()
        out = StringIO()
        call_command("prune_member_memory", stdout=out)
        self.assertIn("[dry-run] member_events: would delete 1", out.getvalue())
        self.assertEqual(MemberEvent.objects.count(), 2)
        out = StringIO()
        call_command("prune_member_memory", "--apply", stdout=out)
        self.assertIn("member_events: deleted 1", out.getvalue())
        self.assertIn("member_item_days: deleted 1", out.getvalue())
        self.assertEqual(MemberEvent.objects.count(), 1)
        self.assertNotIn(self.user.username, out.getvalue())
