"""MIP-2557: `assign_task` on a milestone's own task row writes the milestone's assignees.

It used to write only the row, whose assignee `sync_backing_task` takes from
`MilestoneAssignees`, so the next milestone edit put the old person back.
"""

from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.assign_task import ASSIGN_TASK
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.tests.test_milestone_assignment_notice import MilestoneNoticeBase


class AssignTaskMilestoneTests(MilestoneNoticeBase):
    def assign(self, user, **extra):
        args = {"task_id": self.milestone.task_id, **extra}
        if user is not None:
            args["assignee_id"] = str(user.id)
        return ASSIGN_TASK.run(args, self.ctx)

    def people(self):
        return {a.user_id for a in MilestoneAssignees.objects.filter(milestone=self.milestone)}

    def row(self):
        return TaskMaster.objects.get(task_id=self.milestone.task_id)

    def edit_milestone(self):
        MANAGE_MILESTONE.run(
            {"action": "update", "milestone_id": self.milestone.milestone_id, "title": "Again"},
            self.ctx,
        )

    def test_it_replaces_the_milestones_assignees_and_a_later_edit_keeps_it(self):
        out = self.assign(self.user3)

        self.assertEqual(self.people(), {self.user3.id})
        self.assertEqual(self.row().assignee_id, self.user3.id)
        self.assertEqual(out["assignee_id"], str(self.user3.id))
        self.assertEqual(out["milestone_id"], self.milestone.milestone_id)
        self.assertEqual(out["removed_assignee_ids"], [str(self.user2.id)])
        self.assertIn("took 1 other assignee(s) off", out["__summary__"])
        self.edit_milestone()
        self.assertEqual(self.row().assignee_id, self.user3.id)

    def test_the_person_added_is_told(self):
        self.assign(self.user3)
        self.assertEqual(self._told(self.user3).count(), 1)

    def test_assigning_who_is_already_the_only_one_changes_nothing(self):
        out = self.assign(self.user2)
        self.assertNotIn("removed_assignee_ids", out)
        self.assertEqual(self.people(), {self.user2.id})
        self.assertFalse(self._told(self.user2).exists())

    def test_unassign_clears_the_milestone(self):
        out = self.assign(None)
        self.assertEqual(self.people(), set())
        self.assertIsNone(out["assignee_id"])
        self.assertEqual(out["removed_assignee_ids"], [str(self.user2.id)])
        self.edit_milestone()
        self.assertEqual(self.people(), set())

    def test_a_person_outside_the_project_and_team_is_refused(self):
        stranger = CustomUser.objects.create_user(
            email="out@example.com", username="outsider", password="x"
        )
        with self.assertRaises(ToolError):
            self.assign(stranger)
        self.assertEqual(self.people(), {self.user2.id})

    def test_holding_the_row_without_being_on_the_project_is_not_enough(self):
        holder = CustomUser.objects.create_user(
            email="hold@example.com", username="holder", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=holder)
        TaskMaster.objects.filter(task_id=self.milestone.task_id).update(assignee=holder)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(holder.id))
        with self.assertRaises(ToolForbidden):
            self.assign(self.user3)
        self.assertEqual(self.people(), {self.user2.id})

    def test_a_deleted_milestone_row_is_not_written(self):
        MilestoneMaster.objects.filter(pk=self.milestone.pk).update(is_deleted=True)
        TaskMaster.objects.filter(task_id=self.milestone.task_id).update(is_deleted=True)
        with self.assertRaises(ToolError):
            self.assign(self.user3)
