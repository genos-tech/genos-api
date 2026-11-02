"""Project names are unique per team, not across every company (MIP-942).

`ProjectMaster.project_name` used to be `unique=True`, a constraint over the
whole database, while the views only looked for a duplicate inside the
caller's own team. The visible damage:

  * the starter workspace uses fixed project names, so only the FIRST team
    that ticked the (default) option ever got one; the second hit the
    constraint inside the seeding transaction, which rolled back the
    projects, the guide notes and the to-dos, and the view logged it and
    answered 201;
  * renaming a project to a name another company used was a 500;
  * creating one was a 400 that told the caller the name exists elsewhere.
"""

from unittest import mock

from django.db import IntegrityError, transaction
from rest_framework import status

from origin.models.chat.todo_models import ToDoItem
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.starter_seeder import JAPANESE_STARTER_BLUEPRINT, STARTER_BLUEPRINT
from origin.tests.test_base import BaseAPITestCase


class ProjectNamePerTeamTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.other_team = TeamMaster.objects.create(
            team_name="Other Team", team_email="other-team@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)

    def _create(self, team, name):
        return self.client.post(
            "/api/v2/project/",
            {
                "team": str(team.team_id),
                "project_name": name,
                "owner": str(self.user.id),
                "project_system_user": str(self.user.id),
            },
            format="json",
        )

    def _project(self, team, name):
        project = ProjectMaster.objects.create(
            team=team, project_name=name, owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.get_or_create(team=team, project=project, attendee=self.user)
        return project

    def test_another_teams_name_can_be_used_on_create(self):
        self._project(self.team, "Website")

        res = self._create(self.other_team, "Website")

        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(ProjectMaster.objects.filter(project_name="Website").count(), 2)

    def test_another_teams_name_can_be_used_on_rename(self):
        self._project(self.team, "Website")
        mine = self._project(self.other_team, "Landing")

        res = self.client.put(
            "/api/v2/project/",
            {"project_id": mine.project_id, "project_name": "Website"},
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK, getattr(res, "data", None))
        mine.refresh_from_db()
        self.assertEqual(mine.project_name, "Website")

    def test_a_name_taken_in_the_same_team_is_still_refused_on_create(self):
        self._project(self.team, "Website")

        res = self._create(self.team, "Website")

        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("hint", res.data)
        self.assertEqual(ProjectMaster.objects.filter(project_name="Website").count(), 1)

    def test_a_name_taken_in_the_same_team_is_still_refused_on_rename(self):
        self._project(self.team, "Website")
        mine = self._project(self.team, "Landing")

        res = self.client.put(
            "/api/v2/project/",
            {"project_id": mine.project_id, "project_name": "Website"},
            format="json",
        )

        self.assertEqual(res.status_code, status.HTTP_409_CONFLICT)

    def test_the_database_itself_keeps_a_team_to_one_project_per_name(self):
        """The views check first, but two requests can pass that check
        together; the constraint is what stops the second."""
        self._project(self.team, "Website")

        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectMaster.objects.create(
                team=self.team,
                project_name="Website",
                owner=self.user,
                project_system_user=self.user,
            )

    def test_every_team_that_asks_gets_the_starter_workspace(self):
        """The second team used to get nothing: 0 projects, no guide notes, no to-dos."""
        self.authenticate()
        seeded = []
        for name in ("Acme", "Beta", "Gamma"):
            with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
                res = self.client.post(
                    "/api/v2/team/create/",
                    {
                        "team_name": name,
                        "team_email": f"{name.lower()}@example.com",
                        "owner_id": str(self.user.id),
                        "with_starter": True,
                    },
                    format="json",
                )
            self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
            seeded.append(TeamMaster.objects.get(team_id=res.data["teamDetails"]["teamId"]))

        for team in seeded:
            names = set(
                ProjectMaster.objects.filter(team=team).values_list("project_name", flat=True)
            )
            self.assertEqual(
                names,
                {STARTER_BLUEPRINT["name"], JAPANESE_STARTER_BLUEPRINT["name"]},
                f"{team.team_name} got {names}",
            )
            self.assertTrue(
                ToDoItem.objects.filter(group__team=team).exists(),
                f"{team.team_name} got no starter to-dos",
            )
