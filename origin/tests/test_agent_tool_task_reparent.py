"""`update_task` `parent_task_id` and `project_id` (MIP-899).

Before this, neither field could be changed by the agent or over MCP: a
task could only be re-filed by a person dragging it in the UI, and a task
nested under the wrong parent stayed there. Both moves have to leave the
links the UI's PUT leaves, or a task is "in" a project for one surface and
not another (the project table nests rows under their parent; the task
list is read with `team` AND `project`). So the core tests drive the same
move through the UI's PUT and through the tool on twin tasks, and compare
where each ends up.
"""

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.tools import ToolError, ToolForbidden
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.update_task import UPDATE_TASK

from .test_agent_tool_task_milestone_move import MilestoneMoveTestBase


def _place(task):
    """Everything that says where a task sits."""
    task.refresh_from_db()
    return (
        task.project_id,
        task.team_id,
        task.milestone_id,
        task.parent_task_id,
        # A top-level task roots at itself, which differs between twins.
        "self" if task.root_task_id == task.task_id else task.root_task_id,
        task.sprint_id,
    )


class ReparentBase(MilestoneMoveTestBase):
    def setUp(self):
        super().setUp()
        self.other = self._project(self.team, "Other", "OTH")

    def _set(self, task, **extra):
        return UPDATE_TASK.run({"task_id": task.task_id, **extra}, self.ctx)

    def _ui_put(self, task, **payload):
        self.authenticate()
        res = self.client.put("/api/v2/task/", {"task_id": task.task_id, **payload}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        return res

    def _unchanged(self, *tasks):
        before = [_place(t) for t in tasks]
        return lambda: self.assertEqual([_place(t) for t in tasks], before)


class UpdateTaskParentTests(ReparentBase):
    def test_reparent_matches_the_uis_put(self):
        """The same move through the UI's PUT and through the tool lands two
        identical tasks in identical places."""
        parent = self._task("parent")
        by_tool, by_ui = self._task("by tool"), self._task("by ui")

        out = self._set(by_tool, parent_task_id=str(parent.task_id))
        self._ui_put(by_ui, parent_task_id=parent.task_id)

        expected = (self.project.project_id, self.team.team_id, None, parent.task_id)
        self.assertEqual(_place(by_tool)[:4], expected)
        self.assertEqual(_place(by_tool), _place(by_ui))
        self.assertEqual(by_tool.root_task_id, parent.task_id)
        self.assertEqual(out["changed_fields"], ["parent"])
        self.assertEqual(out["parent_task_id"], parent.task_id)

    def test_filing_under_a_milestone_row_joins_the_milestone_and_its_sprint(self):
        by_tool, by_ui = self._task("by tool"), self._task("by ui")

        self._set(by_tool, parent_task_id=self.ms_a.task_id)
        self._ui_put(by_ui, parent_task_id=self.ms_a.task_id)

        self.assertEqual(_place(by_tool), _place(by_ui))
        self.assertEqual(by_tool.milestone_id, self.ms_a.milestone_id)
        self.assertEqual(by_tool.sprint_id, self.sprint.sprint_id)

    def test_the_sub_tree_follows_the_task(self):
        """Milestone, sprint and root are denormalized down the tree, so a
        descendant left behind would still point at the place it left."""
        parent = self._task("parent", milestone=self.ms_a, parent_task_id=self.ms_a.task_id)
        top, child, grandchild = self._chain(self.ms_b)

        self._set(top, parent_task_id=parent.task_id)

        for task in (child, grandchild):
            task.refresh_from_db()
            self.assertEqual(task.milestone_id, self.ms_a.milestone_id)
            self.assertEqual(task.sprint_id, self.sprint.sprint_id)
            self.assertEqual(task.root_task_id, self.ms_a.task_id)

    def test_empty_string_makes_the_task_top_level(self):
        by_tool = self._task("by tool", milestone=self.ms_a, parent_task_id=self.ms_a.task_id)
        by_ui = self._task("by ui", milestone=self.ms_a, parent_task_id=self.ms_a.task_id)

        out = self._set(by_tool, parent_task_id="")
        self._ui_put(by_ui, parent_task_id=None)

        self.assertEqual(_place(by_tool), _place(by_ui))
        self.assertEqual(
            _place(by_tool)[2:],
            (None, None, "self", None),
        )
        self.assertEqual(out["changed_fields"], ["parent(cleared)"])

    def test_naming_the_current_parent_is_a_no_op(self):
        parent = self._task("parent")
        child = self._task("child", parent_task_id=parent.task_id)

        out = self._set(child, parent_task_id=str(parent.task_id))

        self.assertEqual(out["changed_fields"], [])

    def test_a_task_cannot_go_under_itself_or_its_own_descendant(self):
        top, child, grandchild = self._chain(self.ms_a)
        unchanged = self._unchanged(top, child, grandchild)

        for target in (top, child, grandchild):
            with self.assertRaises(ToolError) as caught:
                self._set(top, parent_task_id=target.task_id)
            self.assertIn("itself or one of its own sub-tasks", str(caught.exception))
        unchanged()

    def test_a_parent_in_another_project_is_refused(self):
        task = self._task("task")
        elsewhere = TaskMaster.objects.create(
            team=self.team, project=self.other, reporter=self.user, title="x", status="Open"
        )
        unchanged = self._unchanged(task)

        with self.assertRaises(ToolError) as caught:
            self._set(task, parent_task_id=elsewhere.task_id)

        self.assertIn("Move the task into that project first", str(caught.exception))
        unchanged()

    def test_a_deleted_or_missing_parent_is_refused(self):
        task = self._task("task")
        gone = self._task("gone")
        TaskMaster.objects.filter(task_id=gone.task_id).update(status="Deleted")
        removed = self._task("removed")
        TaskMaster.objects.filter(task_id=removed.task_id).update(is_deleted=True)

        for target in (gone.task_id, removed.task_id, 987654):
            with self.assertRaises(ToolError) as caught:
                self._set(task, parent_task_id=target)
            self.assertIn("not found", str(caught.exception))

    def test_a_parent_in_another_team_is_refused(self):
        team_b = TeamMaster.objects.create(
            team_name="B", team_email="b@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=team_b, attendee=self.user2)
        project_b = ProjectMaster.objects.create(
            team=team_b,
            project_name="B",
            code="BBB",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=team_b, project=project_b, attendee=self.user2)
        foreign = TaskMaster.objects.create(
            team=team_b, project=project_b, reporter=self.user2, title="x", status="Open"
        )
        task = self._task("task")

        with self.assertRaises((ToolError, ToolForbidden)):
            self._set(task, parent_task_id=foreign.task_id)
        task.refresh_from_db()
        self.assertIsNone(task.parent_task_id)

    def test_a_parent_the_caller_cannot_read_is_forbidden(self):
        """A parent's title is shown on the task, so a task the caller can't
        read can't be one, even in the same team and project."""
        hidden_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Hidden",
            code="HID",
            owner=self.user2,
            project_system_user=self.user2,
        )
        hidden = TaskMaster.objects.create(
            team=self.team,
            project=hidden_project,
            reporter=self.user2,
            title="secret",
            status="Open",
        )
        task = self._task("task")

        with self.assertRaises(ToolForbidden):
            self._set(task, parent_task_id=hidden.task_id)

    def test_a_milestone_row_cannot_be_filed_under_a_task(self):
        parent = self._task("parent")
        unchanged = self._unchanged(self.ms_a.task)

        with self.assertRaises(ToolError) as caught:
            self._set(
                TaskMaster.objects.get(task_id=self.ms_a.task_id), parent_task_id=parent.task_id
            )

        self.assertIn("is a milestone", str(caught.exception))
        unchanged()

    def test_not_a_number_is_refused(self):
        task = self._task("task")

        with self.assertRaises(ToolError) as caught:
            self._set(task, parent_task_id="the other one")

        self.assertIn("parent_task_id", str(caught.exception))

    def test_a_refused_move_applies_nothing_else_in_the_call(self):
        top, child, _ = self._chain(self.ms_a)

        with self.assertRaises(ToolError):
            self._set(top, parent_task_id=child.task_id, title="Renamed", priority="Critical")

        top.refresh_from_db()
        self.assertNotEqual(top.title, "Renamed")
        self.assertNotEqual(top.priority, "Critical")

    def test_other_fields_in_the_same_call_apply_with_the_move(self):
        parent = self._task("parent")
        task = self._task("task")

        out = self._set(task, parent_task_id=parent.task_id, title="Renamed")

        task.refresh_from_db()
        self.assertEqual((task.title, task.parent_task_id), ("Renamed", parent.task_id))
        self.assertEqual(out["changed_fields"], ["title", "parent"])

    def test_the_move_is_in_the_task_history(self):
        parent = self._task("parent")
        task = self._task("task")

        self._set(task, parent_task_id=parent.task_id)

        self.assertTrue(
            TaskActivity.objects.filter(
                task=task, action_type=TaskActivityActionType.PARENT
            ).exists()
        )


class UpdateTaskProjectTests(ReparentBase):
    def test_project_move_matches_the_uis_put(self):
        by_tool, by_ui = self._task("by tool"), self._task("by ui")
        kid_tool = self._task("kid tool", parent_task_id=by_tool.task_id)
        kid_ui = self._task("kid ui", parent_task_id=by_ui.task_id)

        out = self._set(by_tool, project_id=self.other.project_id)
        self._ui_put(by_ui, project=self.other.project_id)

        self.assertEqual(_place(by_tool), _place(by_ui))
        self.assertEqual(_place(kid_tool)[:2], _place(kid_ui)[:2])
        self.assertEqual(_place(by_tool)[0], self.other.project_id)
        self.assertEqual(_place(kid_tool)[0], self.other.project_id)
        self.assertEqual(out["changed_fields"], ["project"])
        self.assertEqual(out["project_id"], self.other.project_id)

    def test_the_task_is_renumbered_in_the_destination(self):
        existing = TaskMaster.objects.create(
            team=self.team, project=self.other, reporter=self.user, title="e", status="Open"
        )
        task = self._task("task")
        old_id = task.display_id

        out = self._set(task, project_id=self.other.project_id)

        task.refresh_from_db()
        self.assertTrue(task.display_id.startswith("OTH-"))
        self.assertNotEqual(task.project_task_number, existing.project_task_number)
        self.assertEqual(out["display_id"], task.display_id)
        self.assertIn(f"was {old_id}", out["__summary__"])

    def test_tags_are_cleared_on_the_task_and_its_sub_tasks(self):
        tag = [{"tagName": "bug", "tagColor": "#f00", "tagTextColor": "white"}]
        task = self._task("task", tags=tag)
        kid = self._task("kid", parent_task_id=task.task_id, tags=tag)

        self._set(task, project_id=self.other.project_id)

        for t in (task, kid):
            t.refresh_from_db()
            self.assertFalse(t.tags)

    def test_a_task_in_a_milestone_leaves_the_milestone_behind(self):
        by_tool = self._task("by tool", milestone=self.ms_a, parent_task_id=self.ms_a.task_id)
        by_ui = self._task("by ui", milestone=self.ms_a, parent_task_id=self.ms_a.task_id)

        self._set(by_tool, project_id=self.other.project_id)
        # The picker re-sends the stale milestone with the new project.
        self._ui_put(by_ui, project=self.other.project_id, milestone=self.ms_a.milestone_id)

        self.assertEqual(_place(by_tool), _place(by_ui))
        self.assertEqual(_place(by_tool)[2:], (None, None, "self", None))

    def test_the_rows_keyed_to_the_task_cross_with_it(self):
        """Its notes carry their own project, and its dependency edges stay."""
        task, other_task = self._task("task"), self._task("other")
        edge = TaskDependency.objects.create(
            team=self.team, blocker_task=other_task, blocked_task=task
        )
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, owner=self.user, task=task, title="n"
        )

        self._set(task, project_id=self.other.project_id)

        note.refresh_from_db()
        self.assertEqual(note.project_id, self.other.project_id)
        self.assertTrue(TaskDependency.objects.filter(pk=edge.pk).exists())

    def test_a_nested_task_follows_its_parent_and_is_refused(self):
        parent = self._task("parent")
        child = self._task("child", parent_task_id=parent.task_id)
        unchanged = self._unchanged(parent, child)

        with self.assertRaises(ToolError) as caught:
            self._set(child, project_id=self.other.project_id)

        self.assertIn("follows the project of the task or milestone", str(caught.exception))
        self.assertIn("parent_task_id: ''", str(caught.exception))
        unchanged()

    def test_a_nested_task_can_leave_its_parent_and_move_in_one_call(self):
        parent = self._task("parent")
        child = self._task("child", parent_task_id=parent.task_id)

        out = self._set(child, parent_task_id="", project_id=self.other.project_id)

        child.refresh_from_db()
        self.assertEqual((child.project_id, child.parent_task_id), (self.other.project_id, None))
        self.assertEqual(out["changed_fields"], ["parent(cleared)", "project"])

    def test_a_parent_and_a_project_together_are_refused(self):
        parent = self._task("parent")
        task = self._task("task")
        unchanged = self._unchanged(task)

        with self.assertRaises(ToolError) as caught:
            self._set(task, parent_task_id=parent.task_id, project_id=self.other.project_id)

        self.assertIn("two calls", str(caught.exception))
        unchanged()

    def test_a_milestone_and_a_move_in_one_call_are_refused(self):
        task = self._task("task")
        unchanged = self._unchanged(task)

        for extra in (
            {"project_id": self.other.project_id},
            {"parent_task_id": self._task("p").task_id},
        ):
            with self.assertRaises(ToolError) as caught:
                self._set(task, milestone_id=str(self.ms_a.milestone_id), **extra)
            self.assertIn("send one of them per call", str(caught.exception))
        unchanged()

    def test_a_project_the_caller_is_not_in_is_not_found(self):
        hidden = ProjectMaster.objects.create(
            team=self.team,
            project_name="Hidden",
            code="HID",
            owner=self.user2,
            project_system_user=self.user2,
        )
        task = self._task("task")
        unchanged = self._unchanged(task)

        for project_id in (hidden.project_id, 987654):
            with self.assertRaises(ToolError) as caught:
                self._set(task, project_id=project_id)
            self.assertIn("not found", str(caught.exception))
        unchanged()

    def test_a_project_in_another_team_is_refused(self):
        team_b = TeamMaster.objects.create(
            team_name="B", team_email="b@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=team_b, attendee=self.user)
        project_b = ProjectMaster.objects.create(
            team=team_b,
            project_name="B",
            code="BBB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=team_b, project=project_b, attendee=self.user)
        task = self._task("task")
        unchanged = self._unchanged(task)

        with self.assertRaises(ToolError) as caught:
            self._set(task, project_id=project_b.project_id)

        self.assertIn("different team", str(caught.exception))
        unchanged()

    def test_a_milestone_row_is_refused(self):
        row = TaskMaster.objects.get(task_id=self.ms_a.task_id)

        with self.assertRaises(ToolError) as caught:
            self._set(row, project_id=self.other.project_id)

        self.assertIn("is a milestone", str(caught.exception))

    def test_naming_the_current_project_is_a_no_op(self):
        task = self._task("task")

        out = self._set(task, project_id=self.project.project_id)

        self.assertEqual(out["changed_fields"], [])

    def test_the_move_is_in_the_task_history(self):
        task = self._task("task")

        self._set(task, project_id=self.other.project_id)

        self.assertTrue(
            TaskActivity.objects.filter(
                task=task, action_type=TaskActivityActionType.PROJECT
            ).exists()
        )

    def test_a_refused_move_applies_nothing_else_in_the_call(self):
        parent = self._task("parent")
        child = self._task("child", parent_task_id=parent.task_id)

        with self.assertRaises(ToolError):
            self._set(child, project_id=self.other.project_id, title="Renamed")

        child.refresh_from_db()
        self.assertNotEqual(child.title, "Renamed")


class CreateTaskParentTests(ReparentBase):
    def _create(self, **extra):
        return CREATE_TASK.run(
            {"title": "New sub-task", "project_id": self.project.project_id, **extra}, self.ctx
        )

    def _count(self):
        return TaskMaster.objects.filter(title="New sub-task").count()

    def test_a_new_task_is_filed_under_its_parent(self):
        parent = self._task("parent")

        out = self._create(parent_task_id=str(parent.task_id))

        made = TaskMaster.objects.get(task_id=out["task_id"])
        self.assertEqual((made.parent_task_id, made.root_task_id), (parent.task_id, parent.task_id))
        self.assertIsNone(made.milestone_id)
        self.assertEqual(out["parent_task_id"], parent.task_id)
        self.assertIn(f"under {parent.display_id}", out["__summary__"])

    def test_it_joins_the_milestone_and_sprint_its_parent_lives_in(self):
        parent = self._task("parent", milestone=self.ms_a, parent_task_id=self.ms_a.task_id)

        out = self._create(parent_task_id=parent.task_id)

        made = TaskMaster.objects.get(task_id=out["task_id"])
        self.assertEqual(made.milestone_id, self.ms_a.milestone_id)
        self.assertEqual(made.sprint_id, self.sprint.sprint_id)
        self.assertEqual(made.root_task_id, self.ms_a.task_id)
        self.assertEqual(out["milestone_id"], self.ms_a.milestone_id)

    def test_under_a_milestone_row_is_the_same_as_filing_in_the_milestone(self):
        by_parent = TaskMaster.objects.get(
            task_id=self._create(parent_task_id=self.ms_a.task_id)["task_id"]
        )
        by_milestone = TaskMaster.objects.get(
            task_id=self._create(milestone_id=self.ms_a.milestone_id)["task_id"]
        )

        self.assertEqual(
            (by_parent.milestone_id, by_parent.parent_task_id, by_parent.sprint_id),
            (by_milestone.milestone_id, by_milestone.parent_task_id, by_milestone.sprint_id),
        )

    def _assert_answer_is_the_row(self, out):
        """What the tool answered is what was written: read the row back from
        the database, not from the instance the tool created."""
        row = TaskMaster.objects.values("parent_task_id", "milestone_id").get(
            task_id=out["task_id"]
        )
        self.assertEqual(out["parent_task_id"], row["parent_task_id"])
        self.assertEqual(out["milestone_id"], row["milestone_id"])
        return row

    def test_the_answer_for_a_task_filed_in_a_milestone_names_its_parent(self):
        """MIP-924: `milestone_id` hangs the task under the milestone's backing
        row, and the answer said it was top-level (`parent_task_id: null`)."""
        out = self._create(milestone_id=self.ms_a.milestone_id)

        row = self._assert_answer_is_the_row(out)
        self.assertEqual(row["parent_task_id"], self.ms_a.task_id)
        self.assertEqual(out["parent_task_id"], self.ms_a.task_id)
        self.assertEqual(out["milestone_id"], self.ms_a.milestone_id)

    def test_the_answer_for_a_task_filed_under_a_parent_matches_the_row(self):
        parent = self._task("parent", milestone=self.ms_a, parent_task_id=self.ms_a.task_id)

        out = self._create(parent_task_id=parent.task_id)

        row = self._assert_answer_is_the_row(out)
        self.assertEqual(row["parent_task_id"], parent.task_id)
        # It joined the milestone its parent lives in, and the answer says so.
        self.assertEqual(out["milestone_id"], self.ms_a.milestone_id)

    def test_the_answer_for_a_task_filed_under_a_milestone_row_matches_the_row(self):
        out = self._create(parent_task_id=self.ms_a.task_id)

        row = self._assert_answer_is_the_row(out)
        self.assertEqual(row["parent_task_id"], self.ms_a.task_id)
        self.assertEqual(out["milestone_id"], self.ms_a.milestone_id)

    def test_the_answer_for_a_task_filed_under_a_parent_outside_any_milestone(self):
        parent = self._task("parent")

        out = self._create(parent_task_id=parent.task_id)

        row = self._assert_answer_is_the_row(out)
        self.assertEqual(row["parent_task_id"], parent.task_id)
        self.assertIsNone(out["milestone_id"])

    def test_a_top_level_task_answers_no_parent_and_no_milestone(self):
        out = self._create()

        row = self._assert_answer_is_the_row(out)
        self.assertIsNone(row["parent_task_id"])
        self.assertIsNone(out["parent_task_id"])
        self.assertIsNone(out["milestone_id"])

    def test_a_parent_in_another_project_creates_nothing(self):
        elsewhere = TaskMaster.objects.create(
            team=self.team, project=self.other, reporter=self.user, title="x", status="Open"
        )

        with self.assertRaises(ToolError) as caught:
            self._create(parent_task_id=elsewhere.task_id)

        self.assertIn("not " + str(self.project.project_id), str(caught.exception))
        self.assertEqual(self._count(), 0)

    def test_a_milestone_and_a_parent_together_create_nothing(self):
        parent = self._task("parent")

        with self.assertRaises(ToolError) as caught:
            self._create(parent_task_id=parent.task_id, milestone_id=self.ms_a.milestone_id)

        self.assertIn("send one of them", str(caught.exception))
        self.assertEqual(self._count(), 0)

    def test_a_parent_the_caller_cannot_read_creates_nothing(self):
        hidden_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Hidden",
            code="HID",
            owner=self.user2,
            project_system_user=self.user2,
        )
        hidden = TaskMaster.objects.create(
            team=self.team,
            project=hidden_project,
            reporter=self.user2,
            title="secret",
            status="Open",
        )

        with self.assertRaises(ToolForbidden):
            self._create(parent_task_id=hidden.task_id)
        self.assertEqual(self._count(), 0)

    def test_a_missing_parent_creates_nothing(self):
        with self.assertRaises(ToolError):
            self._create(parent_task_id=987654)
        with self.assertRaises(ToolError):
            self._create(parent_task_id="the other one")
        self.assertEqual(self._count(), 0)
