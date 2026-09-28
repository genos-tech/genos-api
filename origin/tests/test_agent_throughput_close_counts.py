"""The three throughput tools must count closes from STATUS rows.

`TaskActivityActionType.CLOSED` is declared in the enum but **no emitter
writes it**: closing a task goes through the generic field-diff loop in
`task_signals.py`, which records `action_type='status_changed'` with the
new status in `new_value`. Filtering on `action_type=CLOSED` therefore
reports zero closes forever, which is what `get_task_throughput_stats`,
`get_my_throughput` and `get_top_task_closers` all used to do — while
`TaskVelocityView`, reading the same rows, reported the real number.

So the tests here deliberately seed closes the way the *signal* does,
never the way the old code read them. Each one would pass trivially
against a `CLOSED`-filtering implementation only if it also seeded
`CLOSED` rows — so every close below goes in as a STATUS row, and a
regression to `action_type=CLOSED` turns these red.

What's pinned:
  * a status transition into Closed counts as a close;
  * so does one into Deleted (both are in `CLOSED_STATUSES`);
  * a transition to a NON-closed status (WIP / Blocked / Open) does not,
    and does not leak into bucketed output as a spurious empty bucket;
  * reopen→close cycles count once per close, which is the property
    these tools read the audit log for in the first place;
  * a dead `action_type='closed'` row is NOT counted — it can't occur in
    practice, and counting it would double-count if an emitter is ever
    added;
  * `get_my_throughput`'s `recently_closed` list resolves real tasks;
  * the leaderboard attributes each close to its actor.

`ts_created_at` is `auto_now_add`, so it's forced afterwards with a
queryset `.update()`, same approach as `test_task_velocity_view`. Task
creation emits its own signal rows, so the audit table is cleared before
each test seeds its own.
"""

from datetime import timedelta

from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.get_my_throughput import GET_MY_THROUGHPUT
from origin.search_engine.agent.tools.get_task_throughput_stats import GET_TASK_THROUGHPUT_STATS
from origin.search_engine.agent.tools.get_top_task_closers import GET_TOP_TASK_CLOSERS

from .test_base import BaseAPITestCase


class ThroughputCloseCountTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Throughput Project",
            owner=self.user,
            project_system_user=self.user,
        )
        # All three tools scope to the caller's ProjectMembers rows.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task_a = TaskMaster.objects.create(
            team=self.team, project=self.project, title="A", status="Open"
        )
        self.task_b = TaskMaster.objects.create(
            team=self.team, project=self.project, title="B", status="Open"
        )
        # Drop the signal-generated CREATED rows so each test controls
        # its own audit trail.
        TaskActivity.objects.all().delete()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _activity(self, task, action, *, days_ago=1, new_value=None, actor=None):
        row = TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=task,
            actor=self.user if actor is None else actor,
            action_type=action,
            field_name="status" if action == TaskActivityActionType.STATUS else None,
            new_value=new_value,
        )
        TaskActivity.objects.filter(pk=row.pk).update(
            ts_created_at=timezone.now() - timedelta(days=days_ago)
        )
        return row

    def _close(self, task, *, days_ago=1, status="Closed", actor=None):
        """Seed a close the way `task_signals` actually records one."""
        return self._activity(
            task,
            TaskActivityActionType.STATUS,
            days_ago=days_ago,
            new_value=status,
            actor=actor,
        )

    def _stats(self, **args):
        return GET_TASK_THROUGHPUT_STATS.run({"days": 30, **args}, self.ctx)

    def _mine(self, **args):
        return GET_MY_THROUGHPUT.run({"days": 30, **args}, self.ctx)

    def _closers(self, **args):
        return GET_TOP_TASK_CLOSERS.run({"days": 30, **args}, self.ctx)

    # ------------------------------------------------------------------
    # the bug: a real close must be counted
    # ------------------------------------------------------------------
    def test_status_change_to_closed_counts_as_a_close(self):
        self._close(self.task_a)

        self.assertEqual(self._stats()["closed_total"], 1)
        self.assertEqual(self._mine()["closed_total"], 1)
        self.assertEqual(self._closers()["closers"][0]["closed_count"], 1)

    def test_status_change_to_deleted_also_counts(self):
        """`CLOSED_STATUSES` is ("Closed", "Deleted") — both are closes."""
        self._close(self.task_a, status="Deleted")

        self.assertEqual(self._stats()["closed_total"], 1)
        self.assertEqual(self._mine()["closed_total"], 1)

    def test_dead_closed_action_type_is_not_counted(self):
        """No emitter writes `action_type='closed'`.

        Counting it as well would double-count the day someone wires an
        emitter up, since that emitter would still write the STATUS row.
        """
        self._activity(self.task_a, TaskActivityActionType.CLOSED, new_value="Closed")

        self.assertEqual(self._stats()["closed_total"], 0)
        self.assertEqual(self._mine()["closed_total"], 0)
        self.assertEqual(self._closers()["closers"], [])

    # ------------------------------------------------------------------
    # non-closes must stay out
    # ------------------------------------------------------------------
    def test_non_closing_status_changes_are_not_closes(self):
        self._close(self.task_a, status="WIP")
        self._close(self.task_a, status="Blocked")
        self._close(self.task_b, status="Open")

        self.assertEqual(self._stats()["closed_total"], 0)
        self.assertEqual(self._mine()["closed_total"], 0)
        self.assertEqual(self._closers()["closers"], [])

    def test_non_closing_status_changes_do_not_create_empty_buckets(self):
        """A WIP transition is neither a creation nor a close.

        It must not widen the base queryset, or a bucketed response grows
        `created: 0, closed: 0` buckets on days when nothing happened —
        which reads to the model as "we looked, and that day was empty".
        """
        self._close(self.task_a, days_ago=5, status="WIP")
        self._activity(self.task_b, TaskActivityActionType.CREATED, days_ago=2)

        buckets = self._stats(bucket="day")["buckets"]

        self.assertEqual(len(buckets), 1)
        self.assertEqual(buckets[0]["created"], 1)
        self.assertEqual(buckets[0]["closed"], 0)

    def test_other_action_types_are_ignored(self):
        self._activity(self.task_a, TaskActivityActionType.PRIORITY, new_value="High")
        self._activity(self.task_a, TaskActivityActionType.ASSIGNEE)
        self._activity(self.task_a, TaskActivityActionType.COMMENT_ADDED)

        stats = self._stats()
        self.assertEqual(stats["created_total"], 0)
        self.assertEqual(stats["closed_total"], 0)

    # ------------------------------------------------------------------
    # the property these tools read the audit log FOR
    # ------------------------------------------------------------------
    def test_reopen_close_cycle_counts_each_close(self):
        """Close → reopen → close is two closes, not one.

        This is the stated reason these tools aggregate `TaskActivity`
        instead of `TaskMaster.ts_updated_at`, so it's worth pinning: a
        status-based count gets it right for free, because each close
        writes its own row.
        """
        self._close(self.task_a, days_ago=10)
        self._close(self.task_a, days_ago=5, status="Open")  # reopened
        self._close(self.task_a, days_ago=1)

        self.assertEqual(self._stats()["closed_total"], 2)
        self.assertEqual(self._mine()["closed_total"], 2)
        self.assertEqual(self._closers()["closers"][0]["closed_count"], 2)

    # ------------------------------------------------------------------
    # created vs closed stay distinguishable
    # ------------------------------------------------------------------
    def test_created_and_closed_are_counted_separately(self):
        self._activity(self.task_a, TaskActivityActionType.CREATED, days_ago=9)
        self._activity(self.task_b, TaskActivityActionType.CREATED, days_ago=8)
        self._close(self.task_a, days_ago=2)

        stats = self._stats()
        self.assertEqual(stats["created_total"], 2)
        self.assertEqual(stats["closed_total"], 1)
        self.assertIn("2 created, 1 closed (net +1)", stats["__summary__"])

    # ------------------------------------------------------------------
    # per-tool specifics
    # ------------------------------------------------------------------
    def test_my_throughput_lists_recently_closed_tasks(self):
        self._close(self.task_a, days_ago=3)
        self._close(self.task_b, days_ago=1)

        recent = self._mine()["recently_closed"]

        self.assertEqual([r["title"] for r in recent], ["B", "A"])  # newest first
        self.assertEqual(recent[0]["display_id"], self.task_b.display_id)
        self.assertEqual(recent[0]["project_name"], "Throughput Project")

    def test_my_throughput_excludes_other_actors_closes(self):
        other = self._other_member()
        self._close(self.task_a, actor=other)
        self._close(self.task_b)

        self.assertEqual(self._mine()["closed_total"], 1)
        self.assertEqual(self._stats()["closed_total"], 2)  # workspace-wide

    def test_leaderboard_attributes_closes_to_each_actor(self):
        other = self._other_member()
        self._close(self.task_a, actor=other, days_ago=3)
        self._close(self.task_a, actor=other, days_ago=2, status="Open")
        self._close(self.task_a, actor=other, days_ago=1)
        self._close(self.task_b)

        closers = {c["username"]: c["closed_count"] for c in self._closers()["closers"]}

        self.assertEqual(closers[other.username], 2)
        self.assertEqual(closers[self.user.username], 1)

    # ------------------------------------------------------------------
    # window
    # ------------------------------------------------------------------
    def test_closes_outside_the_window_are_excluded(self):
        self._close(self.task_a, days_ago=40)
        self._close(self.task_b, days_ago=2)

        self.assertEqual(GET_TASK_THROUGHPUT_STATS.run({"days": 7}, self.ctx)["closed_total"], 1)
        self.assertEqual(GET_MY_THROUGHPUT.run({"days": 7}, self.ctx)["closed_total"], 1)

    # ------------------------------------------------------------------
    def _other_member(self):
        from django.contrib.auth import get_user_model

        other = get_user_model().objects.create_user(
            email="closer2@example.com", username="Closer Two", password="x"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=other)
        return other
