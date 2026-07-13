"""Tests for the `create_task_plan` composite write tool (BAU Wave 1).

Contract under test:

  * one call creates milestone + task tree + dependencies with the same
    invariants the UI paths produce (backing task double-link, milestone
    FK inheritance for sub-tasks, root_task_id via signal);
  * tasks-only modes (existing milestone / standalone) attach correctly;
  * validation is all-up-front, names the offending index, and rejects
    forward parent refs, deep nesting, cycles, bad enums/dates, and
    non-member assignees;
  * ACL: foreign-team project / non-member requester → ToolError with
    ZERO rows created;
  * atomicity: a failure mid-batch rolls the whole plan back.
"""

from unittest.mock import patch

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools import create_task_plan as ctp_module
from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN

from .test_base import BaseAPITestCase


class CreateTaskPlanTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def run_tool(self, args, ctx=None):
        return CREATE_TASK_PLAN.run(args, ctx or self.ctx)

    def assert_nothing_created(self):
        self.assertEqual(MilestoneMaster.objects.count(), 0)
        self.assertEqual(TaskMaster.objects.count(), 0)
        self.assertEqual(TaskDependency.objects.count(), 0)


class CreateTaskPlanHappyPathTests(CreateTaskPlanTestBase):
    def test_milestone_with_tree_and_dependencies(self):
        out = self.run_tool(
            {
                "project_id": self.project.project_id,
                "milestone": {
                    "title": "Beta launch",
                    "description_markdown": "## Goal\n- ship the beta",
                    "priority": "High",
                    "due_date": "2026-08-31",
                    "assignee_ids": [str(self.user2.id)],
                },
                "tasks": [
                    {"title": "Design review", "priority": "High", "due_date": "2026-08-01"},
                    {
                        "title": "Implementation",
                        "content_markdown": "**Build** the thing",
                        "blocked_by_indexes": [0],
                        "assignee_id": str(self.user2.id),
                    },
                    {"title": "Write unit tests", "parent_index": 1},
                    {"title": "QA pass", "blocked_by_indexes": [1]},
                ],
            }
        )

        milestone = MilestoneMaster.objects.get()
        backing = milestone.task
        self.assertTrue(backing.is_milestone)
        self.assertEqual(milestone.title, "Beta launch")
        self.assertEqual(str(milestone.due_date), "2026-08-31")
        # Milestone body went through markdown_to_blocks (structured, not
        # a plain-text wall).
        self.assertEqual(milestone.description[0]["type"], "heading")
        self.assertEqual(
            set(
                MilestoneAssignees.objects.filter(milestone=milestone).values_list(
                    "user_id", flat=True
                )
            ),
            {self.user2.id},
        )

        tasks = {t.title: t for t in TaskMaster.objects.filter(is_milestone=False)}
        self.assertEqual(len(tasks), 4)
        # Top-level tasks: double-linked (parent = backing AND milestone FK).
        for title in ("Design review", "Implementation", "QA pass"):
            self.assertEqual(tasks[title].parent_task_id, backing.task_id)
            self.assertEqual(tasks[title].milestone_id, milestone.milestone_id)
            self.assertEqual(tasks[title].root_task_id, backing.task_id)
            self.assertEqual(str(tasks[title].reporter_id), str(self.user.id))
        # Auto-"Blocked": a plan task created with an open blocker starts
        # Blocked (services/task_blocking, invoked after bulk_create);
        # unblocked ones start Open.
        self.assertEqual(tasks["Design review"].status, "Open")
        self.assertEqual(tasks["Implementation"].status, "Blocked")
        self.assertEqual(tasks["QA pass"].status, "Blocked")
        # Sub-task: nests under its sibling, inherits the milestone FK.
        sub = tasks["Write unit tests"]
        self.assertEqual(sub.parent_task_id, tasks["Implementation"].task_id)
        self.assertEqual(sub.milestone_id, milestone.milestone_id)
        self.assertEqual(sub.root_task_id, backing.task_id)
        # Markdown body converted; assignee applied.
        impl = tasks["Implementation"]
        self.assertEqual(str(impl.assignee_id), str(self.user2.id))
        self.assertTrue(
            any(s.get("styles", {}).get("bold") for s in impl.content[0]["content"])
        )

        deps = {
            (d.blocker_task.title, d.blocked_task.title) for d in TaskDependency.objects.all()
        }
        self.assertEqual(
            deps,
            {("Design review", "Implementation"), ("Implementation", "QA pass")},
        )

        # Result payload: ids + display_ids for every created row.
        self.assertEqual(out["milestone"]["milestone_id"], milestone.milestone_id)
        self.assertEqual(out["milestone"]["display_id"], backing.display_id)
        self.assertEqual(len(out["tasks"]), 4)
        self.assertTrue(all(t["display_id"].startswith("WRD-") for t in out["tasks"]))
        self.assertEqual(out["dependencies_created"], 2)
        self.assertIn('Created milestone "Beta launch" with 4 task(s)', out["__summary__"])

    def test_tasks_only_under_existing_milestone(self):
        existing = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="v1.0 Public Launch",
        )
        out = self.run_tool(
            {
                "project_id": self.project.project_id,
                "existing_milestone_id": existing.milestone_id,
                "tasks": [{"title": "Analytics review"}, {"title": "User feedback"}],
            }
        )
        existing.refresh_from_db()
        # Backing task was lazily created for the legacy-style milestone.
        self.assertIsNotNone(existing.task_id)
        for t in TaskMaster.objects.filter(is_milestone=False):
            self.assertEqual(t.parent_task_id, existing.task_id)
            self.assertEqual(t.milestone_id, existing.milestone_id)
        self.assertIsNotNone(out["milestone"])
        self.assertIn('in milestone "v1.0 Public Launch"', out["__summary__"])

    def test_standalone_tasks_without_milestone(self):
        out = self.run_tool(
            {
                "project_id": self.project.project_id,
                "tasks": [
                    {"title": "Parent task"},
                    {"title": "Child task", "parent_index": 0},
                ],
            }
        )
        parent = TaskMaster.objects.get(title="Parent task")
        child = TaskMaster.objects.get(title="Child task")
        self.assertIsNone(parent.parent_task_id)
        self.assertIsNone(parent.milestone_id)
        self.assertEqual(parent.root_task_id, parent.task_id)
        self.assertEqual(child.parent_task_id, parent.task_id)
        self.assertIsNone(child.milestone_id)
        self.assertEqual(child.root_task_id, parent.task_id)
        self.assertIsNone(out["milestone"])
        self.assertEqual(MilestoneMaster.objects.count(), 0)

    def test_subtasks_under_existing_task(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v1.0"
        )
        anchor = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            milestone=milestone,
            title="Investigate slow homepage load",
            status="WIP",
        )
        out = self.run_tool(
            {
                "project_id": self.project.project_id,
                "parent_task_id": anchor.task_id,
                "tasks": [
                    {"title": "Rerun trace on real devices"},
                    {"title": "Strip legacy CSS bundle", "blocked_by_indexes": [0]},
                ],
            }
        )
        subs = TaskMaster.objects.filter(parent_task_id=anchor.task_id)
        self.assertEqual(subs.count(), 2)
        for s in subs:
            # Sub-tasks inherit the anchor's milestone and hang off its root.
            self.assertEqual(s.milestone_id, milestone.milestone_id)
            self.assertEqual(s.root_task_id, anchor.task_id)
        self.assertIsNone(out["milestone"])
        self.assertEqual(out["parent_task"]["task_id"], anchor.task_id)
        self.assertEqual(out["parent_task"]["display_id"], anchor.display_id)
        self.assertIn(f"2 sub-task(s) under {anchor.display_id}", out["__summary__"])
        self.assertEqual(out["dependencies_created"], 1)


