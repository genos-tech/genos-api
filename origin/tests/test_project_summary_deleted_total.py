"""MIP-2558: a task whose status is Deleted is not in a project summary's total (nor a bucket)."""

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.get_project_summary import GET_PROJECT_SUMMARY
from origin.tests.test_base import BaseAPITestCase


class ProjectSummaryDeletedTotalTests(BaseAPITestCase):
    def test_the_total_is_the_buckets_without_rejected(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Sum", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        for status in ("Open", "Closed", "Rejected", "Deleted", "Deleted"):
            TaskMaster.objects.create(
                team=self.team,
                project=project,
                title=status,
                reporter=self.user,
                status=status,
            )
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

        out = GET_PROJECT_SUMMARY.run({"project_id": project.project_id}, ctx)

        self.assertEqual(out["total_tasks"], 2)
        self.assertEqual(
            out["total_tasks"],
            sum(n for s, n in out["status_breakdown"].items() if s != "Rejected"),
        )
        self.assertNotIn("Deleted", out["status_breakdown"])
        self.assertIn("2 task(s)", out["__summary__"])
