"""The guest-collaborator model: defined by the row it does NOT have.

A guest holds `ProjectMembers` rows and **no `TeamMembers` row**. That
absence is the entire security model, so this suite asserts the denials
it produces rather than the role string it stores.

The alternative — `member_role="guest"` on a `TeamMembers` row — would
have inherited every one of these as a GRANT, because each gate below
keys on team membership and has no idea guests exist. `TestGuestIsDeniedByDefault`
is therefore the argument for the model, written as tests.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.member_roles import (
    ASSIGNABLE_PROJECT_ROLES,
    ASSIGNABLE_ROLES,
    GUEST,
    MANAGER_ROLES,
    can_manage,
    is_assignable,
    is_assignable_project_role,
    is_guest_role,
)
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_folder_role import is_team_member as note_is_team_member
from origin.views.utils.scope_guards import (
    guest_project_ids,
    is_guest,
    is_project_member,
    is_team_member,
)

User = get_user_model()


class GuestModelBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Client Work", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

        # The other project in the same team — a guest must not see it.
        self.other_project = ProjectMaster.objects.create(
            team=self.team, project_name="Internal Work", owner=self.user
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user
        )

        # The guest: one ProjectMembers row, NO TeamMembers row.
        self.guest = User.objects.create_user(
            username="guest", email="guest@agency.example", password="pw"
        )
        ProjectMembers.objects.create(
            team=self.team,
            project=self.project,
            attendee=self.guest,
            member_role=GUEST,
        )


class TestRoleVocabulary(BaseAPITestCase):
    def test_guest_is_not_assignable_on_a_team(self):
        """There is no team row to put it on."""
        self.assertNotIn(GUEST, ASSIGNABLE_ROLES)
        self.assertFalse(is_assignable(GUEST))

    def test_guest_is_assignable_on_a_project(self):
        self.assertIn(GUEST, ASSIGNABLE_PROJECT_ROLES)
        self.assertTrue(is_assignable_project_role(GUEST))

    def test_guest_can_never_manage(self):
        """An external collaborator who could invite people would defeat
        the point of scoping them."""
        self.assertNotIn(GUEST, MANAGER_ROLES)
        self.assertFalse(can_manage(GUEST))

    def test_is_guest_role(self):
        self.assertTrue(is_guest_role(GUEST))
        for other in ("owner", "editor", "viewer", None, ""):
            self.assertFalse(is_guest_role(other))


class TestGuestIsDeniedByDefault(GuestModelBase):
    """Each assertion is a team-wide gate the guest fails WITHOUT that
    gate knowing guests exist."""

    def test_no_team_membership_row(self):
        self.assertFalse(TeamMembers.objects.filter(team=self.team, attendee=self.guest).exists())

    def test_team_membership_predicates_say_no(self):
        self.assertFalse(is_team_member(self.team.team_id, self.guest.id))
        # The note-ACL copy must agree, or public Team Notes folders —
        # which grant EDITOR to any team member — would open up.
        self.assertFalse(note_is_team_member(self.team.team_id, self.guest.id))

    def test_effective_tier_is_not_inherited_from_the_team(self):
        """get_effective_tier walks TeamMembers, so a guest never picks
        up the team's paid plan by being invited to one project."""
        from origin.search_engine.quota import get_effective_tier, invalidate_effective_tier

        self.team.plan = "max"
        self.team.save(update_fields=["plan"])
        invalidate_effective_tier(str(self.guest.id))
        invalidate_effective_tier(str(self.user2.id))

        self.assertEqual(get_effective_tier(str(self.guest.id)), "free")
        # ...while a real member of the same team does inherit it.
        self.assertEqual(get_effective_tier(str(self.user2.id)), "max")

    def test_guest_sees_only_the_invited_project(self):
        self.assertTrue(is_project_member(self.project.project_id, self.guest.id))
        self.assertFalse(is_project_member(self.other_project.project_id, self.guest.id))

    def test_a_full_member_is_not_a_guest(self):
        self.assertFalse(is_guest(self.team.team_id, self.user2.id))
        self.assertFalse(is_guest(self.team.team_id, self.user.id))

    def test_a_stranger_is_not_a_guest(self):
        stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.assertFalse(is_guest(self.team.team_id, stranger.id))

    def test_the_guest_is_a_guest(self):
        self.assertTrue(is_guest(self.team.team_id, self.guest.id))

    def test_guest_project_ids_lists_only_invited_projects(self):
        ids = guest_project_ids(self.team.team_id, self.guest.id)
        self.assertEqual(ids, [self.project.project_id])

    def test_guest_project_ids_is_empty_for_a_full_member(self):
        """Deliberately: this helper answers "what may a GUEST see", and
        a member is not one. Callers must not use it as a general scope."""
        self.assertEqual(guest_project_ids(self.team.team_id, self.user.id), [])


class TestGuestRegainsAccessCorrectly(GuestModelBase):
    def test_promoting_a_guest_to_a_member_stops_them_being_a_guest(self):
        TeamMembers.objects.create(team=self.team, attendee=self.guest)
        self.assertFalse(is_guest(self.team.team_id, self.guest.id))
        self.assertTrue(is_team_member(self.team.team_id, self.guest.id))

    def test_removing_the_last_project_stops_them_being_a_guest(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.guest).delete()
        self.assertFalse(is_guest(self.team.team_id, self.guest.id))

    def test_a_guest_in_two_teams_is_scoped_per_team(self):
        other_team = TeamMaster.objects.create(
            team_name="Second Client", team_email="second@example.com", owner=self.user2
        )
        self.assertTrue(is_guest(self.team.team_id, self.guest.id))
        self.assertFalse(is_guest(other_team.team_id, self.guest.id))
