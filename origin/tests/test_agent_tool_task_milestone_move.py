"""`update_task` / `update_tasks_bulk` `milestone_id` (MIP-692).

Before this, neither write tool could re-file a task: a task created
outside a milestone stayed outside it, and the only fix was a human
dragging it in the UI. The move has to produce the linkage the
milestone picker produces — `milestone_id`, `parent_task_id` and
`root_task_id` on the milestone's backing task, `sprint_id` from the
milestone — or the task is "in" the milestone for the rollups but not
nested under it in the table (or the other way round), and its sub-tasks
stay behind. So the core assertion compares against the UI's own PUT.
"""

from unittest import mock

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext, ToolError, update_tasks_bulk
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.search_engine.agent.tools.update_tasks_bulk import UPDATE_TASKS_BULK
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase


def _linkage(task):
    task.refresh_from_db()
    return (task.milestone_id, task.parent_task_id, task.root_task_id, task.sprint_id)


class MilestoneMoveTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = self._project(self.team, "Release", "REL")
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S1",
            sequence_number=1,
            start_date="2026-10-01",
            end_date="2026-10-14",
        )
        self.ms_a = self._milestone("MS A", sprint=self.sprint)
        self.ms_b = self._milestone("MS B")

    def _project(self, team, name, code):
        project = ProjectMaster.objects.create(
            team=team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=team, project=project, attendee=self.user)
        return project

    def _milestone(self, title, project=None, team=None, backed=True, **kwargs):
        milestone = MilestoneMaster.objects.create(
            team=team or self.team,
            project=project or self.project,
            title=title,
            reporter=self.user,
            **kwargs,
        )
        if backed:
            ensure_backing_task(milestone)
            milestone.refresh_from_db()
        return milestone

    def _task(self, title, **kwargs):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title=title,
            status="Open",
            **kwargs,
        )

    def _chain(self, milestone):
        """milestone → top → child → grandchild, built the way the product
        builds it (the create signal derives each root)."""
        top = self._task("top", milestone=milestone, parent_task_id=milestone.task_id)
        child = self._task("child", milestone=milestone, parent_task_id=top.task_id)
        grandchild = self._task("grandchild", milestone=milestone, parent_task_id=child.task_id)
        return top, child, grandchild

    def _move(self, task, milestone_id, **extra):
        return UPDATE_TASK.run(
            {"task_id": task.task_id, "milestone_id": milestone_id, **extra}, self.ctx
        )


