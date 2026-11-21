"""Regression (MIP-1726): a dependency deleted by a cascade must not write history for a task that goes.

A `TaskDependency` is a CASCADE child of its blocker and of its blocked task. When a project (or
a task, or a team) is deleted, its dependency rows go in the same cascade. `dependency_deleted`
then re-derives the blocked task's status (`sync_blocked_status`), and that save writes a status
"TaskActivity" row. When the blocked task is itself being deleted, the row names a task that the
same cascade removes a moment later. Django collected the cascade before the row existed, so the
row survives, and the deferred foreign key rejects it at COMMIT.

`delete_project` hid this with `suppress_task_activity()`. These tests go through the plain
`.delete()` the cascade uses, so the cause is what is tested, not the wrapper around it.

Every test is a `TransactionTestCase`: a deferred constraint is only checked at a real COMMIT,
and `TestCase` rolls back without committing, so the bug is invisible under `TestCase`.
"""

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.tests.transaction_support import CheapFlushTransactionTestCase

User = get_user_model()


def _delete_project_cascade(project):
    """What `delete_project` does to the project, minus its activity suppression.

    A project's chat is PROTECT on the project, so the chat is let go first, as the service does.
    What is left is the plain cascade that writes the history row this file is about.
    """
    Channel.objects.filter(project=project).update(is_deleted=True, project=None)
    project.delete()


class DependencyDeleteInCascadeTests(CheapFlushTransactionTestCase):
    def _team(self):
        user = User.objects.create_user(
            username="cascade1726", email="cascade1726@example.com", password="x"
        )
        team = TeamMaster.objects.create(
            team_name="Cascade 1726", team_email="cascade1726@example.com", owner=user
        )
        TeamMembers.objects.create(team=team, attendee=user)
        return user, team

    def _blocked_pair(self, team, user, project):
        blocker = TaskMaster.objects.create(
            team=team, project=project, title="Blocker", assignee=user, reporter=user, status="Open"
        )
        blocked = TaskMaster.objects.create(
            team=team, project=project, title="Blocked", assignee=user, reporter=user, status="Open"
        )
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=blocked, team=team)
        blocked.refresh_from_db()
        self.assertEqual(blocked.status, "Blocked")  # the setup really blocks it
        return blocker, blocked

    def test_deleting_the_starter_project_commits(self):
        # The exact case of MIP-1726: the starter project, whose own task blocks another one.
        from origin.services.starter_seeder import create_starter_workspace

        user, team = self._team()
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            create_starter_workspace(user, team)
        project = ProjectMaster.objects.get(team=team, project_name="Getting started with Genos")
        blocked_ids = list(
            TaskMaster.objects.filter(project=project, status="Blocked").values_list(
                "task_id", flat=True
            )
        )
        self.assertTrue(blocked_ids)  # the setup really blocks a task

        _delete_project_cascade(project)  # raised IntegrityError at COMMIT before the fix

        self.assertFalse(ProjectMaster.objects.filter(pk=project.pk).exists())
        self.assertFalse(TaskMaster.objects.filter(task_id__in=blocked_ids).exists())
        self.assertFalse(TaskActivity.objects.filter(task_id__in=blocked_ids).exists())

    def test_deleting_the_blocker_task_still_unblocks_a_survivor(self):
        # The fix must not skip the resync when the blocked task survives: deleting only the
        # blocker in a task delete leaves the blocked task, which has to become Open again.
        user, team = self._team()
        project = ProjectMaster.objects.create(
            team=team, project_name="Survivor project", owner=user
        )
        blocker, blocked = self._blocked_pair(team, user, project)

        blocker.delete()

        blocked.refresh_from_db()
        self.assertEqual(blocked.status, "Open")
        self.assertTrue(TaskMaster.objects.filter(task_id=blocked.task_id).exists())

    def test_deleting_a_blocker_in_another_project_resyncs_the_blocked_task(self):
        # The blocked task is in another project, which survives, so its status must still follow.
        user, team = self._team()
        blocker_project = ProjectMaster.objects.create(
            team=team, project_name="Blocker side", owner=user
        )
        blocked_project = ProjectMaster.objects.create(
            team=team, project_name="Blocked side", owner=user
        )
        blocker = TaskMaster.objects.create(
            team=team,
            project=blocker_project,
            title="B",
            assignee=user,
            reporter=user,
            status="Open",
        )
        blocked = TaskMaster.objects.create(
            team=team,
            project=blocked_project,
            title="X",
            assignee=user,
            reporter=user,
            status="Open",
        )
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=blocked, team=team)
        blocked.refresh_from_db()
        self.assertEqual(blocked.status, "Blocked")

        _delete_project_cascade(blocker_project)

        blocked.refresh_from_db()
        self.assertEqual(blocked.status, "Open")
        self.assertTrue(ProjectMaster.objects.filter(pk=blocked_project.pk).exists())


class StarterDeleteThroughTheAppTests(CheapFlushTransactionTestCase):
    """MIP-1735: the app's own delete of the starter project, through the endpoint the page calls.

    `DELETE /api/v2/project/` is what the project menu sends. The test commits for real (a
    `TransactionTestCase`), so a COMMIT-time foreign key failure is seen as the person would see it.
    """

    def test_the_owner_deletes_the_starter_project_and_it_commits(self):
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken

        from origin.services.starter_seeder import create_starter_workspace

        user = User.objects.create_user(
            username="starter1735", email="starter1735@example.com", password="x"
        )
        team = TeamMaster.objects.create(
            team_name="Starter 1735", team_email="starter1735@example.com", owner=user
        )
        TeamMembers.objects.create(team=team, attendee=user)
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            create_starter_workspace(user, team)
        project = ProjectMaster.objects.get(team=team, project_name="Getting started with Genos")

        client = APIClient()
        token = RefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        with (
            mock.patch("origin.search_engine.purge.purge_entities"),
            mock.patch("origin.search_engine.purge.purge_chat"),
        ):
            response = client.delete(
                f"/api/v2/project/?team_id={team.team_id}&project_id={project.project_id}"
            )

        self.assertEqual(response.status_code, 204, response.content)
        self.assertFalse(ProjectMaster.objects.filter(pk=project.pk).exists())
        self.assertFalse(TaskActivity.objects.filter(project_id=project.pk).exists())
