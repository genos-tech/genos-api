"""`list_milestones` can filter on every status a milestone can hold (MIP-868).

A milestone reaches `Blocked` (it is mirrored by a backing task and the
dependency rule flips that), and `list_milestones` returned them, yet
`statuses: ["Blocked"]` was refused by both the validator and the schema enum.
"""

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.list_milestones import LIST_MILESTONES

from .test_base import BaseAPITestCase

_STATUSES = ["Open", "WIP", "Blocked", "Pending", "Closed", "Deleted"]


class ListMilestonesStatusFilterTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Genos",
            code="GEN",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        for status in _STATUSES:
            MilestoneMaster.objects.create(
                team=self.team,
                project=self.project,
                reporter=self.user,
                title=f"m-{status}",
                status=status,
            )

    def _titles(self, **args):
        out = LIST_MILESTONES.run({"project_id": self.project.project_id, **args}, self.ctx)
        return {m["title"] for m in out["milestones"]}

    def test_blocked_is_a_valid_filter(self):
        self.assertEqual(self._titles(statuses=["Blocked"]), {"m-Blocked"})

    def test_blocked_combines_with_other_statuses(self):
        self.assertEqual(self._titles(statuses=["Blocked", "Pending"]), {"m-Blocked", "m-Pending"})

    def test_every_status_a_milestone_can_hold_is_accepted(self):
        for status in _STATUSES:
            with self.subTest(status=status):
                self.assertEqual(self._titles(statuses=[status]), {f"m-{status}"})

    def test_the_published_schema_enum_matches_the_validator(self):
        enum = LIST_MILESTONES.parameters_schema["properties"]["statuses"]["items"]["enum"]
        self.assertEqual(sorted(enum), sorted(_STATUSES))

    def test_an_unknown_status_is_still_refused(self):
        with self.assertRaises(ToolError):
            self._titles(statuses=["Stuck"])