class UpdateTaskMilestoneMoveTests(MilestoneMoveTestBase):
    def test_move_in_matches_the_milestone_picker(self):
        """The same move through the UI's PUT and through the tool lands
        two identical tasks in identical places."""
        by_tool = self._task("by tool")
        by_ui = self._task("by ui")

        out = self._move(by_tool, str(self.ms_a.milestone_id))
        self.authenticate()
        res = self.client.put(
            "/api/v2/task/",
            {
                "task_id": by_ui.task_id,
                "project": self.project.project_id,
                "milestone": self.ms_a.milestone_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        expected = (
            self.ms_a.milestone_id,
            self.ms_a.task_id,
            self.ms_a.task_id,
            self.sprint.sprint_id,
        )
        self.assertEqual(_linkage(by_tool), expected)
        self.assertEqual(_linkage(by_ui), expected)
        self.assertEqual(out["changed_fields"], ["milestone"])
        self.assertEqual(out["milestone_id"], self.ms_a.milestone_id)

    def test_integer_id_is_accepted(self):
        task = self._task("t")
        self._move(task, self.ms_a.milestone_id)
        self.assertEqual(_linkage(task)[0], self.ms_a.milestone_id)

    def test_sub_tree_moves_with_the_task_and_keeps_its_edges(self):
        top, child, grandchild = self._chain(self.ms_a)

        self._move(top, str(self.ms_b.milestone_id))

        self.assertEqual(
            _linkage(top), (self.ms_b.milestone_id, self.ms_b.task_id, self.ms_b.task_id, None)
        )
        for row, parent in ((child, top), (grandchild, child)):
            self.assertEqual(
                _linkage(row),
                (self.ms_b.milestone_id, parent.task_id, self.ms_b.task_id, None),
                f"{row.title} was left behind or flattened",
            )

    def test_detach_reroots_the_sub_tree_on_the_task(self):
        top, child, grandchild = self._chain(self.ms_a)

        out = self._move(top, "")

        self.assertEqual(out["changed_fields"], ["milestone(cleared)"])
        self.assertEqual(_linkage(top), (None, None, top.task_id, None))
        for row, parent in ((child, top), (grandchild, child)):
            self.assertEqual(_linkage(row), (None, parent.task_id, top.task_id, None))

    def test_already_in_the_milestone_is_a_noop_that_keeps_the_chain(self):
        """Re-running the bridge would lift `child` out from under `top`
        and onto the milestone row itself."""
        _top, child, _grandchild = self._chain(self.ms_a)
        before = _linkage(child)

        out = self._move(child, str(self.ms_a.milestone_id))

        self.assertEqual(out["changed_fields"], [])
        self.assertEqual(_linkage(child), before)

    def test_move_combines_with_field_changes(self):
        task = self._task("t")

        out = self._move(task, str(self.ms_a.milestone_id), status="WIP", priority="High")

        task.refresh_from_db()
        self.assertEqual((task.status, task.priority), ("WIP", "High"))
        self.assertEqual(task.milestone_id, self.ms_a.milestone_id)
        self.assertEqual(out["changed_fields"], ["status", "priority", "milestone"])

    def test_milestone_without_a_backing_row_gets_one(self):
        """A legacy milestone has no backing task until first access; the
        bridge alone would no-op and report nothing moved."""
        legacy = self._milestone("Legacy", backed=False)
        self.assertIsNone(legacy.task_id)
        task = self._task("t")

        self._move(task, str(legacy.milestone_id))

        legacy.refresh_from_db()
        self.assertIsNotNone(legacy.task_id)
        self.assertEqual(_linkage(task)[:3], (legacy.milestone_id, legacy.task_id, legacy.task_id))

    def test_refusals_leave_the_task_untouched(self):
        other_project = self._project(self.team, "Other", "OTH")
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        foreign_project = self._project(other_team, "Foreign", "FOR")
        deleted = self._milestone("Gone", is_deleted=True)
        cases = {
            "other project": (
                self._milestone("X", project=other_project).milestone_id,
                "belongs to project",
            ),
            "other team": (
                self._milestone("Y", project=foreign_project, team=other_team).milestone_id,
                "different team",
            ),
            "unknown": (987654, "not found"),
            "deleted": (deleted.milestone_id, "not found"),
            "garbage": ("soon", "must be an integer"),
        }
        task = self._task("t", priority="Low")
        before = _linkage(task)
        for label, (milestone_id, message) in cases.items():
            with self.subTest(label):
                with self.assertRaisesMessage(ToolError, message):
                    self._move(task, milestone_id, priority="High")
                task.refresh_from_db()
                self.assertEqual(_linkage(task), before)
                self.assertEqual(
                    task.priority, "Low", "a refused move still saved the other fields"
                )

    def test_milestone_backing_task_cannot_be_moved_or_detached(self):
        backing = TaskMaster.objects.get(task_id=self.ms_a.task_id)
        for target in (str(self.ms_b.milestone_id), ""):
            with self.subTest(target=target):
                with self.assertRaisesMessage(ToolError, "is a milestone"):
                    self._move(backing, target)
        backing.refresh_from_db()
        self.assertEqual(backing.milestone_id, self.ms_a.milestone_id)

    def test_moving_a_task_under_a_milestone_below_it_is_refused(self):
        """Drifted data: a milestone's backing row hanging under the task.
        Filing the task there would loop the tree."""
        task = self._task("t")
        TaskMaster.objects.filter(task_id=self.ms_b.task_id).update(parent_task_id=task.task_id)

        with self.assertRaisesMessage(ToolError, "cycle"):
            self._move(task, str(self.ms_b.milestone_id))


class UpdateTasksBulkMilestoneMoveTests(MilestoneMoveTestBase):
    def _bulk(self, *rows):
        return UPDATE_TASKS_BULK.run(
            {"updates": [{"rationale": "Belongs to this release.", **row} for row in rows]},
            self.ctx,
        )

    def test_files_a_batch_under_a_milestone(self):
        top, child, _grandchild = self._chain(self.ms_b)
        loose = self._task("loose")

        out = self._bulk(
            {"task_id": top.task_id, "milestone_id": str(self.ms_a.milestone_id)},
            {"task_id": loose.task_id, "milestone_id": self.ms_a.milestone_id, "priority": "High"},
        )

        backing = self.ms_a.task_id
        self.assertEqual(
            _linkage(top), (self.ms_a.milestone_id, backing, backing, self.sprint.sprint_id)
        )
        self.assertEqual(
            _linkage(loose), (self.ms_a.milestone_id, backing, backing, self.sprint.sprint_id)
        )
        self.assertEqual(
            _linkage(child), (self.ms_a.milestone_id, top.task_id, backing, self.sprint.sprint_id)
        )
        self.assertEqual(
            [row["changed_fields"] for row in out["updated"]],
            [["milestone"], ["priority", "milestone"]],
        )

    def test_detach_carries_the_sub_tree_out(self):
        top, child, _grandchild = self._chain(self.ms_a)

        out = self._bulk({"task_id": top.task_id, "milestone_id": ""})

        self.assertEqual(_linkage(top), (None, None, top.task_id, None))
        self.assertEqual(_linkage(child), (None, top.task_id, top.task_id, None))
        self.assertEqual(out["updated"][0]["changed_fields"], ["milestone(cleared)"])

    def test_already_in_the_milestone_is_reported_as_noop(self):
        _top, child, _grandchild = self._chain(self.ms_a)
        before = _linkage(child)

        out = self._bulk({"task_id": child.task_id, "milestone_id": str(self.ms_a.milestone_id)})

        self.assertEqual(out["updated"], [])
        self.assertEqual(out["noops"], [child.task_id])
        self.assertEqual(_linkage(child), before)

    def test_one_bad_milestone_rejects_the_whole_batch(self):
        good = self._task("good")
        bad = self._task("bad")
        other_project = self._project(self.team, "Other", "OTH")
        foreign = self._milestone("X", project=other_project)

        with self.assertRaisesMessage(ToolError, "updates[1]: Milestone"):
            self._bulk(
                {"task_id": good.task_id, "milestone_id": str(self.ms_a.milestone_id)},
                {"task_id": bad.task_id, "milestone_id": str(foreign.milestone_id)},
            )

        self.assertEqual(_linkage(good)[0], None, "the valid row was applied anyway")

    def test_failure_mid_apply_rolls_back_the_moves(self):
        """All-or-nothing covers the hierarchy writes too: they run inside
        the batch's transaction."""
        first = self._task("first")
        second = self._task("second")
        real_move = update_tasks_bulk.move_task_to_milestone
        calls = []

        def flaky(task, milestone_id):
            calls.append(task.task_id)
            if len(calls) == 2:
                raise RuntimeError("db went away")
            return real_move(task, milestone_id)

        with mock.patch.object(update_tasks_bulk, "move_task_to_milestone", flaky):
            with self.assertRaisesMessage(ToolError, "nothing was applied"):
                self._bulk(
                    {"task_id": first.task_id, "milestone_id": str(self.ms_a.milestone_id)},
                    {"task_id": second.task_id, "milestone_id": str(self.ms_a.milestone_id)},
                )

        self.assertEqual(_linkage(first)[0], None)
