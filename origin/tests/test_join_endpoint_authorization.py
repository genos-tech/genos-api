"""Authorization on the two membership-mutation endpoints.

`POST /api/v2/team/join/` and `POST /api/v2/project/join/` both used to
run with no authorization whatsoever — they read the ids out of the
request body and wrote the membership row. Any authenticated user could
add anyone, including themselves, to any team or project whose id they
held. Project ids are sequential integers, so those needed no guessing
at all.

Every `test_*_is_refused` here FAILS on the pre-fix code, which is the
point: they are the regression tests for the hole, not documentation of
the fix.

The awkward part of closing this is that four legitimate callers share
`/team/join/`, and one of them is not a self-join — see
`TeamMembersView._authorize`. Each has a test below so a future tightening
cannot silently break team creation, team switching, or project creation.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.member_roles import EDITOR
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

TEAM_JOIN = "/api/v2/team/join/"
PROJECT_JOIN = "/api/v2/project/join/"


class TeamJoinAuthorizationTests(BaseAPITestCase):
    """`self.user` owns `self.team`; `self.user2` is a member of it."""

    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="pw"
        )
        self.victim = User.objects.create_user(
            username="victim", email="victim@example.com", password="pw"
        )

    # ── the hole ──────────────────────────────────────────────────────

    def test_stranger_self_join_is_refused(self):
        """The core defect: join any team you know the id of."""
        self.authenticate(self.outsider)
        res = self.client.post(
            TEAM_JOIN,
            {"team_id": str(self.team.team_id), "attendee_id": str(self.outsider.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(
            TeamMembers.objects.filter(team=self.team, attendee=self.outsider).exists()
        )

    def test_stranger_cannot_add_a_third_party(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            TEAM_JOIN,
            {"team_id": str(self.team.team_id), "attendee_id": str(self.victim.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(TeamMembers.objects.filter(team=self.team, attendee=self.victim).exists())

    def test_ordinary_member_cannot_add_a_person(self):
        """Adding people is member management: owner/editor only, matching
        the gate on InviteTeamMembersView."""
        self.authenticate(self.user2)  # a viewer
        res = self.client.post(
            TEAM_JOIN,
            {"team_id": str(self.team.team_id), "attendee_id": str(self.victim.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    # ── the four legitimate callers ───────────────────────────────────

    def test_owner_without_membership_row_may_self_join(self):
        """Caller 1 — fresh team creation. TeamMasterView.post does not
        write a membership row, so the creator arrives here with none."""
        fresh_owner = User.objects.create_user(
            username="freshowner", email="freshowner@example.com", password="pw"
        )
        fresh_team = TeamMaster.objects.create(
            team_name="Freshly Made", team_email="fresh@example.com", owner=fresh_owner
        )
        self.assertFalse(TeamMembers.objects.filter(team=fresh_team).exists())

        self.authenticate(fresh_owner)
        res = self.client.post(
            TEAM_JOIN,
            {"team_id": str(fresh_team.team_id), "attendee_id": str(fresh_owner.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertTrue(TeamMembers.objects.filter(team=fresh_team, attendee=fresh_owner).exists())

    def test_existing_member_self_join_is_a_no_op(self):
        """Caller 2a — the client calls this on every team switch."""
        self.authenticate(self.user2)
        res = self.client.post(
            TEAM_JOIN,
            {"team_id": str(self.team.team_id), "attendee_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(TeamMembers.objects.filter(team=self.team, attendee=self.user2).count(), 1)

    def test_departed_member_may_rejoin(self):
        """Caller 2b — LeaveTeamView soft-deletes; rejoin un-deletes."""
        row = TeamMembers.objects.get(team=self.team, attendee=self.user2)
        row.is_deleted = True
        row.save(update_fields=["is_deleted"])

        self.authenticate(self.user2)
        res = self.client.post(
            TEAM_JOIN,
            {"team_id": str(self.team.team_id), "attendee_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        row.refresh_from_db()
        self.assertFalse(row.is_deleted)

    def test_any_member_may_attach_a_system_user(self):
        """Caller 3 — project creation joins its `is_system_user` account
        to the team, and the creator need not be an owner or editor."""
        system_user = User.objects.create_user(
            username="prjsystem", email="prjsystem@example.com", password="pw"
        )
        system_user.is_system_user = True
        system_user.save(update_fields=["is_system_user"])

        self.authenticate(self.user2)  # a plain viewer
        res = self.client.post(
            TEAM_JOIN,
            {"team_id": str(self.team.team_id), "attendee_id": str(system_user.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    def test_editor_may_add_a_person(self):
        """Caller 4 — deliberate member management."""
        row = TeamMembers.objects.get(team=self.team, attendee=self.user2)
        row.member_role = EDITOR
        row.save(update_fields=["member_role"])

        self.authenticate(self.user2)
        res = self.client.post(
            TEAM_JOIN,
            {"team_id": str(self.team.team_id), "attendee_id": str(self.victim.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertTrue(TeamMembers.objects.filter(team=self.team, attendee=self.victim).exists())

    def test_owner_may_add_a_person(self):
        self.authenticate(self.user)
        res = self.client.post(
            TEAM_JOIN,
            {"team_id": str(self.team.team_id), "attendee_id": str(self.victim.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    def test_missing_params_are_a_400_not_a_500(self):
        self.authenticate(self.user)
        res = self.client.post(TEAM_JOIN, {"team_id": str(self.team.team_id)}, format="json")
        self.assertEqual(res.status_code, 400)


class ProjectJoinAuthorizationTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Join Auth Project", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        # In the team, but NOT in the project.
        self.team_only = User.objects.create_user(
            username="teamonly", email="teamonly@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.team_only)
        # In neither.
        self.outsider = User.objects.create_user(
            username="prjoutsider", email="prjoutsider@example.com", password="pw"
        )

    def _payload(self, attendee):
        return {
            "team_id": str(self.team.team_id),
            "project_id": self.project.project_id,
            "attendee_id": str(attendee.id),
        }

    # ── the hole ──────────────────────────────────────────────────────

    def test_outsider_self_join_is_refused(self):
        """Project PKs are sequential ints — this needed no guesswork."""
        self.authenticate(self.outsider)
        res = self.client.post(PROJECT_JOIN, self._payload(self.outsider), format="json")
        self.assertEqual(res.status_code, 404)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.outsider).exists()
        )

    def test_team_member_cannot_add_themselves_to_a_private_project(self):
        """Private projects keep the request → owner-approval flow. Only
        the PUBLIC case below is self-service."""
        private = ProjectMaster.objects.create(
            team=self.team, project_name="Private Join Project", owner=self.user, is_private=True
        )
        self.authenticate(self.team_only)
        res = self.client.post(
            PROJECT_JOIN,
            {
                "team_id": str(self.team.team_id),
                "project_id": private.project_id,
                "attendee_id": str(self.team_only.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(
            ProjectMembers.objects.filter(project=private, attendee=self.team_only).exists()
        )

    def test_team_member_cannot_add_a_third_party_to_a_public_project(self):
        """The public exemption is self-join only — it must not become a
        way for a non-member to staff someone else's project."""
        self.assertFalse(self.project.is_private)
        self.authenticate(self.team_only)
        res = self.client.post(PROJECT_JOIN, self._payload(self.user2), format="json")
        self.assertEqual(res.status_code, 404)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.user2).exists()
        )

    def test_outsider_cannot_be_added_by_a_project_member(self):
        """A project must not be usable to pull in someone outside the team."""
        self.authenticate(self.user)
        res = self.client.post(PROJECT_JOIN, self._payload(self.outsider), format="json")
        self.assertEqual(res.status_code, 403)

    def test_project_from_another_team_is_refused(self):
        other_owner = User.objects.create_user(
            username="othertm", email="othertm@example.com", password="pw"
        )
        other_team = TeamMaster.objects.create(
            team_name="Other Join Team", team_email="otherjoin@example.com", owner=other_owner
        )
        other_project = ProjectMaster.objects.create(
            team=other_team, project_name="Other Join Project", owner=other_owner
        )
        self.authenticate(self.user)
        res = self.client.post(
            PROJECT_JOIN,
            {
                "team_id": str(self.team.team_id),
                "project_id": other_project.project_id,
                "attendee_id": str(self.user.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    # ── legitimate callers ────────────────────────────────────────────

    def test_project_member_may_add_a_teammate(self):
        """Deliberately ANY project member, not just owner/editor — that
        is the documented project policy."""
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.authenticate(self.user2)  # a plain project member
        res = self.client.post(PROJECT_JOIN, self._payload(self.team_only), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.team_only).exists()
        )

    def test_team_member_may_self_join_a_public_project(self):
        """What the "Join" button in `ModalJoinProject` does. Gating this
        on prior project membership made the button always 404."""
        self.assertFalse(self.project.is_private)
        self.authenticate(self.team_only)
        res = self.client.post(PROJECT_JOIN, self._payload(self.team_only), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.team_only).exists()
        )

    def test_project_owner_without_a_member_row_may_self_join(self):
        """Project creation: owner is set on the project before the
        membership row is written."""
        fresh = ProjectMaster.objects.create(
            team=self.team, project_name="Fresh Project", owner=self.user2
        )
        self.assertFalse(ProjectMembers.objects.filter(project=fresh).exists())
        self.authenticate(self.user2)
        res = self.client.post(
            PROJECT_JOIN,
            {
                "team_id": str(self.team.team_id),
                "project_id": fresh.project_id,
                "attendee_id": str(self.user2.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    def test_system_user_may_be_attached_without_team_membership(self):
        system_user = User.objects.create_user(
            username="prjsys2", email="prjsys2@example.com", password="pw"
        )
        system_user.is_system_user = True
        system_user.save(update_fields=["is_system_user"])
        self.assertFalse(TeamMembers.objects.filter(team=self.team, attendee=system_user).exists())

        self.authenticate(self.user)
        res = self.client.post(PROJECT_JOIN, self._payload(system_user), format="json")
        self.assertEqual(res.status_code, 201)

    def test_missing_params_are_a_400_not_a_500(self):
        self.authenticate(self.user)
        res = self.client.post(PROJECT_JOIN, {"team_id": str(self.team.team_id)}, format="json")
        self.assertEqual(res.status_code, 400)