class CreateTaskPlanValidationTests(CreateTaskPlanTestBase):
    def _base_args(self, **overrides):
        args = {
            "project_id": self.project.project_id,
            "tasks": [{"title": "A"}, {"title": "B"}],
        }
        args.update(overrides)
        return args

    def test_attach_modes_are_mutually_exclusive(self):
        with self.assertRaisesMessage(ToolError, "at most ONE"):
            self.run_tool(
                self._base_args(milestone={"title": "New"}, existing_milestone_id=1)
            )
        with self.assertRaisesMessage(ToolError, "at most ONE"):
            self.run_tool(self._base_args(milestone={"title": "New"}, parent_task_id=1))
        with self.assertRaisesMessage(ToolError, "at most ONE"):
            self.run_tool(self._base_args(existing_milestone_id=1, parent_task_id=1))
        self.assert_nothing_created()

    def test_parent_task_from_other_project_rejected(self):
        other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Elsewhere",
            owner=self.user,
            project_system_user=self.user,
        )
        foreign_parent = TaskMaster.objects.create(
            team=self.team, project=other_project, reporter=self.user, title="Foreign"
        )
        with self.assertRaisesMessage(ToolError, "belongs to project"):
            self.run_tool(self._base_args(parent_task_id=foreign_parent.task_id))
        self.assertEqual(TaskMaster.objects.exclude(title="Foreign").count(), 0)

    def test_deleted_parent_task_rejected(self):
        gone = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Gone",
            is_deleted=True,
        )
        with self.assertRaisesMessage(ToolError, "has been deleted"):
            self.run_tool(self._base_args(parent_task_id=gone.task_id))

    def test_forward_parent_index_rejected(self):
        with self.assertRaisesMessage(ToolError, "EARLIER task"):
            self.run_tool(
                self._base_args(tasks=[{"title": "A", "parent_index": 1}, {"title": "B"}])
            )
        self.assert_nothing_created()

    def test_nesting_under_a_subtask_rejected(self):
        with self.assertRaisesMessage(ToolError, "one level of nesting"):
            self.run_tool(
                self._base_args(
                    tasks=[
                        {"title": "A"},
                        {"title": "B", "parent_index": 0},
                        {"title": "C", "parent_index": 1},
                    ]
                )
            )
        self.assert_nothing_created()

    def test_dependency_cycle_rejected(self):
        with self.assertRaisesMessage(ToolError, "dependency cycle"):
            self.run_tool(
                self._base_args(
                    tasks=[
                        {"title": "A", "blocked_by_indexes": [1]},
                        {"title": "B", "blocked_by_indexes": [0]},
                    ]
                )
            )
        self.assert_nothing_created()

    def test_task_count_cap(self):
        with self.assertRaisesMessage(ToolError, "Too many tasks"):
            self.run_tool(
                self._base_args(tasks=[{"title": f"T{i}"} for i in range(21)])
            )
        self.assert_nothing_created()

    def test_bad_enum_and_date_report_the_index(self):
        with self.assertRaises(ToolError) as caught:
            self.run_tool(
                self._base_args(
                    tasks=[
                        {"title": "A", "priority": "Urgent"},
                        {"title": "B", "due_date": "next friday"},
                        {"title": ""},
                    ]
                )
            )
        msg = str(caught.exception)
        self.assertIn("tasks[0]: `priority`", msg)
        self.assertIn("tasks[1].due_date", msg)
        self.assertIn("tasks[2]: `title` is required", msg)
        self.assert_nothing_created()

    def test_non_member_assignee_rejected(self):
        with self.assertRaisesMessage(ToolError, "not an active member"):
            self.run_tool(
                self._base_args(
                    tasks=[{"title": "A", "assignee_id": "00000000-0000-4000-8000-0000000000ff"}]
                )
            )
        self.assert_nothing_created()

    def test_existing_milestone_from_other_project_rejected(self):
        other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other project",
            owner=self.user,
            project_system_user=self.user,
        )
        foreign_ms = MilestoneMaster.objects.create(
            team=self.team, project=other_project, reporter=self.user, title="Elsewhere"
        )
        with self.assertRaisesMessage(ToolError, "belongs to project"):
            self.run_tool(self._base_args(existing_milestone_id=foreign_ms.milestone_id))
        self.assertEqual(TaskMaster.objects.filter(is_milestone=False).count(), 0)


