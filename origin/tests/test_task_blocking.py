"""Dependency-driven auto-"Blocked" status (services/task_blocking +
signals/dependency_signals).

Rules under test:
  * dependency added with an open blocker  → blocked task auto-"Blocked"
  * last open blocker cleared (closed / soft-deleted / edge removed)
    → a task sitting at exactly "Blocked" auto-returns to "Open"
  * manual statuses are respected: "Blocked" set by hand (no blockers)
    is never touched; a manual WIP override on a dep-blocked task
    survives the unblock event
  * closed tasks are never reopened by gaining a blocker; milestone
    backing rows are never auto-transitioned
  * a blocker REOPENING re-blocks its dependents
"""

from origin.models.task.task_models import TaskDependency, TaskMaster

from .test_base import BaseAPITestCase


class TaskBlockingAutoStatusTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.blocker = TaskMaster.objects.create(
            team=self.team, title="Blocker", assignee=self.user, reporter=self.user, status="Open"
        )
        self.task = TaskMaster.objects.create(
            team=self.team, title="Dependent", assignee=self.user, reporter=self.user, status="Open"
        )

    def _dep(self, blocker=None, blocked=None):
        return TaskDependency.objects.create(
            blocker_task=blocker or self.blocker,
            blocked_task=blocked or self.task,
            team=self.team,
            created_by=self.user,
        )

    def _status(self, task):
        task.refresh_from_db()
        return task.status

    # ----- blocking ----------------------------------------------------

    def test_adding_open_blocker_sets_blocked(self):
        self._dep()
        self.assertEqual(self._status(self.task), "Blocked")

    def test_adding_closed_blocker_does_not_block(self):
        self.blocker.status = "Closed"
        self.blocker.save(update_fields=["status", "ts_updated_at"])
        self._dep()
        self.assertEqual(self._status(self.task), "Open")

    def test_wip_task_gets_blocked_too(self):
        self.task.status = "WIP"
        self.task.save(update_fields=["status", "ts_updated_at"])
        self._dep()
        self.assertEqual(self._status(self.task), "Blocked")

    def test_closed_task_is_never_reopened_by_a_blocker(self):
        self.task.status = "Closed"
        self.task.save(update_fields=["status", "ts_updated_at"])
        self._dep()
        self.assertEqual(self._status(self.task), "Closed")

    def test_milestone_backing_task_is_skipped(self):
        backing = TaskMaster.objects.create(
            team=self.team,
            title="Milestone backing",
            assignee=self.user,
            reporter=self.user,
            status="Open",
            is_milestone=True,
        )
        self._dep(blocked=backing)
        self.assertEqual(self._status(backing), "Open")

    # ----- unblocking --------------------------------------------------

    def test_closing_blocker_reverts_to_open(self):
        self._dep()
        self.assertEqual(self._status(self.task), "Blocked")
        self.blocker.status = "Closed"
        self.blocker.save(update_fields=["status", "ts_updated_at"])
        self.assertEqual(self._status(self.task), "Open")

    def test_soft_deleting_blocker_reverts_to_open(self):
        self._dep()
        self.blocker.is_deleted = True
        self.blocker.save(update_fields=["is_deleted", "ts_updated_at"])
        self.assertEqual(self._status(self.task), "Open")

    def test_removing_dependency_reverts_to_open(self):
        dep = self._dep()
        dep.delete()
        self.assertEqual(self._status(self.task), "Open")

    def test_stays_blocked_until_last_open_blocker_clears(self):
        blocker2 = TaskMaster.objects.create(
            team=self.team, title="Blocker 2", assignee=self.user, reporter=self.user, status="Open"
        )
        self._dep()
        self._dep(blocker=blocker2)
        self.blocker.status = "Closed"
        self.blocker.save(update_fields=["status", "ts_updated_at"])
        self.assertEqual(self._status(self.task), "Blocked")
        blocker2.status = "Closed"
        blocker2.save(update_fields=["status", "ts_updated_at"])
        self.assertEqual(self._status(self.task), "Open")

    def test_reopening_blocker_re_blocks_dependent(self):
        self._dep()
        self.blocker.status = "Closed"
        self.blocker.save(update_fields=["status", "ts_updated_at"])
        self.assertEqual(self._status(self.task), "Open")
        self.blocker.status = "Open"
        self.blocker.save(update_fields=["status", "ts_updated_at"])
        self.assertEqual(self._status(self.task), "Blocked")

    # ----- manual-status respect ---------------------------------------

    def test_manually_blocked_task_without_blockers_is_untouched(self):
        # Blocked for a non-task reason (e.g. staffing). No dependency
        # events reference this task, so the automation must never
        # flip it back.
        self.task.status = "Blocked"
        self.task.save(update_fields=["status", "ts_updated_at"])
        # Unrelated blocker activity elsewhere.
        other = TaskMaster.objects.create(
            team=self.team, title="Unrelated", assignee=self.user, reporter=self.user, status="Open"
        )
        self._dep(blocked=other)
        self.blocker.status = "Closed"
        self.blocker.save(update_fields=["status", "ts_updated_at"])
        self.assertEqual(self._status(self.task), "Blocked")

    def test_manual_wip_override_survives_unblock(self):
        # User forces a dep-blocked task to WIP; when the blocker later
        # closes, the auto-revert targets only status == "Blocked", so
        # the manual override stands.
        self._dep()
        self.assertEqual(self._status(self.task), "Blocked")
        self.task.refresh_from_db()
        self.task.status = "WIP"
        self.task.save(update_fields=["status", "ts_updated_at"])
        self.blocker.status = "Closed"
        self.blocker.save(update_fields=["status", "ts_updated_at"])
        self.assertEqual(self._status(self.task), "WIP")
