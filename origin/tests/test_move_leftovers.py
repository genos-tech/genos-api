"""MIP-1597: the leftovers of the milestone convert / project move work (MIP-1583, api #881 + #882).

One class per item:

1. the single-task PUT move answers `notesNarrowed` as the milestone PATCH does;
2. a reporter outside the destination does not stop a move and is counted
   (`reportersOutsideDestination`); the reporter keeps reading the task;
3. the single-task PUT move refuses an assignee who is not on the destination, as the milestone
   PATCH does (`assignee_not_on_destination`);
4. the move counts (`tasks_moved` and the approval cards) are the live tasks only;
5. a convert makes no "changed in your AI app" notice (decided, pinned).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.mcp_notice import RULES, build_notice
from origin.tests.test_agent_parity_milestones import ToolBase
from origin.tests.test_task_personal_note_roles_revoked import TaskNoteBase
from origin.views.utils.scope_guards import can_access_task

User = get_user_model()
MILESTONE_URL = "/api/v2/milestone/"


# --------------------------------------------------------------------------------------------
# item 4: the counts are the live tasks
# --------------------------------------------------------------------------------------------


class MoveCountsLiveTasksTests(ToolBase):
    """A task deleted in the app has the status "Deleted" and the row stays: a person does not see
    it, so the count of "tasks that go with it" does not include it (as `subTasksKept`)."""

    def test_the_tool_does_not_count_a_task_whose_status_is_deleted(self):
        m = self.roadmap_milestone()
        TaskMaster.objects.filter(title="Second").update(status="Deleted")  # not is_deleted
        out = self.move_tool(m)
        self.assertEqual(out["moved"]["tasks_moved"], 2)
        self.assertIn("with 2 task(s)", out["__summary__"])
        # it still moved: the count is what a person sees, not what is stored
        self.assertEqual(TaskMaster.objects.get(title="Second").project_id, self.dest.project_id)

    def test_the_tool_counts_every_task_when_none_is_deleted(self):
        m = self.roadmap_milestone()
        self.assertEqual(self.move_tool(m)["moved"]["tasks_moved"], 3)

    def card(self, args):
        from origin.search_engine.agent import controller  # noqa: PLC0415

        return controller._friendly_arguments(args, "manage_milestone", ctx=self.ctx())

    def test_the_move_card_says_the_same(self):
        m = self.roadmap_milestone()
        TaskMaster.objects.filter(title="Second").update(status="Deleted")
        out = self.card(
            {"action": "update", "milestone_id": m.milestone_id, "project_id": self.dest.project_id}
        )
        self.assertEqual(out["current"]["move"]["tasks"], 2)

    def test_the_convert_card_says_the_same(self):
        m = self.roadmap_milestone()
        TaskMaster.objects.filter(title="Second").update(status="Deleted")
        out = self.card({"action": "convert", "milestone_id": m.milestone_id})
        self.assertEqual(out["current"]["tasks"], 2)


