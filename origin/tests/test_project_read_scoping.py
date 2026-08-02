"""Project reads, writes, and the inbox-approval holes.

Six `high` findings. The two that matter most are not reads at all:

  **`ProjectMasterView.post`** took `team` from the request body and
  never checked it, so a project could be planted in another tenant —
  where it then appears in their project list and gets a PM channel.

  **The two `join/fromInbox` endpoints** fetched the inbox item by id
  alone, so anyone could approve anyone's join request into any team or
  project by counting. That is the same shape as the `/team/join/` hole
  closed in #252, one step removed — approving somebody else's request
  rather than making one.

The fix for both inbox endpoints is to scope the lookup to
`receiver=request.user`. The item is addressed TO its approver, so that
single filter is both the authorization check and the fetch, and a 404
leaks nothing about which item ids exist.
"""

from django.contrib.auth import get_user_model

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

PROJECT = "/api/v2/project/"
PROJECTS = "/api/v2/project/projects/"
TAGS = "/api/v2/project/tag/"
JOIN_PRJ_INBOX = "/api/v2/project/join/fromInbox/"
JOIN_TEAM_INBOX = "/api/v2/team/join/fromInbox/"


class ProjectScopingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # The project-list serializer dereferences the system user that
        # the real create flow always signs up.
        sysuser = User.objects.create_user(
            username="prssys", email="prssys@example.com", password="pw"
        )
        sysuser.is_system_user = True
        sysuser.save(update_fields=["is_system_user"])
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Ours",
            owner=self.user,
            project_system_user=sysuser,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

        self.outsider = User.objects.create_user(
            username="prsout", email="prsout@example.com", password="pw"
        )
        self.outsider_team = TeamMaster.objects.create(
            team_name="PRS Outsider", team_email="prsout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)

    # ── creation ──────────────────────────────────────────────────────

    def test_outsider_cannot_plant_a_project_in_a_foreign_team(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            PROJECT,
            {
                "team": str(self.team.team_id),
                "project_name": "planted",
                "owner": str(self.outsider.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(ProjectMaster.objects.filter(project_name="planted").exists())

    def test_a_member_can_still_create(self):
        self.authenticate(self.user)
        res = self.client.post(
            PROJECT,
            {"team": str(self.team.team_id), "project_name": "legit", "owner": str(self.user.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    # ── reads ─────────────────────────────────────────────────────────

    def test_project_detail_refuses_a_non_member(self):
        """Returns the full member roster, emails included."""
        self.authenticate(self.outsider)
        res = self.client.get(
            PROJECT,
            {"team_id": str(self.team.team_id), "project_id": self.project.project_id},
        )
        self.assertEqual(res.status_code, 404)

    def test_project_list_refuses_a_foreign_team(self):
        self.authenticate(self.outsider)
        res = self.client.get(PROJECTS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 404)

    def test_project_list_cannot_name_another_user(self):
        """`attendee_id` drove both `isJoined` and the CACHE KEY."""
        self.authenticate(self.user2)
        res = self.client.get(
            PROJECTS,
            {"team_id": str(self.team.team_id), "attendee_id": str(self.user.id)},
        )
        self.assertEqual(res.status_code, 403)

    def test_project_list_still_shows_projects_you_have_not_joined(self):
        """Deliberately not narrowed to your own projects: the list is
        how someone finds a project to ask to join."""
        self.authenticate(self.user2)
        res = self.client.get(PROJECTS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        names = {p["projectName"] for p in res.data}
        self.assertIn("Ours", names)

    # ── tags ──────────────────────────────────────────────────────────

    def test_outsider_cannot_add_a_tag(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            TAGS,
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "tag_name": "planted",
                "tag_color": "#fff",
                "tag_text_color": "#000",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_outsider_cannot_delete_a_tag(self):
        """Tags drive task filtering, so deleting them reshapes a board."""
        ProjectTags.objects.create(
            team=self.team,
            project=self.project,
            tag_id=1,
            tag_name="keep",
            tag_color="#fff",
            tag_text_color="#000",
        )
        self.authenticate(self.outsider)
        res = self.client.delete(
            TAGS,
            {"project_id": self.project.project_id, "tag_name": "keep"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertTrue(ProjectTags.objects.filter(tag_name="keep").exists())


class InboxApprovalTests(BaseAPITestCase):
    """Approving a join request is a membership mutation."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Gated", owner=self.user
        )
        self.applicant = User.objects.create_user(
            username="prsapplicant", email="prsapplicant@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.applicant)
        self.attacker = User.objects.create_user(
            username="prsattacker", email="prsattacker@example.com", password="pw"
        )

        # A request addressed to self.user, the project owner.
        self.item = InboxItems.objects.create(
            team=self.team,
            sender=self.applicant,
            receiver=self.user,
            item_type=2,
            item_body="wants in",
            item_optionals={
                "project_id": self.project.project_id,
                "project_name": "Gated",
            },
        )

    def test_only_the_receiver_can_approve_a_project_request(self):
        self.authenticate(self.attacker)
        res = self.client.post(
            JOIN_PRJ_INBOX,
            {"team_id": str(self.team.team_id), "item_id": self.item.item_id},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.applicant).exists()
        )

    def test_the_receiver_can_still_approve(self):
        self.authenticate(self.user)
        res = self.client.post(
            JOIN_PRJ_INBOX,
            {"team_id": str(self.team.team_id), "item_id": self.item.item_id},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.applicant).exists()
        )

    def test_only_the_receiver_can_approve_a_team_request(self):
        newcomer = User.objects.create_user(
            username="prsnewcomer", email="prsnewcomer@example.com", password="pw"
        )
        item = InboxItems.objects.create(
            team=self.team,
            sender=newcomer,
            receiver=self.user,
            item_type=1,
            item_body="wants in",
        )
        self.authenticate(self.attacker)
        res = self.client.post(
            JOIN_TEAM_INBOX,
            {
                "team_id": str(self.team.team_id),
                "team_name": self.team.team_name,
                "item_id": item.item_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(TeamMembers.objects.filter(team=self.team, attendee=newcomer).exists())
