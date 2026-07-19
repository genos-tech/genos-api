"""Regression: demo teardown must not orphan a TaskActivity row.

Tearing down a demo team deletes its tasks in one transaction. Deleting a
`TaskDependency` (a CASCADE child of the task) fires
`dependency_deleted` → `sync_blocked_status()`, which flips a now-unblocked
task Blocked→Open with `task.save(update_fields=["status", ...])`. That save
fires `post_save` on TaskMaster, which records a STATUS `TaskActivity` for a
task that is being deleted in the same cascade — after Django's delete
collector has already fixed its delete set, so the new row escapes the
cascade and orphans against origin_taskmaster.

Because Django FKs are DEFERRABLE INITIALLY DEFERRED, the violation only
surfaces at COMMIT:

    IntegrityError: update or delete on table "origin_taskmaster" violates
    foreign key constraint "origin_taskactivity_task_id_..._fk_origin_ta"
    DETAIL: Key (task_id)=(N) is still referenced from table "origin_taskactivity".

(The comment/attachment delete-receivers are guarded by `_task_or_none`,
which no-ops once the task row is gone — so they do NOT orphan. The
post_save resync path is not guarded, which is why THIS is the trigger.)

`origin.services.activity_suppression.suppress_task_activity()`, applied in
`delete_demo_team_data`, makes all these audit inserts no-ops for the
teardown.

MUST be a TransactionTestCase: a deferred constraint is only checked at a
real COMMIT, and TestCase rolls back without ever committing, so the bug is
invisible under TestCase.
"""

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services.demo_seeder import delete_demo_environment

User = get_user_model()


class DemoCleanupActivityOrphanTests(TransactionTestCase):
    def _demo_env_with_blocked_dependency(self):
        user = User.objects.create_user(
            username="demo_cleanup_user",
            email="demo_cleanup@example.com",
            password="x",
            is_demo=True,
        )
        team = TeamMaster.objects.create(
            team_name="Demo Cleanup Team",
            team_email="demo_cleanup_team@example.com",
            owner=user,
            is_demo=True,
        )
        TeamMembers.objects.create(team=team, attendee=user)
        blocker = TaskMaster.objects.create(
            team=team, title="Blocker", assignee=user, reporter=user, status="Open"
        )
        blocked = TaskMaster.objects.create(
            team=team, title="Blocked", assignee=user, reporter=user, status="Open"
        )
        # Creating the dependency auto-blocks `blocked` (post_save →
        # sync_blocked_status flips it Open→Blocked).
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=blocked, team=team)
        blocked.refresh_from_db()
        return user, team, blocker, blocked

    def test_teardown_with_blocked_dependency_commits_cleanly(self):
        user, team, _blocker, blocked = self._demo_env_with_blocked_dependency()
        # Sanity: the auto-block fired, so teardown WILL trigger a
        # Blocked→Open resync (and its orphan-prone activity) pre-fix.
        self.assertEqual(blocked.status, "Blocked")

        # Pre-fix this raises IntegrityError at the COMMIT of
        # delete_demo_environment's transaction.
        delete_demo_environment(user)

        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(TeamMaster.objects.filter(team_id=team.team_id).exists())
        self.assertFalse(TaskMaster.objects.filter(task_id=blocked.task_id).exists())
        self.assertFalse(TaskActivity.objects.filter(task_id=blocked.task_id).exists())
