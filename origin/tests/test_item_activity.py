"""Per-day activity per item and seen markers (MIP-1084).

`MemberItemDay` counts, per member, task and local day, the saved actions on the task by kind,
from the same sources and rules as lines. What is pinned here, each by a test that fails when
the guard is removed:

  * every event counts in exactly one column; automation and the member's own AI-app markers
    count in none; a day is a half-open local day;
  * the member's items are found by role and by what they did, and only ones they can open;
  * REPRODUCIBLE: a rebuild from the sources (plus the member's ledger rows) gives the same
    rows; a day-by-day fill equals one range rebuild; a rebuild twice does not double; days
    older than the ledger window are frozen, not rewritten;
  * private: another member's rows are never read, exported or counted; a task the member can
    no longer open is dropped before anything is summed;
  * the switch off builds, reads and keeps nothing (the PATCH purges, the writer re-checks under
    the lock); a year of retention; the account deletion and the export;
  * the briefing reads the rows when they exist and never fails for them;
  * seen markers are one time: the latest own read or write, resolved to the task, with no
    duration, count or span in what comes back; `since_seen` counts real event times.

Time: every timestamp is the local midnight of three days ago plus minutes and `now` is passed
explicitly, so no test depends on the hour it runs at.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.item_activity_models import MemberItemDay, MemberItemDayState
from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.services import briefing, item_activity, item_lines
from origin.services.account_deletion import delete_account
from origin.services.account_export import build_export
from origin.tests.test_item_lines import LineBase

PREF = "/api/v2/user/preferences/member-events/"
KINDS = ("mine", "others", "notes", "chat", "prs")


class ActivityBase(LineBase):
    def setUp(self):
        super().setUp()
        self.day0 = self.base.date()
        # Noon of "today" is three days after the day the fixtures are on, so the days that
        # are closed (and filled) are the seven before it.
        self.now = self.at(3 * 24 * 60 + 12 * 60)

    def utc(self, minutes):
        """An instant as the ledger hands it back (UTC), written as the tools write it."""
        return self.at(minutes).astimezone(UTC).isoformat()

    def d(self, offset=0) -> date:
        return self.day0 + timedelta(days=offset)

    def day_minutes(self, offset, minutes=0):
        """Minutes counted from the start of day0, for an event `offset` days later."""
        return offset * 24 * 60 + minutes

    def fill(self, **kw):
        return item_activity.fill_forward(self.ctx, kw.get("now", self.now))

    def rows(self, user=None):
        user = user or self.user
        return {
            (r.task_id, r.day): {k: getattr(r, k) for k in (*KINDS, "capped")}
            for r in MemberItemDay.objects.filter(user=user)
        }

    def counts(self, task, offset=0, user=None):
        found = self.rows(user).get((task.task_id, self.d(offset)))
        return {k: found[k] for k in KINDS} if found else None

    def delivery(self, task, state, merged_minutes=None, n=1):
        return TaskDelivery.objects.create(
            team=self.team,
            task=task,
            repo_owner="o",
            repo_name="r",
            branch=f"b{n}",
            pr_url=f"https://github.com/o/r/pull/{n}",
            pr_number=n,
            pr_title="Ship it",
            state=state,
            merged_at=self.at(merged_minutes) if merged_minutes is not None else None,
            source="webhook",
        )

    def busy_day(self):
        """One task with every column on day0, plus noise that must count nowhere."""
        task = self.make_task()
        self.activity(task, self.user, 10)  # mine
        self.comment(task, self.user, 11)  # mine
        self.comment(task, self.user2, 12)  # others
        self.activity(task, self.user2, 13, action="priority_changed", old="Low", new="High")
        note = self.task_note(task)
        self.version(2, note, self.user2, 14)  # notes
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user2, 15)
        self.link_thread(task, root)
        self.message(channel, self.user, 16, root=root)  # chat
        self.activity(task, None, 17, action="pr_linked", old=None, new=None, pr_title="Fix")
        self.delivery(task, TaskDeliveryState.MERGED, 18)  # prs x2 with the line above
        # noise: automation, and an AI-app read
        self.activity(task, None, 19, action="status_changed")
        self.marker(self.user, 20, "task", task.task_id, kind="read")
        return task


# --------------------------------------------------------------------------- #
# Counting                                                                     #
# --------------------------------------------------------------------------- #


class TestCounting(ActivityBase):
    def test_every_event_counts_in_exactly_one_column(self):
        task = self.busy_day()
        self.fill()
        self.assertEqual(
            self.counts(task),
            {"mine": 2, "others": 2, "notes": 1, "chat": 2, "prs": 2},
        )

    def test_automation_and_the_members_own_ai_app_markers_count_nowhere(self):
        task = self.make_task()
        self.activity(task, None, 10, action="status_changed")
        self.marker(self.user, 11, "task", task.task_id, kind="read")
        self.marker(self.user, 12, "task", task.task_id, kind="write")
        self.fill()
        self.assertIsNone(self.counts(task))

    def test_the_members_own_ledger_changes_count_as_theirs_by_how_many_times(self):
        task = self.make_task()
        self.marker(
            self.user,
            10,
            "task",
            task.task_id,
            kind="dependency_add",
            source="app",
            client="",
            other_id="5",
            count=3,
        )
        self.fill()
        self.assertEqual(self.counts(task)["mine"], 3)

    def test_a_day_is_a_half_open_local_day(self):
        task = self.make_task()
        self.comment(task, self.user2, 0, text="first minute")
        self.comment(task, self.user2, 24 * 60 - 1, text="last minute")
        self.comment(task, self.user2, 24 * 60, text="next day")
        self.comment(task, self.user2, -1, text="day before")
        self.fill()
        self.assertEqual(self.counts(task, 0)["others"], 2)
        self.assertEqual(self.counts(task, 1)["others"], 1)
        self.assertEqual(self.counts(task, -1)["others"], 1)

    def test_a_quiet_task_has_no_row_and_a_row_is_counts_only(self):
        self.make_task()
        self.busy_day()
        self.fill()
        self.assertEqual(MemberItemDay.objects.count(), 1)
        names = {f.name for f in MemberItemDay._meta.get_fields()}
        self.assertFalse(names & {"title", "text", "body", "body_text", "duration", "seconds"})

    def test_today_is_never_stored(self):
        task = self.make_task()
        self.comment(task, self.user2, self.day_minutes(3, 60))  # today (noon is "now")
        self.fill()
        self.assertEqual(self.rows(), {})
        self.assertEqual(MemberItemDayState.objects.get(user=self.user).built_through, self.d(2))


class TestWhichItems(ActivityBase):
    def test_others_activity_on_an_item_of_mine_counts_by_role(self):
        for role in ("assignee", "reporter"):
            task = self.make_task(
                assignee=self.user if role == "assignee" else self.user2,
                reporter=self.user if role == "reporter" else self.user2,
            )
            self.comment(task, self.user2, 10)
            item_activity.rebuild(self.ctx, self.d(-6), self.d(2), self.now)
            self.assertEqual(self.counts(task)["others"], 1, role)

    def test_a_task_i_only_commented_on_is_followed_and_one_i_never_touched_is_not(self):
        mine = self.make_task(assignee=self.user2, reporter=self.user2)
        theirs = self.make_task(assignee=self.user2, reporter=self.user2)
        self.comment(mine, self.user, 10)
        self.comment(mine, self.user2, 11)
        self.comment(theirs, self.user2, 12)
        self.fill()
        self.assertEqual(self.counts(mine), {**dict.fromkeys(KINDS, 0), "mine": 1, "others": 1})
        self.assertIsNone(self.counts(theirs))

    def test_a_task_i_changed_in_the_range_is_followed_without_any_role(self):
        task = self.make_task(assignee=self.user2, reporter=self.user2)
        self.activity(task, self.user, 10)
        self.activity(task, self.user2, 11, action="priority_changed", old="Low", new="High")
        self.fill()
        self.assertEqual(self.counts(task)["mine"], 1)
        self.assertEqual(self.counts(task)["others"], 1)

    def test_a_task_my_ai_app_changed_through_a_ledger_row_is_followed(self):
        task = self.make_task(assignee=self.user2, reporter=self.user2)
        self.marker(
            self.user, 10, "task", task.task_id, kind="collaborator_add", source="app", client=""
        )
        self.fill()
        self.assertEqual(self.counts(task)["mine"], 1)

    def test_a_linked_thread_i_wrote_in_is_followed(self):
        task = self.make_task(assignee=self.user2, reporter=self.user2)
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user2, 10)
        self.link_thread(task, root)
        self.message(channel, self.user, 11, root=root)
        self.fill()
        self.assertEqual(self.counts(task)["chat"], 2)

    def test_a_closed_task_is_followed_only_on_days_before_its_last_update(self):
        task = self.make_task(status="Closed")
        TaskMaster.objects.filter(pk=task.pk).update(
            ts_updated_at=self.at(self.day_minutes(-2, 12))
        )
        self.comment(task, self.user2, self.day_minutes(-3, 10))
        self.comment(task, self.user2, self.day_minutes(-1, 10))
        self.fill()
        self.assertEqual(self.counts(task, -3)["others"], 1)
        self.assertIsNone(self.counts(task, -1))

    def test_a_closed_task_untouched_for_the_range_is_not_followed(self):
        task = self.make_task(status="Closed")
        TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=self.at(-60 * 24 * 40))
        self.comment(task, self.user2, 10)
        self.fill()
        self.assertIsNone(self.counts(task))

    def test_a_comment_whose_author_has_left_is_still_somebody_elses(self):
        task = self.make_task()
        self.comment(task, None, 10)
        self.fill()
        self.assertEqual(self.counts(task)["others"], 1)

    def test_a_task_the_member_cannot_open_is_never_counted(self):
        hidden = self.hidden_task()
        self.comment(hidden, self.user2, 10)
        self.activity(hidden, self.user, 11)  # even an event they "did" on it
        self.fill()
        self.assertEqual(self.rows(), {})

    def test_the_role_cap_marks_the_rows_it_may_have_cut(self):
        # More tasks of the member's by role than the cap. One kept slot goes to a task they can
        # no longer open, so what is followed is under the cap, yet one task was left out:
        # the rows say they are a floor.
        kept = self.make_task()
        self.comment(kept, self.user2, 10)
        hidden = self.hidden_task()
        TaskMaster.objects.filter(pk=hidden.pk).update(assignee=self.user2)
        self.comment(hidden, self.user, 5)  # a role: they commented on it once
        with mock.patch.object(item_activity, "CANDIDATE_MAX", 2):
            self.fill()
        self.assertEqual(self.counts(kept)["others"], 1)
        self.assertTrue(self.rows()[(kept.task_id, self.d(0))]["capped"])

    def test_a_day_follows_at_most_the_cap_and_says_so(self):
        for _ in range(3):
            task = self.make_task(assignee=self.user2, reporter=self.user2)
            self.activity(task, self.user, 10)  # acted on, so followed that day
        with mock.patch.object(item_activity, "CANDIDATE_MAX", 2):
            self.fill()
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(r["capped"] for r in self.rows().values()))

    def test_no_cap_no_flag(self):
        task = self.make_task()
        self.comment(task, self.user2, 10)
        self.fill()
        self.assertFalse(self.rows()[(task.task_id, self.d(0))]["capped"])

    def test_a_pool_hit_marks_the_row(self):
        task = self.make_task()
        for i in range(3):
            self.comment(task, self.user2, 10 + i)
        with mock.patch.object(item_lines, "POOL", 3):
            self.fill()
        self.assertTrue(self.rows()[(task.task_id, self.d(0))]["capped"])
        self.assertEqual(self.counts(task)["others"], 3)

    def test_a_build_does_not_grow_its_queries_with_the_number_of_tasks(self):
        def queries(n):
            MemberItemDay.objects.all().delete()
            MemberItemDayState.objects.all().delete()
            with CaptureQueriesContext(connection) as ctx:
                self.fill()
            return len(ctx)

        for _ in range(2):
            self.comment(self.make_task(), self.user2, 10)
        few = queries(2)
        for _ in range(6):
            self.comment(self.make_task(), self.user2, 10)
        self.assertEqual(queries(8), few)


# --------------------------------------------------------------------------- #
# Reproducible                                                                 #
# --------------------------------------------------------------------------- #


class TestReproducible(ActivityBase):
    def scenario(self):
        task = self.busy_day()
        other = self.make_task()
        self.comment(other, self.user2, self.day_minutes(1, 30))
        self.comment(other, self.user2, self.day_minutes(2, 30))
        self.marker(
            self.user,
            self.day_minutes(2, 40),
            "task",
            other.task_id,
            kind="collaborator_add",
            source="app",
            client="",
        )
        return task, other

    def test_a_rebuild_from_the_sources_gives_the_same_rows(self):
        self.scenario()
        self.fill()
        before = self.rows()
        self.assertGreater(len(before), 2)
        MemberItemDay.objects.all().delete()
        MemberItemDayState.objects.all().delete()
        written = item_activity.rebuild(self.ctx, self.d(-6), self.d(2), self.now)
        self.assertEqual(written, len(before))
        self.assertEqual(self.rows(), before)

    def test_a_fill_day_by_day_equals_one_range_rebuild(self):
        self.scenario()
        # Fill when "today" was day0 (builds the seven days up to day -1 ... ) and again later.
        item_activity.fill_forward(self.ctx, self.at(self.day_minutes(1, 12 * 60)))
        item_activity.fill_forward(self.ctx, self.at(self.day_minutes(2, 12 * 60)))
        item_activity.fill_forward(self.ctx, self.now)
        stepped = self.rows()
        MemberItemDay.objects.all().delete()
        MemberItemDayState.objects.all().delete()
        item_activity.rebuild(self.ctx, self.d(-6), self.d(2), self.now)
        self.assertEqual(self.rows(), stepped)

    def test_a_rebuild_twice_does_not_double_anything(self):
        self.scenario()
        self.fill()
        before = self.rows()
        item_activity.rebuild(self.ctx, self.d(-6), self.d(2), self.now)
        item_activity.rebuild(self.ctx, self.d(-6), self.d(2), self.now)
        self.assertEqual(self.rows(), before)

    def test_a_second_fill_with_nothing_due_writes_nothing(self):
        self.scenario()
        self.assertGreater(self.fill(), 0)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.fill(), 0)
        self.assertEqual(len([q for q in ctx if "INSERT" in q["sql"]]), 0)

    def test_a_rebuild_replaces_a_stale_row_and_leaves_other_days_alone(self):
        task, other = self.scenario()
        self.fill()
        MemberItemDay.objects.filter(task_id=task.task_id, day=self.d(0)).update(mine=99)
        MemberItemDay.objects.filter(task_id=other.task_id, day=self.d(1)).update(others=77)
        item_activity.rebuild(self.ctx, self.d(0), self.d(0), self.now)
        self.assertEqual(self.counts(task)["mine"], 2)
        self.assertEqual(self.counts(other, 1)["others"], 77)

    def test_a_long_absence_is_not_back_filled_by_a_read(self):
        task = self.make_task()
        MemberItemDayState.objects.create(
            user=self.user, team=self.team, built_through=self.d(2) - timedelta(days=20)
        )
        self.comment(task, self.user2, self.day_minutes(-12, 10))  # inside the gap, too old
        self.comment(task, self.user2, 10)
        self.fill()
        self.assertEqual(set(self.rows()), {(task.task_id, self.d(0))})
        self.assertEqual(MemberItemDayState.objects.get(user=self.user).built_through, self.d(2))

    def test_a_day_built_alone_equals_the_same_day_inside_a_range(self):
        # Someone else's task the member touched on day 0 only: day 1 must not follow it
        # whether day 1 is built alone or after day 0 in one range.
        task = self.make_task(assignee=self.user2, reporter=self.user2)
        self.activity(task, self.user, 10)
        self.comment(task, self.user2, 20)
        self.comment(task, self.user2, self.day_minutes(1, 20))
        item_activity.rebuild(self.ctx, self.d(0), self.d(1), self.now)
        in_range = self.rows()
        MemberItemDay.objects.all().delete()
        item_activity.rebuild(self.ctx, self.d(0), self.d(0), self.now)
        item_activity.rebuild(self.ctx, self.d(1), self.d(1), self.now)
        self.assertEqual(self.rows(), in_range)
        self.assertEqual(set(in_range), {(task.task_id, self.d(0))})

    def test_a_rebuild_does_not_move_the_fill_watermark(self):
        self.scenario()
        item_activity.rebuild(self.ctx, self.d(-6), self.d(2), self.now)
        self.assertFalse(MemberItemDayState.objects.filter(user=self.user).exists())
        MemberItemDayState.objects.create(user=self.user, team=self.team, built_through=self.d(-3))
        item_activity.rebuild(self.ctx, self.d(-6), self.d(2), self.now)
        self.assertEqual(MemberItemDayState.objects.get(user=self.user).built_through, self.d(-3))

    def test_a_rebuild_never_goes_into_today_or_the_future(self):
        task = self.make_task()
        self.comment(task, self.user2, self.day_minutes(3, 30))
        item_activity.rebuild(self.ctx, self.d(0), self.d(10), self.now)
        self.assertEqual(self.rows(), {})

    def test_days_older_than_the_ledger_window_are_frozen_not_rewritten(self):
        task = self.make_task()
        self.comment(task, self.user2, 10)
        self.fill()
        self.assertEqual(self.counts(task)["others"], 1)
        # 200 days later the member's own ledger rows of that day are gone; a rebuild of it
        # would erase what they added, so it is refused and the row stays as it was.
        later = self.now + timedelta(days=200)
        self.assertEqual(item_activity.rebuild(self.ctx, self.d(-6), self.d(2), later), 0)
        self.assertEqual(self.counts(task)["others"], 1)

    def test_the_command_rebuilds_a_members_days(self):
        self.scenario()
        out = StringIO()
        with mock.patch("django.utils.timezone.now", return_value=self.now):
            call_command(
                "rebuild_item_activity",
                user=str(self.user.id),
                team=str(self.team.team_id),
                days=10,
                stdout=out,
            )
        self.assertIn("Rebuilt", out.getvalue())
        self.assertGreater(len(self.rows()), 0)
        with self.assertRaises(CommandError):
            call_command("rebuild_item_activity", user=str(uuid.uuid4()), team="x", days=5)
        with self.assertRaises(CommandError):
            call_command(
                "rebuild_item_activity", user=str(self.user.id), team=str(self.team.team_id), days=0
            )


# --------------------------------------------------------------------------- #
# Private, opt-out, retention, deletion, export                                #
# --------------------------------------------------------------------------- #


class TestPrivate(ActivityBase):
    def test_another_members_rows_are_never_read_or_exported(self):
        task = self.make_task(assignee=self.user2, reporter=self.user2)
        self.comment(task, self.user, 10)
        self.comment(task, self.user2, 11)
        self.fill()
        item_activity.fill_forward(
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)), self.now
        )
        self.assertEqual(MemberItemDay.objects.filter(user=self.user2).count(), 1)
        mine = item_activity.week(self.ctx, self.now)
        theirs = item_activity.week(
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)), self.now
        )
        self.assertEqual([i["week"]["mine"] for i in mine["items"]], [1])
        self.assertEqual([i["week"]["mine"] for i in theirs["items"]], [1])
        for member, other in ((self.user, self.user2), (self.user2, self.user)):
            exported = item_activity.export_rows(str(member.id))
            self.assertEqual(len(exported), 1)
            self.assertNotIn(str(other.id), json.dumps(exported))
        # Nobody's rows are reachable without their id: the reader takes the caller's context.
        self.assertEqual(MemberItemDay.objects.filter(user=self.outsider).count(), 0)
        self.assertIsNone(
            item_activity.week(
                ToolContext(team_id=str(self.team.team_id), user_id=str(self.outsider.id)),
                self.now,
            )
        )

    def test_a_task_the_member_can_no_longer_open_is_dropped_before_anything_is_summed(self):
        task = self.make_task()
        self.comment(task, self.user2, 10)
        self.fill()
        self.assertEqual(item_activity.week(self.ctx, self.now)["total_items"], 1)
        # Handed to a project and people they are not in: it is no longer theirs to open.
        TaskMaster.objects.filter(pk=task.pk).update(
            project=self.hidden_project(), assignee=self.user2, reporter=self.user2
        )
        self.assertEqual(item_activity.week(self.ctx, self.now)["items"], [])
        self.assertEqual(item_activity.export_rows(str(self.user.id)), [])
        self.assertEqual(MemberItemDay.objects.count(), 1)  # the row is theirs, just not shown

    def test_a_deleted_task_is_not_shown(self):
        task = self.make_task()
        self.comment(task, self.user2, 10)
        self.fill()
        TaskMaster.objects.filter(pk=task.pk).update(is_deleted=True)
        self.assertEqual(item_activity.week(self.ctx, self.now)["items"], [])


class TestSwitch(ActivityBase):
    def switch(self, value):
        self.client.credentials()
        self.client.force_authenticate(user=self.user)
        return self.client.patch(PREF, {"member_events_enabled": value}, format="json")

    def test_turning_it_off_deletes_the_rows_and_the_watermark(self):
        self.busy_day()
        self.fill()
        self.assertGreater(MemberItemDay.objects.count(), 0)
        self.assertEqual(self.switch(False).status_code, 200)
        self.assertEqual(MemberItemDay.objects.filter(user=self.user).count(), 0)
        self.assertEqual(MemberItemDayState.objects.filter(user=self.user).count(), 0)

    def test_turning_it_off_leaves_other_members_rows(self):
        task = self.make_task(assignee=self.user2, reporter=self.user2)
        self.comment(task, self.user2, 10)
        item_activity.fill_forward(
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)), self.now
        )
        self.switch(False)
        self.assertEqual(MemberItemDay.objects.filter(user=self.user2).count(), 1)

    def test_nothing_is_built_or_read_while_it_is_off(self):
        self.busy_day()
        self.switch(False)
        self.assertEqual(self.fill(), 0)
        self.assertEqual(item_activity.rebuild(self.ctx, self.d(-6), self.d(2), self.now), 0)
        self.assertEqual(MemberItemDay.objects.count(), 0)
        self.assertIsNone(item_activity.briefing_section(self.ctx, self.now))

    def test_a_build_already_running_when_it_is_turned_off_writes_nothing(self):
        self.busy_day()
        real = item_activity.compute

        def compute_then_switch_off(ctx, days):
            result = real(ctx, days)
            self.switch(False)  # the member turns it off while the build is reading
            return result

        with mock.patch.object(item_activity, "compute", compute_then_switch_off):
            self.assertEqual(self.fill(), 0)
        self.assertEqual(MemberItemDay.objects.count(), 0)
        self.assertEqual(MemberItemDayState.objects.count(), 0)

    def test_nothing_is_read_from_the_sources_while_it_is_off(self):
        self.busy_day()
        self.switch(False)
        with mock.patch.object(item_activity, "compute") as compute:
            self.fill()
            item_activity.rebuild(self.ctx, self.d(-6), self.d(2), self.now)
        compute.assert_not_called()

    def test_turning_it_on_again_starts_today_and_does_not_rebuild_the_time_it_was_off(self):
        task = self.make_task()
        self.comment(task, self.user2, 10)
        self.fill()
        self.switch(False)
        self.assertEqual(self.switch(True).status_code, 200)
        state = MemberItemDayState.objects.get(user=self.user, team=self.team)
        self.assertEqual(state.built_through, self.d(2))
        self.assertEqual(self.fill(), 0)
        self.assertEqual(self.rows(), {})
        # The next closed day is built as usual.
        self.comment(task, self.user2, self.day_minutes(3, 30))
        later = self.now + timedelta(days=1)
        self.assertEqual(item_activity.fill_forward(self.ctx, later), 1)
        self.assertEqual(self.counts(task, 3)["others"], 1)

    def test_a_repeat_of_on_does_not_reset_anything(self):
        task = self.make_task()
        self.comment(task, self.user2, 10)
        self.fill()
        MemberItemDayState.objects.update(built_through=self.d(-1))  # behind: a reset would show
        before = self.rows()
        self.switch(True)
        self.assertEqual(self.rows(), before)
        self.assertEqual(MemberItemDayState.objects.get(user=self.user).built_through, self.d(-1))


class TestLifecycle(ActivityBase):
    def test_a_year_old_row_is_swept_by_the_next_build_and_never_read(self):
        task = self.make_task()
        old = MemberItemDay.objects.create(
            user=self.user,
            team=self.team,
            task_id=task.task_id,
            day=self.d(3) - timedelta(days=366),
        )
        fresh = MemberItemDay.objects.create(
            user=self.user,
            team=self.team,
            task_id=task.task_id,
            day=self.d(3) - timedelta(days=360),
        )
        self.comment(task, self.user2, 10)
        self.fill()
        self.assertFalse(MemberItemDay.objects.filter(pk=old.pk).exists())
        self.assertTrue(MemberItemDay.objects.filter(pk=fresh.pk).exists())

    def test_the_sweep_only_touches_the_builders_own_rows(self):
        task = self.make_task()
        theirs = MemberItemDay.objects.create(
            user=self.user2,
            team=self.team,
            task_id=task.task_id,
            day=self.d(3) - timedelta(days=500),
        )
        self.comment(task, self.user2, 10)
        self.fill()
        self.assertTrue(MemberItemDay.objects.filter(pk=theirs.pk).exists())

    def test_the_account_deletion_deletes_the_rows_and_only_theirs(self):
        task = self.make_task(assignee=self.user2, reporter=self.user2)
        self.comment(task, self.user, 10)
        self.comment(task, self.user2, 11)
        self.fill()
        item_activity.fill_forward(
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)), self.now
        )
        delete_account(self.user2)
        self.assertEqual(MemberItemDay.objects.filter(user=self.user2).count(), 0)
        self.assertEqual(MemberItemDayState.objects.filter(user=self.user2).count(), 0)
        self.assertEqual(MemberItemDay.objects.filter(user=self.user).count(), 1)

    def test_the_export_carries_ids_days_and_counts_and_no_title(self):
        task = self.make_task(title="Renegotiate the Hendricks lease")
        self.comment(task, self.user2, 10)
        self.fill()
        export = build_export(self.user)
        self.assertEqual(export["counts"]["item_activity"], 1)
        [row] = export["item_activity"]
        self.assertEqual(
            row,
            {
                "team_id": str(self.team.team_id),
                "task_id": task.task_id,
                "day": self.d(0).isoformat(),
                "mine": 0,
                "others": 1,
                "notes": 0,
                "chat": 0,
                "prs": 0,
                "capped": False,
            },
        )
        self.assertNotIn("Hendricks", json.dumps(export["item_activity"]))


# --------------------------------------------------------------------------- #
# Reading: week, trend, briefing                                               #
# --------------------------------------------------------------------------- #


class TestTrend(ActivityBase):
    def test_rising_cooling_steady_quiet(self):
        trend = item_activity.trend
        self.assertEqual(trend([0] * 7), "quiet")
        self.assertEqual(trend([0, 0, 0, 0, 0, 1, 2]), "rising")  # new activity
        self.assertEqual(trend([1, 1, 1, 1, 3, 3, 3]), "rising")  # 3.0 vs 1.0
        self.assertEqual(trend([4, 4, 4, 4, 0, 0, 1]), "cooling")
        self.assertEqual(trend([3, 3, 3, 3, 0, 0, 0]), "cooling")
        self.assertEqual(trend([2, 2, 2, 2, 2, 2, 2]), "steady")
        self.assertEqual(trend([2, 2, 2, 2, 3, 3, 3]), "steady")  # 3.0 vs 2.0 is under 1.5x
        self.assertEqual(trend([4, 4, 4, 4, 2, 2, 2]), "steady")  # 2.0 vs 4.0 is not under 0.5x
        self.assertEqual(trend([4, 4, 4, 4, 1, 1, 1]), "cooling")

    def test_the_boundary_is_strict(self):
        trend = item_activity.trend
        self.assertEqual(trend([2, 2, 2, 2, 3, 3, 3]), "steady")  # exactly 1.5x
        self.assertEqual(trend([4, 4, 4, 4, 2, 2, 2]), "steady")  # exactly 0.5x


class TestWeek(ActivityBase):
    def test_the_week_is_summed_on_read_by_day_and_kind(self):
        task = self.make_task()
        self.comment(task, self.user2, self.day_minutes(1, 10))
        self.comment(task, self.user2, self.day_minutes(2, 10))
        self.comment(task, self.user, self.day_minutes(2, 20))
        self.activity(task, self.user, self.day_minutes(2, 30))
        self.fill()
        week = item_activity.week(self.ctx, self.now)
        self.assertEqual(week["through"], self.d(2).isoformat())
        self.assertEqual(week["from"], self.d(-4).isoformat())
        [item] = week["items"]
        self.assertEqual(item["days"], [0, 0, 0, 0, 0, 1, 3])
        self.assertEqual(item["week"], {"total": 4, "mine": 2, "others": 2})
        self.assertEqual(item["trend"], "rising")
        self.assertEqual(self.plain(item["title"]), "A task")

    def test_the_week_has_no_rows_before_a_fill_and_none_for_a_member_who_is_off(self):
        self.assertIsNone(item_activity.week(self.ctx, self.now))

    def test_the_busiest_items_come_first_and_the_list_is_cut_with_a_count(self):
        tasks = []
        for n in range(3):
            task = self.make_task()
            for i in range(n + 1):
                self.comment(task, self.user2, 10 + i)
            tasks.append(task)
        self.fill()
        with mock.patch.object(item_activity, "WEEK_ITEMS", 2):
            week = item_activity.week(self.ctx, self.now)
        self.assertEqual(
            [i["task_id"] for i in week["items"]], [tasks[2].task_id, tasks[1].task_id]
        )
        self.assertEqual((week["total_items"], week["truncated"]), (3, True))

    def test_a_capped_item_says_so(self):
        task = self.make_task()
        self.comment(task, self.user2, 10)
        self.fill()
        MemberItemDay.objects.update(capped=True)
        self.assertTrue(item_activity.week(self.ctx, self.now)["items"][0]["capped"])


class TestBriefing(ActivityBase):
    def brief(self, **kw):
        return briefing.build_briefing(self.ctx, now=self.now, **kw)

    def test_the_briefing_reads_the_rollups_when_they_exist(self):
        task = self.make_task()
        self.comment(task, self.user2, self.day_minutes(2, 10))
        MemberItemDay.objects.create(
            user=self.user, team=self.team, task_id=task.task_id, day=self.d(2), others=5
        )
        MemberItemDayState.objects.create(user=self.user, team=self.team, built_through=self.d(2))
        activity = self.brief()["activity"]
        [item] = activity["items"]
        self.assertEqual(item["task_id"], task.task_id)
        self.assertEqual(item["week"]["others"], 5)  # the stored row, not a recount

    def test_the_briefing_fills_the_table_it_reads_and_names_the_task_without_text(self):
        task = self.make_task(title="Renegotiate the Hendricks lease")
        self.comment(task, self.user2, self.day_minutes(2, 10), text="private words")
        activity = self.brief()["activity"]
        self.assertEqual(activity["items"][0]["task_id"], task.task_id)
        self.assertNotIn("private words", json.dumps(activity))

    def test_it_is_null_when_nothing_was_built_and_when_recording_is_off(self):
        self.assertIsNone(self.brief()["activity"])
        self.user.member_events_enabled = False
        self.user.save(update_fields=["member_events_enabled"])
        task = self.make_task()
        self.comment(task, self.user2, 10)
        self.assertIsNone(self.brief()["activity"])
        self.assertEqual(MemberItemDay.objects.count(), 0)

    def test_a_failure_in_the_rollups_never_fails_the_briefing(self):
        with mock.patch.object(item_activity, "week", side_effect=RuntimeError("boom")):
            with self.assertLogs(item_activity.log, level="WARNING"):
                brief = self.brief()
        self.assertIsNone(brief["activity"])
        self.assertIn("changed_for_me", brief)

    def test_a_failed_fill_still_shows_what_was_built(self):
        task = self.make_task()
        MemberItemDay.objects.create(
            user=self.user, team=self.team, task_id=task.task_id, day=self.d(2), others=3
        )
        with mock.patch.object(item_activity, "fill_forward", side_effect=RuntimeError("boom")):
            with self.assertLogs(item_activity.log, level="WARNING"):
                brief = self.brief()
        self.assertEqual(brief["activity"]["items"][0]["week"]["others"], 3)

    def test_nothing_else_in_the_briefing_changes(self):
        task = self.make_task()
        self.comment(task, self.user2, self.day_minutes(2, 10))
        with mock.patch.object(item_activity, "briefing_section", return_value=None):
            without = self.brief()  # opens the window; the next call repeats it
        with_rollups = self.brief()
        self.assertIsNotNone(with_rollups["activity"])
        self.assertEqual(
            set(with_rollups) - {"activity"},
            {
                "window",
                "me",
                "agenda",
                "changed_for_me",
                "waiting_on_me",
                "waiting_on_others",
                "stale_mine",
                "what_you_did",
                "suggested_next",
                "__summary__",
            },
        )
        for key in ("changed_for_me", "suggested_next", "what_you_did", "__summary__"):
            self.assertEqual(with_rollups[key], without[key], key)


# --------------------------------------------------------------------------- #
# Seen markers                                                                 #
# --------------------------------------------------------------------------- #


class TestSeen(ActivityBase):
    def seen(self, *tasks, user=None):
        ctx = (
            self.ctx
            if user is None
            else ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))
        )
        return item_lines.seen_at(ctx, [t.task_id for t in tasks])

    def test_it_is_the_latest_own_read_or_write_and_a_time_only(self):
        task = self.make_task()
        self.marker(self.user, 10, "task", task.task_id, kind="read")
        self.marker(self.user, 30, "task", task.task_id, kind="write")
        self.marker(self.user, 20, "task", task.task_id, kind="read")
        found = self.seen(task)
        self.assertEqual(found, {task.task_id: self.at(30)})
        self.assertEqual(set(found.values()), {self.at(30)})  # no span, count or bucket

    def test_another_members_looks_are_not_mine(self):
        task = self.make_task()
        self.marker(self.user2, 10, "task", task.task_id, kind="read")
        self.assertEqual(self.seen(task), {})
        self.assertEqual(self.seen(task, user=self.user2), {task.task_id: self.at(10)})

    def test_a_ledger_change_is_not_a_look(self):
        task = self.make_task()
        self.marker(
            self.user, 10, "task", task.task_id, kind="collaborator_add", source="app", client=""
        )
        self.assertEqual(self.seen(task), {})

    def test_a_look_at_the_milestone_the_note_or_the_thread_is_a_look_at_the_task(self):
        from origin.models.task.milestone_models import MilestoneMaster

        milestone_task = self.make_task("Milestone row", is_milestone=True)
        milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            title="M",
            reporter=self.user,
            task=milestone_task,
        )
        note_task = self.make_task("With note")
        note = self.task_note(note_task)
        thread_task = self.make_task("With thread")
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user2, 1)
        self.link_thread(thread_task, root)
        self.marker(self.user, 10, "milestone", milestone.milestone_id)
        self.marker(self.user, 20, "note", f"task:{note.note_id}")
        self.marker(self.user, 30, "chat_thread", f"{channel.id}:{root.id}")
        self.assertEqual(
            self.seen(milestone_task, note_task, thread_task),
            {
                milestone_task.task_id: self.at(10),
                note_task.task_id: self.at(20),
                thread_task.task_id: self.at(30),
            },
        )

    def test_the_briefing_item_carries_the_time_and_nothing_else_about_looking(self):
        task, quiet = self.make_task(), self.make_task()
        self.comment(task, self.user2, self.day_minutes(2, 10))
        self.comment(quiet, self.user2, self.day_minutes(2, 20))
        self.marker(self.user, 10, "task", task.task_id, kind="read")
        self.marker(self.user, 35, "task", task.task_id, kind="read")
        items = {
            i["task_id"]: i
            for i in briefing.build_briefing(self.ctx, now=self.now)["activity"]["items"]
        }
        self.assertEqual(items[task.task_id]["last_seen_at"], self.utc(35))
        self.assertFalse(
            {k for k in items[task.task_id] if "seen" in k or "look" in k} - {"last_seen_at"}
        )
        self.assertIsNone(items[quiet.task_id]["last_seen_at"])


class TestSinceSeen(ActivityBase):
    def line(self, task, **kw):
        return item_lines.line_for_item(self.ctx, task.task_id, self.since, self.until, **kw)[
            "line"
        ]

    def test_the_line_says_when_it_was_last_seen_and_counts_what_others_did_after(self):
        task = self.make_task()
        self.comment(task, self.user2, 10, text="before")
        self.marker(self.user, 20, "task", task.task_id, kind="read")
        self.comment(task, self.user2, 30, text="after one")
        self.activity(task, self.user2, 40, action="priority_changed", old="Low", new="High")
        self.comment(task, self.user, 50, text="mine")  # not by others
        self.activity(task, None, 55, action="status_changed")  # automation
        line = self.line(task)
        self.assertEqual(line["last_seen_at"], self.utc(20))
        self.assertEqual(line["since_seen"], {"from": self.utc(20), "by_others": 2})

    def test_a_look_older_than_the_window_counts_from_the_window_start(self):
        task = self.make_task()
        self.marker(self.user, -60 * 24 * 2, "task", task.task_id, kind="read")
        self.comment(task, self.user2, 10)
        line = self.line(task)
        self.assertEqual(line["since_seen"], {"from": self.since.isoformat(), "by_others": 1})

    def test_no_look_means_no_since_seen(self):
        task = self.make_task()
        self.comment(task, self.user2, 10)
        line = self.line(task)
        self.assertIsNone(line["last_seen_at"])
        self.assertNotIn("since_seen", line)

    def test_only_the_root_line_carries_it_and_the_tool_returns_it(self):
        task = self.make_task()
        self.marker(self.user, 20, "task", task.task_id, kind="read")
        full = item_lines.item_lines(self.ctx, task.task_id, self.since, self.until)
        self.assertIn("last_seen_at", full["line"])
        for related in full.get("related", []):
            self.assertNotIn("last_seen_at", related)
        tool = self.tool_json(task_id=task.task_id)
        self.assertIn("last_seen_at", tool["line"])
