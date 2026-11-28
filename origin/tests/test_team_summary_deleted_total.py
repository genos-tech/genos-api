"""MIP-2581: a task whose status is Deleted is in no total of a team task summary."""

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.get_team_task_summary import GET_TEAM_TASK_SUMMARY
from origin.tests.test_base import BaseAPITestCase


class TeamSummaryDeletedTotalTests(BaseAPITestCase):
    def test_the_totals_leave_deleted_out_and_add_up(self):
        for name, statuses in (
            ("One", ("Open", "Closed", "Rejected", "Deleted", "Deleted")),
            ("Two", ("WIP", "Deleted")),
        ):
            project = ProjectMaster.objects.create(
                team=self.team, project_name=name, owner=self.user, project_system_user=self.user
            )
            ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
            for status in statuses:
                TaskMaster.objects.create(
                    team=self.team,
                    project=project,
                    title=status,
                    reporter=self.user,
                    status=status,
                )
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

        out = GET_TEAM_TASK_SUMMARY.run({}, ctx)

        self.assertEqual(out["total_tasks"], 3)
        self.assertEqual(
            {p["project_name"]: p["total"] for p in out["per_project"]}, {"One": 2, "Two": 1}
        )
        # The row stays, counted apart.
        self.assertEqual(out["status_breakdown"]["Deleted"], 3)
        self.assertIn("3 task(s)", out["__summary__"])
