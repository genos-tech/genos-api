"""Inviting an external collaborator to one project.

Guest invites reuse the existing `TeamInvite` token flow rather than the
inbox request/approve machinery, because a guest is external: no inbox to
receive a request in, no workspace to browse. Everything before the
branch — single-use, expiry, email lock — is shared, so a guest link is
exactly as hard to forward or replay as a team link.

What differs is what acceptance WRITES: a `ProjectMembers` row and no
`TeamMembers` row. `test_accepting_a_guest_invite_grants_no_team_membership`
is the one that matters — it is the whole model in one assertion.
"""

import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.common.invite_models import TeamInvite
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.member_roles import EDITOR, GUEST, VIEWER
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.scope_guards import is_guest, is_team_member

User = get_user_model()

INVITE_URL = "/api/v2/team/invite/"
PREVIEW_URL = "/api/v2/team/invite/preview/"
ACCEPT_URL = "/api/v2/team/invite/accept/"


class GuestInviteBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Client Redesign", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.other_project = ProjectMaster.objects.create(
            team=self.team, project_name="Internal Roadmap", owner=self.user
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user
        )
        self.contractor = User.objects.create_user(
            username="contractor", email="contractor@agency.example", password="pw"
        )

    def _invite(self, emails, project_id=None, as_user=None):
        self.authenticate(as_user or self.user)
        body = {"team_id": str(self.team.team_id), "emails": emails}
        if project_id is not None:
            body["project_id"] = project_id
        return self.client.post(INVITE_URL, body, format="json")

    def _mint_token(self, invite):
        """Invite tokens are stored hashed, so tests can't read the one
        the email carried — mint a fresh one onto the row instead."""
        raw = secrets.token_urlsafe(32)
        invite.token_hash = hashlib.sha256(raw.encode()).hexdigest()
        invite.save(update_fields=["token_hash"])
        return raw


class TestGuestInviteCreation(GuestInviteBase):
    def test_project_id_makes_it_a_guest_invite(self):
        res = self._invite(["contractor@agency.example"], project_id=self.project.project_id)
        self.assertEqual(res.status_code, 200)
        invite = TeamInvite.objects.get(invited_email="contractor@agency.example")
        self.assertEqual(invite.member_role, GUEST)
        self.assertEqual(invite.project_id, self.project.project_id)

    def test_without_project_id_it_stays_a_team_invite(self):
        res = self._invite(["newhire@example.com"])
        self.assertEqual(res.status_code, 200)
        invite = TeamInvite.objects.get(invited_email="newhire@example.com")
        self.assertEqual(invite.member_role, VIEWER)
        self.assertIsNone(invite.project_id)

    def test_a_project_from_another_team_is_refused(self):
        from origin.models.common.team_models import TeamMaster

        other_owner = User.objects.create_user(
            username="ginvother", email="ginvother@example.com", password="pw"
        )
        other_team = TeamMaster.objects.create(
            team_name="Other Invite Team", team_email="ginvother@example.com", owner=other_owner
        )
        foreign = ProjectMaster.objects.create(
            team=other_team, project_name="Foreign", owner=other_owner
        )
        res = self._invite(["x@example.com"], project_id=foreign.project_id)
        self.assertEqual(res.status_code, 404)

    def test_a_non_member_of_the_project_cannot_invite_a_guest(self):
        outsider = User.objects.create_user(
            username="ginvout", email="ginvout@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=outsider)
        res = self._invite(["x@example.com"], project_id=self.project.project_id, as_user=outsider)
        self.assertEqual(res.status_code, 404)

    def test_a_project_viewer_cannot_invite_a_guest(self):
        """Bringing an outsider in is management."""
        viewer = User.objects.create_user(
            username="ginvviewer", email="ginvviewer@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=viewer)
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=viewer, member_role=VIEWER
        )
        res = self._invite(["x@example.com"], project_id=self.project.project_id, as_user=viewer)
        self.assertEqual(res.status_code, 403)

    def test_a_project_editor_can_invite_a_guest(self):
        editor = User.objects.create_user(
            username="ginveditor", email="ginveditor@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=editor)
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=editor, member_role=EDITOR
        )
        res = self._invite(["x@example.com"], project_id=self.project.project_id, as_user=editor)
        self.assertEqual(res.status_code, 200)

    def test_a_guest_cannot_invite_further_guests(self):
        """GUEST is not in MANAGER_ROLES, so this falls out of the model."""
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.contractor, member_role=GUEST
        )
        res = self._invite(
            ["another@agency.example"],
            project_id=self.project.project_id,
            as_user=self.contractor,
        )
        self.assertEqual(res.status_code, 403)

    def test_an_existing_team_member_may_still_be_invited_as_a_guest(self):
        """They're not in the project, so this is a legitimate ask and
        must not be short-circuited as `already_member`."""
        res = self._invite([self.user2.email], project_id=self.project.project_id)
        self.assertEqual(res.data["results"][0]["status"], "sent")

    def test_someone_already_in_the_project_is_already_member(self):
        res = self._invite([self.user.email], project_id=self.project.project_id)
        self.assertEqual(res.data["results"][0]["status"], "already_member")

    def test_team_and_guest_invites_to_one_address_are_separate_rows(self):
        self._invite(["dual@example.com"])
        self._invite(["dual@example.com"], project_id=self.project.project_id)
        rows = TeamInvite.objects.filter(invited_email="dual@example.com")
        self.assertEqual(rows.count(), 2)
        self.assertEqual({r.member_role for r in rows}, {VIEWER, GUEST})


