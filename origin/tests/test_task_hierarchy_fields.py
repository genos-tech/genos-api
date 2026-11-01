"""`get_task` and `list_tasks` say where a task sits (MIP-779).

`create_task_plan` returns `parent_task_id` for each task it makes, but no read
tool returned it afterwards, so an agent handed a task id could not see its
milestone and had to walk `list_milestones` and a 50-row `list_tasks` per
milestone. Now `get_task` has `parent_task_id`, `parent_display_id`,
`milestone_id` and `backs_milestone_id`, and every `list_tasks` row has
`parent_task_id`, `milestone_id` and `is_milestone`.
"""

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.list_tasks import LIST_TASKS
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase


class TaskHierarchyFieldsTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Kraken",
            code="KDS",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v1", status="Open"
        )
        self.backing = ensure_backing_task(self.milestone)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _task(self, title="t", **kwargs):
        return TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title=title, **kwargs
        )

    def _get(self, task):
        return FETCH_TASK.run({"task_id": task.task_id}, self.ctx)

    def test_a_top_level_task_in_a_milestone(self):
        task = self._task(milestone=self.milestone, parent_task_id=self.backing.task_id)
        out = self._get(task)
        self.assertEqual(out["milestone_id"], self.milestone.milestone_id)
        self.assertEqual(out["parent_task_id"], self.backing.task_id)
        self.assertEqual(out["parent_display_id"], self.backing.display_id)
        self.assertIsNone(out["backs_milestone_id"])
        self.assertFalse(out["is_milestone"])

    def test_a_subtask_shows_its_parent_task(self):
        parent = self._task("parent", milestone=self.milestone, parent_task_id=self.backing.task_id)
        sub = self._task("sub", milestone=self.milestone, parent_task_id=parent.task_id)
        out = self._get(sub)
        self.assertEqual(out["parent_task_id"], parent.task_id)
        self.assertEqual(out["parent_display_id"], parent.display_id)
        self.assertEqual(out["milestone_id"], self.milestone.milestone_id)

    def test_a_milestones_backing_task_says_which_milestone_it_backs(self):
        out = self._get(self.backing)
        self.assertTrue(out["is_milestone"])
        self.assertEqual(out["backs_milestone_id"], self.milestone.milestone_id)
        # Its own milestone link is the same milestone: only backs_milestone_id
        # and is_milestone tell "is the milestone" from "is in it".
        self.assertEqual(out["milestone_id"], self.milestone.milestone_id)
        self.assertIsNone(out["parent_task_id"])

    def test_a_task_attached_by_parent_only_has_no_milestone_link(self):
        task = self._task(parent_task_id=self.backing.task_id)
        out = self._get(task)
        self.assertIsNone(out["milestone_id"])
        self.assertEqual(out["parent_task_id"], self.backing.task_id)

    def test_a_plain_top_level_task_has_none_of_it(self):
        out = self._get(self._task())
        self.assertIsNone(out["parent_task_id"])
        self.assertIsNone(out["parent_display_id"])
        self.assertIsNone(out["milestone_id"])
        self.assertIsNone(out["backs_milestone_id"])

    def test_a_deleted_milestone_is_not_backed(self):
        MilestoneMaster.objects.filter(pk=self.milestone.pk).update(is_deleted=True)
        self.assertIsNone(self._get(self.backing)["backs_milestone_id"])

    def test_the_parents_label_is_withheld_when_the_caller_cannot_read_it(self):
        """The caller reads the child (assigned to them) but is not a member of
        the parent's project: they get the numeric id and no display id."""
        hidden = ProjectMaster.objects.create(
            team=self.team,
            project_name="Secret",
            code="SEC",
            owner=self.user2,
            project_system_user=self.user2,
        )
        parent = TaskMaster.objects.create(
            team=self.team,
            project=hidden,
            reporter=self.user2,
            title="hidden parent",
            project_task_number=1,
        )
        child = self._task("mine", parent_task_id=parent.task_id, assignee=self.user)
        out = self._get(child)
        self.assertEqual(out["parent_task_id"], parent.task_id)
        self.assertIsNone(out["parent_display_id"])
        self.assertNotIn("SEC", str(out))

    def test_list_tasks_rows_carry_enough_to_rebuild_the_hierarchy(self):
        parent = self._task("parent", milestone=self.milestone, parent_task_id=self.backing.task_id)
        sub = self._task("sub", milestone=self.milestone, parent_task_id=parent.task_id)
        loose = self._task("loose", parent_task_id=self.backing.task_id)
        rows = {
            r["task_id"]: r
            for r in LIST_TASKS.run({"project_id": self.project.project_id, "limit": 50}, self.ctx)[
                "tasks"
            ]
        }
        self.assertEqual(rows[parent.task_id]["parent_task_id"], self.backing.task_id)
        self.assertEqual(rows[parent.task_id]["milestone_id"], self.milestone.milestone_id)
        self.assertEqual(rows[sub.task_id]["parent_task_id"], parent.task_id)
        self.assertIsNone(rows[loose.task_id]["milestone_id"])
        self.assertTrue(rows[self.backing.task_id]["is_milestone"])
        self.assertFalse(rows[sub.task_id]["is_milestone"])
        # The tree is recoverable from the one page.
        children = {}
        for r in rows.values():
            children.setdefault(r["parent_task_id"], []).append(r["task_id"])
        self.assertEqual(set(children[self.backing.task_id]), {parent.task_id, loose.task_id})
        self.assertEqual(children[parent.task_id], [sub.task_id])
