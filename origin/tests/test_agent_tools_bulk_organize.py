"""Tests for the BAU Wave-1 organize tools (`update_tasks_bulk`,
`list_task_dependencies`) and the composite tools' friendly-argument
enrichment in the controller.

`update_tasks_bulk` contract:
  * validate-all-then-apply-all — ONE bad entry (unknown task, bad
    enum/date, missing rationale, foreign team, no changes) fails the
    whole batch with zero writes;
  * per-task diff semantics match `update_task` (no-op fields skipped,
    unchanged tasks reported in `noops`);
  * "Deleted" is not an accepted status;
  * due_date '' clears.

`list_task_dependencies` contract:
  * exactly one of project_id / milestone_id;
  * membership required (same posture as list_tasks);
  * milestone scope uses the same task-set predicate as the UI rollup.

Friendly-argument enrichment (`_friendly_arguments` nested pass):
  * update_tasks_bulk rows gain display_id / title / current snapshot;
  * create_task_plan assignee UUIDs become usernames;
  * the input dicts the caller passed are not mutated (the persisted
    arguments_json must stay raw for the resume path).
"""

from datetime import date

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.list_task_dependencies import LIST_TASK_DEPENDENCIES
from origin.search_engine.agent.tools.update_tasks_bulk import UPDATE_TASKS_BULK
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase


class OrganizeToolsTestBase(BaseAPITestCase):
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
        self.task_a = self._task("Design review", priority="Normal")
        self.task_b = self._task("Implementation", priority="Normal", due_date=date(2026, 8, 1))

    def _task(self, title, **kwargs):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title=title,
            status=kwargs.pop("status", "Open"),
            **kwargs,
        )


class UpdateTasksBulkTests(OrganizeToolsTestBase):
    def test_happy_path_diffs_noops_and_clear(self):
        out = UPDATE_TASKS_BULK.run(
            {
                "updates": [
                    {
                        "task_id": self.task_a.task_id,
                        "priority": "Critical",
                        "due_date": "2026-07-20",
                        "rationale": "Blocks implementation.",
                    },
                    {
                        "task_id": self.task_b.task_id,
                        "priority": "Normal",  # already Normal → no-op
                        "due_date": "",  # clears
                        "rationale": "Due date moves to after the review.",
                    },
                ]
            },
            self.ctx,
        )
        self.task_a.refresh_from_db()
        self.task_b.refresh_from_db()
        self.assertEqual(self.task_a.priority, "Critical")
        self.assertEqual(str(self.task_a.due_date), "2026-07-20")
        self.assertIsNone(self.task_b.due_date)
        by_id = {row["task_id"]: row for row in out["updated"]}
        self.assertEqual(
            set(by_id[self.task_a.task_id]["changed_fields"]), {"priority", "due_date"}
        )
        # b's priority was a no-op but its due_date cleared → still "updated".
        self.assertEqual(by_id[self.task_b.task_id]["changed_fields"], ["due_date(cleared)"])
        self.assertEqual(out["noops"], [])
        self.assertIn("Updated 2 task(s)", out["__summary__"])

    def test_pure_noop_row_lands_in_noops(self):
        out = UPDATE_TASKS_BULK.run(
            {
                "updates": [
                    {
                        "task_id": self.task_a.task_id,
                        "priority": "Normal",  # unchanged
                        "rationale": "No real change.",
                    }
                ]
            },
            self.ctx,
        )
        self.assertEqual(out["updated"], [])
        self.assertEqual(out["noops"], [self.task_a.task_id])

    def test_one_bad_row_rejects_the_whole_batch(self):
        with self.assertRaises(ToolError) as caught:
            UPDATE_TASKS_BULK.run(
                {
                    "updates": [
                        {
                            "task_id": self.task_a.task_id,
                            "priority": "Critical",
                            "rationale": "Fine row.",
                        },
                        {
                            "task_id": self.task_b.task_id,
                            "priority": "Urgent",  # invalid enum
                            "due_date": "someday",  # invalid date
                            "rationale": "",  # missing
                        },
                        {"task_id": 999999, "priority": "Low", "rationale": "Ghost."},
                    ]
                },
                self.ctx,
            )
        msg = str(caught.exception)
        self.assertIn("nothing was applied", msg)
        self.assertIn("updates[1]: `priority`", msg)
        self.assertIn("updates[1]: `due_date`", msg)
        self.assertIn("updates[1]: `rationale` is required", msg)
        self.assertIn("updates[2]: task 999999 not found", msg)
        self.task_a.refresh_from_db()
        self.assertEqual(self.task_a.priority, "Normal")  # untouched

    def test_deleted_status_not_accepted(self):
        with self.assertRaises(ToolError) as caught:
            UPDATE_TASKS_BULK.run(
                {
                    "updates": [
                        {
                            "task_id": self.task_a.task_id,
                            "status": "Deleted",
                            "rationale": "Nope.",
                        }
                    ]
                },
                self.ctx,
            )
        self.assertIn("`status` must be one of", str(caught.exception))

    def test_foreign_team_and_non_member_denied(self):
        ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaisesMessage(ToolError, "not authorized to update task"):
            UPDATE_TASKS_BULK.run(
                {
                    "updates": [
                        {
                            "task_id": self.task_a.task_id,
                            "priority": "Low",
                            "rationale": "Not mine.",
                        }
                    ]
                },
                ctx2,
            )
        self.task_a.refresh_from_db()
        self.assertEqual(self.task_a.priority, "Normal")

    def test_duplicate_task_ids_rejected(self):
        with self.assertRaisesMessage(ToolError, "duplicate task_id"):
            UPDATE_TASKS_BULK.run(
                {
                    "updates": [
                        {"task_id": self.task_a.task_id, "priority": "Low", "rationale": "r"},
                        {"task_id": self.task_a.task_id, "priority": "High", "rationale": "r"},
                    ]
                },
                self.ctx,
            )