class TestGuestInviteAcceptance(GuestInviteBase):
    def _pending_guest_invite(self, project=None):
        self._invite(["contractor@agency.example"], project_id=(project or self.project).project_id)
        return TeamInvite.objects.get(invited_email="contractor@agency.example")

    def test_accepting_a_guest_invite_grants_no_team_membership(self):
        """The whole model, in one assertion."""
        invite = self._pending_guest_invite()
        token = self._mint_token(invite)

        self.authenticate(self.contractor)
        res = self.client.post(ACCEPT_URL, {"token": token}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["is_guest"])
        self.assertEqual(res.data["project_id"], self.project.project_id)

        self.assertFalse(
            TeamMembers.objects.filter(team=self.team, attendee=self.contractor).exists()
        )
        self.assertFalse(is_team_member(self.team.team_id, self.contractor.id))
        self.assertTrue(is_guest(self.team.team_id, self.contractor.id))

    def test_the_guest_lands_in_exactly_one_project(self):
        invite = self._pending_guest_invite()
        token = self._mint_token(invite)
        self.authenticate(self.contractor)
        self.client.post(ACCEPT_URL, {"token": token}, format="json")

        rows = ProjectMembers.objects.filter(attendee=self.contractor)
        self.assertEqual([r.project_id for r in rows], [self.project.project_id])
        self.assertEqual(rows.first().member_role, GUEST)

    def test_a_forwarded_guest_link_cannot_be_redeemed_by_someone_else(self):
        invite = self._pending_guest_invite()
        token = self._mint_token(invite)
        self.authenticate(self.user2)
        res = self.client.post(ACCEPT_URL, {"token": token}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["detail"], "email_mismatch")

    def test_an_expired_guest_invite_is_refused(self):
        invite = self._pending_guest_invite()
        invite.expires_at = timezone.now() - timedelta(minutes=1)
        invite.save(update_fields=["expires_at"])
        token = self._mint_token(invite)
        self.authenticate(self.contractor)
        res = self.client.post(ACCEPT_URL, {"token": token}, format="json")
        self.assertEqual(res.data["detail"], "expired")

    def test_a_guest_invite_is_single_use(self):
        invite = self._pending_guest_invite()
        token = self._mint_token(invite)
        self.authenticate(self.contractor)
        self.client.post(ACCEPT_URL, {"token": token}, format="json")
        second = self.client.post(ACCEPT_URL, {"token": token}, format="json")
        self.assertEqual(second.status_code, 400)

    def test_a_deleted_project_refuses_rather_than_granting_the_team(self):
        """The project FK is SET_NULL. Falling through to the team-join
        branch here would grant vastly more than intended."""
        invite = self._pending_guest_invite()
        token = self._mint_token(invite)
        self.project.is_deleted = True
        self.project.save(update_fields=["is_deleted"])

        self.authenticate(self.contractor)
        res = self.client.post(ACCEPT_URL, {"token": token}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["detail"], "project_unavailable")
        self.assertFalse(
            TeamMembers.objects.filter(team=self.team, attendee=self.contractor).exists()
        )

    def test_a_normal_invite_still_grants_team_membership(self):
        self._invite(["newhire@example.com"])
        invite = TeamInvite.objects.get(invited_email="newhire@example.com")
        token = self._mint_token(invite)
        newhire = User.objects.create_user(
            username="newhire", email="newhire@example.com", password="pw"
        )
        self.authenticate(newhire)
        res = self.client.post(ACCEPT_URL, {"token": token}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["is_guest"])
        self.assertTrue(is_team_member(self.team.team_id, newhire.id))


class TestGuestInvitePreview(GuestInviteBase):
    def test_preview_names_the_project_not_the_team(self):
        """This endpoint is unauthenticated — anyone holding the token
        reads it — so a guest's token must not disclose the workspace
        they're being kept out of."""
        self._invite(["contractor@agency.example"], project_id=self.project.project_id)
        invite = TeamInvite.objects.get(invited_email="contractor@agency.example")
        token = self._mint_token(invite)

        self.unauthenticate()
        res = self.client.get(PREVIEW_URL, {"token": token})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["valid"])
        self.assertTrue(res.data["is_guest"])
        self.assertEqual(res.data["team_name"], self.project.project_name)
        self.assertNotEqual(res.data["team_name"], self.team.team_name)

    def test_preview_of_a_team_invite_still_names_the_team(self):
        self._invite(["newhire@example.com"])
        invite = TeamInvite.objects.get(invited_email="newhire@example.com")
        token = self._mint_token(invite)
        self.unauthenticate()
        res = self.client.get(PREVIEW_URL, {"token": token})
        self.assertFalse(res.data["is_guest"])
        self.assertEqual(res.data["team_name"], self.team.team_name)

    def test_preview_hides_a_guest_invite_whose_project_is_gone(self):
        self._invite(["contractor@agency.example"], project_id=self.project.project_id)
        invite = TeamInvite.objects.get(invited_email="contractor@agency.example")
        token = self._mint_token(invite)
        self.project.is_deleted = True
        self.project.save(update_fields=["is_deleted"])

        self.unauthenticate()
        res = self.client.get(PREVIEW_URL, {"token": token})
        self.assertFalse(res.data["valid"])