class CreateTaskPlanAclTests(CreateTaskPlanTestBase):
    def test_foreign_team_project_denied(self):
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        ctx_other = ToolContext(team_id=str(other_team.team_id), user_id=str(self.user2.id))
        with self.assertRaisesMessage(ToolError, "different team"):
            self.run_tool(
                {"project_id": self.project.project_id, "tasks": [{"title": "A"}]},
                ctx=ctx_other,
            )
        self.assert_nothing_created()

    def test_non_project_member_denied(self):
        ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaisesMessage(ToolError, "Not authorized to create tasks"):
            self.run_tool(
                {"project_id": self.project.project_id, "tasks": [{"title": "A"}]},
                ctx=ctx2,
            )
        self.assert_nothing_created()


class CreateTaskPlanAtomicityTests(CreateTaskPlanTestBase):
    def test_mid_batch_failure_rolls_everything_back(self):
        real_create = TaskMaster.objects.create
        calls = {"n": 0}

        def failing_create(**kwargs):
            # The backing task is created first (via create_milestone);
            # then plan tasks 1 and 2 succeed and task 3 blows up.
            calls["n"] += 1
            if calls["n"] == 4:
                raise RuntimeError("boom")
            return real_create(**kwargs)

        with patch.object(ctp_module.TaskMaster.objects, "create", side_effect=failing_create):
            with self.assertRaisesMessage(ToolError, "nothing was created"):
                self.run_tool(
                    {
                        "project_id": self.project.project_id,
                        "milestone": {"title": "Doomed"},
                        "tasks": [{"title": "A"}, {"title": "B"}, {"title": "C"}],
                    }
                )
        self.assert_nothing_created()
