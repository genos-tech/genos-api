"""A task's history must not name a project, milestone, sprint or parent the reader may not see
(MIP-2523), in the app's feed and in the agent / MCP `get_task_history`.

`self.user` is on both projects; `self.user2` only on the destination.
"""

from django.core.cache import cache

from origin.middleware.current_user import clear_current_user
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.get_task_history import GET_TASK_HISTORY

from .test_base import BaseAPITestCase

SECRET = ("Vault", "Vault milestone", "Vault sprint", "Vault parent", "VLT-")


class HistoryHiddenLabelTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.old = self._project("Vault", "VLT", members=[self.user], is_hidden=True)
        self.new = self._project("Open", "OPN", members=[self.user, self.user2])
        self.sprint = Sprint.objects.create(
            team=self.team,
            project=self.old,
            name="Vault sprint",
            sequence_number=1,
            start_date="2026-01-01",
            end_date="2026-01-14",
        )
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.old, title="Vault milestone", reporter=self.user
        )
        self.parent = self._task("Vault parent", self.old)
        self.task = self._task("moved", self.old)
        self.task.milestone = self.milestone
        self.task.sprint = self.sprint
        self.task.parent_task_id = self.parent.task_id
        self.task.save()
        # The move: out of the hidden project, leaving its milestone, sprint and parent.
        self.task.project = self.new
        self.task.milestone = None
        self.task.sprint = None
        self.task.parent_task_id = None
        self.task.save()

    def tearDown(self):
        clear_current_user()
        super().tearDown()

    def _project(self, name, code, *, members, is_hidden=False):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
            is_private=is_hidden,
            is_hidden=is_hidden,
        )
        for member in members:
            ProjectMembers.objects.create(team=self.team, project=project, attendee=member)
        return project

    def _task(self, title, project, **kwargs):
        return TaskMaster.objects.create(
            team=self.team, project=project, title=title, status="Open", **kwargs
        )

    def _feed(self, user, task=None):
        self.authenticate(user)
        res = self.client.get(
            "/api/v2/task/activity/",
            {"team_id": str(self.team.team_id), "task_id": (task or self.task).task_id},
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def _tool(self, user, task=None):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))
        return GET_TASK_HISTORY.run({"task_id": (task or self.task).task_id}, ctx)["history"]

    def _row(self, feed, action):
        return next(r for r in feed if r["actionType"] == action and r["oldValue"] is not None)

    def test_a_reader_off_the_hidden_project_sees_none_of_its_names(self):
        for read in (self._feed, self._tool):
            with self.subTest(read=read.__name__):
                body = str(read(self.user2))
                for name in SECRET:
                    self.assertNotIn(name, body)
                # The destination, which they are on, is still named.
                self.assertIn("Open", body)

    def test_the_ids_stay_so_the_row_still_reads(self):
        row = self._row(self._feed(self.user2), TaskActivityActionType.PROJECT)
        self.assertEqual(row["oldValue"], self.old.project_id)
        self.assertNotIn("oldLabel", row["metadata"])
        self.assertEqual(row["metadata"]["newLabel"], "Open")

    def test_a_member_of_both_sees_every_name(self):
        for read in (self._feed, self._tool):
            with self.subTest(read=read.__name__):
                body = str(read(self.user))
                for name in SECRET:
                    # The tool shows a parent by its display id, not its title.
                    if name == "Vault parent" and read == self._tool:
                        continue
                    self.assertIn(name, body)

    def test_the_stored_rows_are_not_changed(self):
        self._feed(self.user2)
        self._tool(self.user2)
        row = TaskActivity.objects.filter(
            task=self.task, action_type=TaskActivityActionType.PROJECT
        ).get()
        self.assertEqual(row.metadata["oldLabel"], "Vault")

    def test_a_visible_project_keeps_its_name_but_not_its_milestone(self):
        """Not Hidden: the name is listed to everyone on the team; what is in it is not."""
        ProjectMaster.objects.filter(pk=self.old.pk).update(is_hidden=False)
        body = str(self._feed(self.user2))
        self.assertIn("'Vault'", body)
        for name in ("Vault milestone", "Vault sprint", "Vault parent", "VLT-"):
            self.assertNotIn(name, body)

    def test_the_tasks_own_project_is_named_to_a_reader_outside_its_member_list(self):
        task = self._task("assigned", self.old, assignee=self.user2)
        task.milestone = self.milestone
        task.save()
        row = next(
            r
            for r in self._feed(self.user2, task)
            if r["actionType"] == TaskActivityActionType.MILESTONE_LINK
        )
        self.assertEqual(row["metadata"]["newLabel"], "Vault milestone")
