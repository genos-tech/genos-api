"""Membership guards — `origin/views/utils/scope_guards.py`.

The invariant these protect is the one the role resolvers cannot express:
`resolve_team_role` / `resolve_project_role` return VIEWER for a total
non-member, so "resolve the role and compare" admits strangers. Every
test here therefore pairs an allowed caller with a stranger and asserts
the stranger is refused.

Two behaviours are load-bearing and easy to regress:

  * the OWNER may hold no membership row at all, and must still pass;
  * refusal is 404, never 403 — a 403 confirms the id names something
    real, which is itself a disclosure.
"""

from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import TestCase

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.views.utils.scope_guards import (
    is_project_member,
    is_team_member,
    member_project_ids,
    require_project_member,
    require_project_member_or_response,
    require_team_member,
    require_team_member_or_response,
)

User = get_user_model()


class ScopeGuardBase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="sgowner", email="sgowner@test.com", password="pw"
        )
        self.member = User.objects.create_user(
            username="sgmember", email="sgmember@test.com", password="pw"
        )
        self.stranger = User.objects.create_user(
            username="sgstranger", email="sgstranger@test.com", password="pw"
        )
        self.team = TeamMaster.objects.create(
            team_name="Scope Guard Team", team_email="sg@test.com", owner=self.owner
        )
        # Deliberately NO TeamMembers row for the owner: team creation
        # does not always write one, and the guard must still admit them.
        TeamMembers.objects.create(team=self.team, attendee=self.member)

        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Scope Guard Project", owner=self.owner
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.member)

        # A second tenant, to prove cross-tenant refusal rather than
        # merely "unknown id refused".
        self.other_owner = User.objects.create_user(
            username="sgother", email="sgother@test.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Other Team", team_email="sgother@test.com", owner=self.other_owner
        )
        self.other_project = ProjectMaster.objects.create(
            team=self.other_team, project_name="Other Project", owner=self.other_owner
        )


class TestTeamMembership(ScopeGuardBase):
    def test_owner_without_membership_row_is_a_member(self):
        """The regression the two `_verify_team_member` copies both have."""
        self.assertFalse(TeamMembers.objects.filter(team=self.team, attendee=self.owner).exists())
        self.assertTrue(is_team_member(self.team.team_id, self.owner.id))
        self.assertEqual(require_team_member(self.owner, self.team.team_id), self.team)

    def test_active_member_passes(self):
        self.assertTrue(is_team_member(self.team.team_id, self.member.id))

    def test_stranger_is_refused(self):
        self.assertFalse(is_team_member(self.team.team_id, self.stranger.id))
        with self.assertRaises(Http404):
            require_team_member(self.stranger, self.team.team_id)

    def test_soft_deleted_membership_is_refused(self):
        row = TeamMembers.objects.get(team=self.team, attendee=self.member)
        row.is_deleted = True
        row.save(update_fields=["is_deleted"])
        self.assertFalse(is_team_member(self.team.team_id, self.member.id))

    def test_member_of_another_team_is_refused(self):
        self.assertFalse(is_team_member(self.team.team_id, self.other_owner.id))

    def test_deleted_team_is_refused_even_for_its_owner(self):
        self.team.is_deleted = True
        self.team.save(update_fields=["is_deleted"])
        with self.assertRaises(Http404):
            require_team_member(self.owner, self.team.team_id)

    def test_none_ids_are_refused_not_crashing(self):
        self.assertFalse(is_team_member(None, self.member.id))
        self.assertFalse(is_team_member(self.team.team_id, None))
        with self.assertRaises(Http404):
            require_team_member(self.member, None)

    def test_response_variant_returns_404_never_403(self):
        self.assertIsNone(require_team_member_or_response(self.member, self.team.team_id))
        res = require_team_member_or_response(self.stranger, self.team.team_id)
        self.assertIsNotNone(res)
        self.assertEqual(res.status_code, 404)

    def test_refusal_does_not_confirm_the_team_exists(self):
        """A real-but-forbidden team and a nonexistent one look identical."""
        real = require_team_member_or_response(self.stranger, self.team.team_id)
        fake = require_team_member_or_response(
            self.stranger, "00000000-0000-0000-0000-000000000000"
        )
        self.assertEqual(real.status_code, fake.status_code)
        self.assertEqual(real.data, fake.data)


class TestProjectMembership(ScopeGuardBase):
    def test_owner_without_membership_row_is_a_member(self):
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.owner).exists()
        )
        self.assertTrue(is_project_member(self.project.project_id, self.owner.id))

    def test_member_passes_and_stranger_is_refused(self):
        self.assertTrue(is_project_member(self.project.project_id, self.member.id))
        self.assertFalse(is_project_member(self.project.project_id, self.stranger.id))
        with self.assertRaises(Http404):
            require_project_member(self.stranger, self.project.project_id)

    def test_team_membership_alone_does_not_grant_the_project(self):
        """The guest model depends on this: being in the team is not
        being in the project."""
        outsider = User.objects.create_user(
            username="sgteamonly", email="sgteamonly@test.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=outsider)
        self.assertTrue(is_team_member(self.team.team_id, outsider.id))
        self.assertFalse(is_project_member(self.project.project_id, outsider.id))

    def test_cross_tenant_project_is_refused(self):
        self.assertFalse(is_project_member(self.other_project.project_id, self.member.id))
        with self.assertRaises(Http404):
            require_project_member(self.member, self.other_project.project_id)

    def test_response_variant_returns_404(self):
        self.assertIsNone(require_project_member_or_response(self.member, self.project.project_id))
        res = require_project_member_or_response(self.stranger, self.project.project_id)
        self.assertEqual(res.status_code, 404)


class TestMemberProjectIds(ScopeGuardBase):
    def test_returns_joined_and_owned_projects(self):
        ids = member_project_ids(self.member.id)
        self.assertIn(self.project.project_id, ids)
        self.assertNotIn(self.other_project.project_id, ids)

        owner_ids = member_project_ids(self.owner.id)
        self.assertIn(self.project.project_id, owner_ids)

    def test_narrows_to_one_team(self):
        second_project = ProjectMaster.objects.create(
            team=self.other_team, project_name="Second", owner=self.member
        )
        ProjectMembers.objects.create(
            team=self.other_team, project=second_project, attendee=self.member
        )
        all_ids = member_project_ids(self.member.id)
        self.assertIn(second_project.project_id, all_ids)

        scoped = member_project_ids(self.member.id, team_id=self.team.team_id)
        self.assertIn(self.project.project_id, scoped)
        self.assertNotIn(second_project.project_id, scoped)

    def test_stranger_gets_nothing(self):
        self.assertEqual(member_project_ids(self.stranger.id), [])
        self.assertEqual(member_project_ids(None), [])