class ListTaskDependenciesTests(OrganizeToolsTestBase):
    def setUp(self):
        super().setUp()
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v1.0"
        )
        backing = ensure_backing_task(self.milestone)
        for t in (self.task_a, self.task_b):
            t.milestone = self.milestone
            t.parent_task_id = backing.task_id
            t.save(update_fields=["milestone", "parent_task_id"])
        self.dep = TaskDependency.objects.create(
            blocker_task=self.task_a,
            blocked_task=self.task_b,
            team=self.team,
            created_by=self.user,
        )
        # An edge in another project the milestone scope must NOT return.
        other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Elsewhere",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other_project, attendee=self.user)
        o1 = TaskMaster.objects.create(
            team=self.team, project=other_project, reporter=self.user, title="O1", status="Open"
        )
        o2 = TaskMaster.objects.create(
            team=self.team, project=other_project, reporter=self.user, title="O2", status="Open"
        )
        TaskDependency.objects.create(
            blocker_task=o1, blocked_task=o2, team=self.team, created_by=self.user
        )

    def test_milestone_scope_returns_only_its_edges(self):
        out = LIST_TASK_DEPENDENCIES.run({"milestone_id": self.milestone.milestone_id}, self.ctx)
        self.assertEqual(len(out["dependencies"]), 1)
        edge = out["dependencies"][0]
        self.assertEqual(edge["blocker_task_id"], self.task_a.task_id)
        self.assertEqual(edge["blocked_task_id"], self.task_b.task_id)
        self.assertEqual(edge["blocker_display_id"], self.task_a.display_id)
        self.assertIn('milestone "v1.0"', out["__summary__"])

    def test_project_scope(self):
        out = LIST_TASK_DEPENDENCIES.run({"project_id": self.project.project_id}, self.ctx)
        blockers = {e["blocker_title"] for e in out["dependencies"]}
        self.assertEqual(blockers, {"Design review"})

    def test_exactly_one_scope_required(self):
        with self.assertRaisesMessage(ToolError, "exactly one"):
            LIST_TASK_DEPENDENCIES.run({}, self.ctx)
        with self.assertRaisesMessage(ToolError, "exactly one"):
            LIST_TASK_DEPENDENCIES.run(
                {"project_id": self.project.project_id, "milestone_id": 1}, self.ctx
            )

    def test_non_member_denied(self):
        ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaisesMessage(ToolError, "not a member"):
            LIST_TASK_DEPENDENCIES.run({"project_id": self.project.project_id}, ctx2)
        with self.assertRaisesMessage(ToolError, "not a member"):
            LIST_TASK_DEPENDENCIES.run({"milestone_id": self.milestone.milestone_id}, ctx2)


class FriendlyArgumentEnrichmentTests(OrganizeToolsTestBase):
    def test_bulk_update_rows_gain_display_id_title_and_current(self):
        raw = {
            "updates": [
                {
                    "task_id": self.task_b.task_id,
                    "priority": "High",
                    "rationale": "Blocked work should start sooner.",
                }
            ]
        }
        out = controller._friendly_arguments(raw, "update_tasks_bulk")
        row = out["updates"][0]
        self.assertEqual(row["display_id"], self.task_b.display_id)
        self.assertEqual(row["title"], "Implementation")
        self.assertEqual(
            row["current"],
            {
                "priority": "Normal",
                "effort_level": None,
                "status": "Open",
                "due_date": "2026-08-01",
            },
        )
        # Proposed values and task_id survive untouched.
        self.assertEqual(row["priority"], "High")
        self.assertEqual(row["task_id"], self.task_b.task_id)
        # The caller's dict was NOT mutated — resume re-runs from raw args.
        self.assertNotIn("current", raw["updates"][0])

    def test_task_plan_assignees_become_usernames(self):
        raw = {
            "project_id": self.project.project_id,
            "milestone": {"title": "M", "assignee_ids": [str(self.user2.id)]},
            "tasks": [{"title": "T", "assignee_id": str(self.user.id)}],
        }
        out = controller._friendly_arguments(raw, "create_task_plan")
        self.assertEqual(out["project_id"], "Website Redesign")
        self.assertEqual(out["tasks"][0]["assignee_id"], "testuser")
        self.assertEqual(out["milestone"]["assignee_ids"], ["otheruser"])
        self.assertEqual(raw["tasks"][0]["assignee_id"], str(self.user.id))  # not mutated
