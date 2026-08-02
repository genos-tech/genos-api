"""The last of the `medium` tier: team-wide catalogs anyone could read.

Three rows, and they do not all get the same treatment — which is the
point of re-reading each handler instead of applying one rule.

  **`ProjectTagsView.get`** and **`ProjectLabelsView.get`** are plain
  omissions. Both take a client-supplied id and filter on nothing else.
  `ProjectTagsView.post`, on the same class, already calls
  `require_project_member_or_response`; only the read forgot.
  `ProjectLabelsView`'s docstring even claimed "open to any authenticated
  team member" while the handler enforced only the first half of that.

  **`CheckTeamExistsView.get`** is NOT an omission. It backs the
  join-by-id flow, where the caller is not a member yet and that is the
  entire purpose. Requiring membership would break joining a team. The
  fix is to narrow the payload instead: a non-member gets what the join
  card renders, and `teamOwnerId` — which names a specific person and is
  read by nobody in that state — is withheld.

A tag or label vocabulary is not dramatic on its own. Read across a
whole install it is a fair sketch of what every team is working on, and
project ids are sequential integers.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import (
    ProjectLabel,
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

TAGS = "/api/v2/project/tag/"
LABELS = "/api/v2/project/label/"
TEAM_EXISTS = "/api/v2/team/exist/"


class TeamCatalogScopingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Ours", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectTags.objects.create(
            team=self.team,
            project=self.project,
            tag_id=1,
            tag_name="roadmap-q4",
            tag_color="#fff",
            tag_text_color="#000",
        )
        ProjectLabel.objects.create(team=self.team, name="Confidential", color="#f00")

        self.outsider = User.objects.create_user(
            username="tcsout", email="tcsout@example.com", password="pw"
        )
        self.outsider_team = TeamMaster.objects.create(
            team_name="TCS Outsider", team_email="tcsout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)

    # ── project tags ──────────────────────────────────────────────────

    def test_tags_refuse_a_project_i_am_not_in(self):
        self.authenticate(self.outsider)
        res = self.client.get(
            TAGS,
            {"team_id": str(self.team.team_id), "project_id": self.project.project_id},
        )
        self.assertEqual(res.status_code, 404)

    def test_a_teammate_outside_the_project_is_also_refused(self):
        """Scoped to the PROJECT, matching the sibling `post` — being in
        the team is not being in the project."""
        self.authenticate(self.user2)
        res = self.client.get(
            TAGS,
            {"team_id": str(self.team.team_id), "project_id": self.project.project_id},
        )
        self.assertEqual(res.status_code, 404)

    def test_a_project_member_can_still_read_tags(self):
        self.authenticate(self.user)
        res = self.client.get(
            TAGS,
            {"team_id": str(self.team.team_id), "project_id": self.project.project_id},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual([t["tagName"] for t in res.data], ["roadmap-q4"])

    # ── project labels ────────────────────────────────────────────────

    def test_labels_refuse_a_foreign_team(self):
        self.authenticate(self.outsider)
        res = self.client.get(LABELS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 404)

    def test_a_team_member_can_still_read_labels(self):
        """Team-scoped, not project-scoped: the chips render for everyone
        in the team, which is what the docstring always claimed."""
        self.authenticate(self.user2)
        res = self.client.get(LABELS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual([label["name"] for label in res.data], ["Confidential"])

    # ── team existence probe ──────────────────────────────────────────

    def test_a_non_member_can_still_look_up_a_team(self):
        """Load-bearing: this is the join-by-id flow. Denying it would
        make it impossible to join a team you were given the id for."""
        self.authenticate(self.outsider)
        res = self.client.get(TEAM_EXISTS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["exist"])
        self.assertEqual(res.data["teamDetails"]["teamName"], self.team.team_name)

    def test_a_non_member_does_not_get_the_owner_id(self):
        self.authenticate(self.outsider)
        res = self.client.get(TEAM_EXISTS, {"team_id": str(self.team.team_id)})
        self.assertNotIn("teamOwnerId", res.data["teamDetails"])

    def test_a_member_still_gets_the_full_object(self):
        """`initCurrentTeam` feeds this straight into `setCurrentTeam`, so
        the member payload must not shrink."""
        self.authenticate(self.user)
        res = self.client.get(TEAM_EXISTS, {"team_id": str(self.team.team_id)})
        details = res.data["teamDetails"]
        self.assertEqual(str(details["teamOwnerId"]), str(self.user.id))
        for key in ("teamId", "teamName", "teamEmail", "teamImgPath"):
            self.assertIn(key, details)

    def test_an_unknown_team_still_reports_absence(self):
        self.authenticate(self.outsider)
        res = self.client.get(TEAM_EXISTS, {"team_id": "8b1f3c2e-0000-4000-8000-000000000000"})
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["exist"])
