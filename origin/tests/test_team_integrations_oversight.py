"""A team's owner and editors see the connected AI apps in it, and can end them (MIP-1371).

`GET/DELETE team/mcp-connections/` mirror the team's API keys (`test_api_keys`): the list is
open to the owner and editors, each row says whether the caller may end it, and ending follows
`can_remove_member` (the owner anyone's, an editor a viewer's or guest's, anyone their own).
Ending is for THIS team only: the person's other teams keep working, and an app left with no
team ends altogether. Removing a member from the team prunes their connections at once.

Also here: the pending-invite list and revoke.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from origin.models.common.invite_models import TeamInvite
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMembers
from origin.services.member_roles import EDITOR
from origin.services.team_membership import InviteAcceptError, accept_invite
from origin.tests.test_mcp_connections import ConnectionsBase

LIST = "/api/v2/team/mcp-connections/"


class TeamConnectionsTests(ConnectionsBase):
    def setUp(self):
        super().setUp()
        # self.user owns self.team and has a Claude connection (from the base); user2 is a member.
        self.editor = self.user2
        TeamMembers.objects.filter(team=self.team, attendee=self.editor).update(member_role=EDITOR)
        self.viewer = self._more("viewer")
        self.editor2 = self._more("editor2", EDITOR)
        self.viewer_conn = self.connect(self.viewer, self.app)
        self.editor_conn = self.connect(self.editor, self.app)
        self.editor2_conn = self.connect(self.editor2, self.app)

    def _more(self, name, role="viewer"):
        user = type(self.user).objects.create_user(
            username=f"tc{name}", email=f"tc{name}@example.com", password="pw12345678"
        )
        TeamMembers.objects.create(team=self.team, attendee=user, member_role=role)
        return user

    def list(self, team=None):
        return self.client.get(LIST, {"team_id": str((team or self.team).team_id)})

    def end(self, conn, team=None):
        return self.client.delete(f"{LIST}{conn.id}/?team_id={(team or self.team).team_id}")

    def test_the_owner_sees_every_connection_and_may_end_any_of_them(self):
        self.authenticate(self.user)
        res = self.list()
        self.assertEqual(res.status_code, 200)
        rows = {r["userName"]: r for r in res.json()["connections"]}
        self.assertEqual(
            set(rows),
            {self.user.username, self.viewer.username, self.editor.username, self.editor2.username},
        )
        self.assertTrue(all(r["revocable"] for r in rows.values()))
        row = rows[self.viewer.username]
        self.assertEqual(row["client"]["name"], "Claude")
        self.assertEqual(row["scope"], "write")
        self.assertEqual(row["userEmail"], self.viewer.email)

    def test_an_editor_sees_all_but_may_end_only_a_viewers_and_their_own(self):
        self.authenticate(self.editor)
        rows = {r["userName"]: r for r in self.list().json()["connections"]}
        self.assertEqual(
            {n: r["revocable"] for n, r in rows.items()},
            {
                self.user.username: False,
                self.viewer.username: True,
                self.editor.username: True,
                self.editor2.username: False,
            },
        )
        self.assertEqual(self.end(self.editor2_conn).status_code, 403)
        self.assertEqual(self.end(self.conn).status_code, 403)  # the owner's
        self.assertEqual(self.end(self.viewer_conn).status_code, 200)

    def test_a_viewer_and_a_stranger_get_a_404_and_end_nothing(self):
        self.authenticate(self.viewer)
        self.assertEqual(self.list().status_code, 404)
        self.assertEqual(self.end(self.editor_conn).status_code, 404)
        outsider = type(self.user).objects.create_user(
            username="tcout", email="tcout@example.com", password="pw12345678"
        )
        self.authenticate(outsider)
        self.assertEqual(self.list().status_code, 404)
        self.assertEqual(self.end(self.viewer_conn).status_code, 404)
        self.viewer_conn.refresh_from_db()
        self.assertIsNone(self.viewer_conn.revoked_at)

    def test_ending_removes_only_this_team_and_keeps_the_others(self):
        other = self.team_of(self.viewer)
        self.viewer_conn.team_ids = [str(self.team.team_id), str(other.team_id)]
        self.viewer_conn.default_team = self.team
        self.viewer_conn.save()
        self.authenticate(self.user)
        res = self.end(self.viewer_conn)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["ended"])
        self.viewer_conn.refresh_from_db()
        self.assertIsNone(self.viewer_conn.revoked_at)
        self.assertEqual(self.viewer_conn.team_ids, [str(other.team_id)])
        self.assertEqual(str(self.viewer_conn.default_team_id), str(other.team_id))

    def test_an_app_left_with_no_team_ends_with_its_tokens(self):
        self.tokens(self.viewer, self.app, "v")
        self.authenticate(self.user)
        res = self.end(self.viewer_conn)
        self.assertTrue(res.json()["ended"])
        self.viewer_conn.refresh_from_db()
        self.assertIsNotNone(self.viewer_conn.revoked_at)
        self.assertEqual(self.list().json()["connections"].__len__(), 3)

    def test_the_list_leaves_out_other_teams_connections_and_revoked_ones(self):
        elsewhere_team = self.team_of(self.viewer)
        other_conn = self.connect(
            self.viewer,
            self.other_app,
            default_team=elsewhere_team,
            team_ids=[str(elsewhere_team.team_id)],
        )
        self.editor2_conn.revoke()
        self.authenticate(self.user)
        ids = {r["id"] for r in self.list().json()["connections"]}
        self.assertNotIn(str(other_conn.id), ids)
        self.assertNotIn(str(self.editor2_conn.id), ids)
        # A connection of another team cannot be ended through this team either.
        self.assertEqual(self.end(other_conn).status_code, 404)

    def test_a_connection_of_someone_who_left_the_team_is_not_listed(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.viewer).update(is_deleted=True)
        self.authenticate(self.user)
        names = {r["userName"] for r in self.list().json()["connections"]}
        self.assertNotIn(self.viewer.username, names)

    def test_ending_leaves_a_trace_naming_the_actor(self):
        other = self.team_of(self.viewer)
        self.viewer_conn.team_ids = [str(self.team.team_id), str(other.team_id)]
        self.viewer_conn.save()
        self.authenticate(self.user)
        self.end(self.viewer_conn)
        event = SecurityEvent.objects.get(action=SecurityAction.MCP_UPDATED)
        self.assertEqual(event.metadata["reason"], "removed_by_team")
        self.assertEqual(str(event.actor_id), str(self.user.id))

    def test_removing_a_member_from_the_team_prunes_their_connection_at_once(self):
        other = self.team_of(self.viewer)
        self.viewer_conn.team_ids = [str(self.team.team_id), str(other.team_id)]
        self.viewer_conn.save()
        self.authenticate(self.user)
        res = self.client.post(
            "/api/v2/team/member/remove/",
            {"team_id": str(self.team.team_id), "user_id": str(self.viewer.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.viewer_conn.refresh_from_db()
        self.assertEqual(self.viewer_conn.team_ids, [str(other.team_id)])
        self.assertIsNone(self.viewer_conn.revoked_at)

    def test_unauthenticated_is_refused(self):
        self.client.credentials()
        self.assertIn(self.list().status_code, (401, 403))


class InviteTests(ConnectionsBase):
    def setUp(self):
        super().setUp()
        self.owner, self.member = self.user, self.user2
        self.invite = TeamInvite.objects.create(
            team=self.team,
            invited_email="newhire@example.com",
            invited_by=self.owner,
            token_hash="a" * 64,
            expires_at=timezone.now() + timedelta(days=3),
        )

    def invites(self):
        return self.client.get("/api/v2/team/invites/", {"team_id": str(self.team.team_id)})

    def revoke(self, invite=None):
        return self.client.post(
            "/api/v2/team/invite/revoke/",
            {"team_id": str(self.team.team_id), "invite_id": str((invite or self.invite).id)},
            format="json",
        )

    def test_the_owner_lists_pending_invites_with_the_address(self):
        self.authenticate(self.owner)
        rows = self.invites().json()["invites"]
        self.assertEqual([r["email"] for r in rows], ["newhire@example.com"])
        self.assertEqual(rows[0]["invitedBy"], self.owner.username)
        self.assertEqual(rows[0]["memberRole"], "viewer")

    def test_accepted_revoked_and_expired_are_not_listed(self):
        for status, delta in (("accepted", 3), ("revoked", 3), ("pending", -1)):
            TeamInvite.objects.create(
                team=self.team,
                invited_email=f"{status}{delta}@example.com",
                invited_by=self.owner,
                token_hash=f"{status[0]}{delta}".ljust(64, "0"),
                status=status,
                expires_at=timezone.now() + timedelta(days=delta),
            )
        self.authenticate(self.owner)
        self.assertEqual(len(self.invites().json()["invites"]), 1)

    def test_a_viewer_cannot_list_and_gets_a_404(self):
        self.authenticate(self.member)
        self.assertEqual(self.invites().status_code, 404)

    def test_revoking_stops_the_link_working_and_leaves_a_trace(self):
        self.authenticate(self.owner)
        res = self.revoke()
        self.assertEqual(res.status_code, 200)
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.status, "revoked")
        with self.assertRaises(InviteAcceptError):
            accept_invite(self.invite, self.member)
        event = SecurityEvent.objects.get(action=SecurityAction.INVITE_REVOKED)
        self.assertEqual(str(event.actor_id), str(self.owner.id))
        self.assertEqual(self.invites().json()["invites"], [])

    def test_revoking_twice_is_a_409_and_records_nothing_new(self):
        self.authenticate(self.owner)
        self.revoke()
        self.assertEqual(self.revoke().status_code, 409)
        self.assertEqual(
            SecurityEvent.objects.filter(action=SecurityAction.INVITE_REVOKED).count(), 1
        )

    def test_a_member_cannot_revoke_but_the_sender_can(self):
        self.authenticate(self.member)
        self.assertEqual(self.revoke().status_code, 404)
        self.invite.refresh_from_db()
        self.assertEqual(self.invite.status, "pending")
        self.invite.invited_by = self.member
        self.invite.save()
        self.assertEqual(self.revoke().status_code, 200)

    def test_an_invite_of_another_team_is_a_404(self):
        other = self.team_of(self.owner)
        foreign = TeamInvite.objects.create(
            team=other,
            invited_email="x@example.com",
            invited_by=self.owner,
            token_hash="b" * 64,
            expires_at=timezone.now() + timedelta(days=3),
        )
        self.authenticate(self.owner)
        self.assertEqual(self.revoke(foreign).status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, "pending")

    def test_missing_ids_are_a_400(self):
        self.authenticate(self.owner)
        res = self.client.post(
            "/api/v2/team/invite/revoke/", {"team_id": str(self.team.team_id)}, format="json"
        )
        self.assertEqual(res.status_code, 400)
