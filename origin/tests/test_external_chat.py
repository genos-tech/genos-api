"""External (cross-team) group chats.

An external chat is an ordinary GM channel owned by ONE host team, with
`is_external=True` and one `ExternalGrant` per guest team. Membership is
still `ChannelMember` rows, so every existing message, read-cursor,
upload and socket path keeps working untouched — which is exactly why the
tests worth writing here are about the boundary, not the chat.

Three properties this suite exists to protect:

1. **The channel is not the connection.** Being in a connected team, or
   even in a team that holds a grant on some *other* object, admits
   nobody. Only a grant naming THIS channel does.
2. **Privacy cannot be dropped.** A public external GM would be
   self-joinable by every member of the host team, which no grant
   approved and the guest side could not observe.
3. **Each side runs its own roster.** The guest team's managers add and
   remove their own people with no host involvement; the host can veto an
   individual but cannot staff the guest's side, and the guest cannot
   staff the host's.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import ExternalGrant, ShareStatus
from origin.services.external_grants import (
    add_external_participants,
    offer_grant,
    respond_to_grant,
    revoke_grant,
)
from origin.services.member_roles import EDITOR, VIEWER
from origin.tests.cross_team_fixtures import CrossTeamTestCase

CHANNELS = "/api/v3/channels/"
MY_TEAMS = "/api/v2/team/getMyTeams/"


class ExternalChatTestCase(CrossTeamTestCase):
    """Channel-endpoint helpers. `create_external_chat` and `shared_chat`
    live on the shared fixture — the search suite needs them too."""

    def members_url(self, channel):
        return f"{CHANNELS}{channel.id}/members/"

    def member_url(self, channel, user):
        return f"{CHANNELS}{channel.id}/members/{user.id}/"


class ExternalChatCreateTests(ExternalChatTestCase):
    def test_creating_with_a_guest_team_offers_a_pending_grant(self):
        self.connect_a_and_b()
        res = self.create_external_chat([self.team_b.team_id])
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["channel"]["isExternal"])
        grant = ExternalGrant.objects.get(object_id=res.data["channel"]["id"])
        # Pending, not active: the guest team still has to accept once.
        self.assertEqual(grant.status, ShareStatus.PENDING)
        self.assertEqual(str(grant.guest_team_id), str(self.team_b.team_id))

    def test_external_chat_is_forced_private(self):
        self.connect_a_and_b()
        res = self.create_external_chat([self.team_b.team_id], is_private=False)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["channel"]["isPrivate"])

    def test_an_external_chat_cannot_be_made_public_afterwards(self):
        channel, _grant = self.shared_chat()
        self.authenticate(self.a_owner)
        res = self.client.patch(
            f"{CHANNELS}{channel.id}/",
            {"is_private": False},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        channel.refresh_from_db()
        self.assertTrue(channel.is_private)

    def test_cannot_offer_to_an_unconnected_team(self):
        res = self.create_external_chat([self.team_c.team_id])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error"], "not_connected")
        # Rolled back with the offer: a channel that names a team it never
        # offered access to would look shared and be nothing of the kind.
        self.assertFalse(Channel.objects.filter(is_external=True).exists())

    def test_a_non_manager_cannot_offer_on_creation(self):
        self.connect_a_and_b()
        self.authenticate(self.a_viewer)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team_a.team_id),
                "title": "Sneaky",
                "is_external": True,
                "guest_team_ids": [str(self.team_b.team_id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(Channel.objects.filter(is_external=True).exists())

    def test_a_plain_member_may_create_an_external_chat_with_no_grants(self):
        """Sharing is gated; creating an empty room is not.

        An external chat with no grants reaches nobody outside the host
        team, so refusing this would only push the same user into
        create-then-ask with no security difference.
        """
        self.authenticate(self.a_viewer)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team_a.team_id),
                "title": "Empty external",
                "is_external": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

    def test_only_a_gm_can_be_external(self):
        self.authenticate(self.a_owner)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.MDM,
                "team_id": str(self.team_a.team_id),
                "title": "",
                "is_external": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_host_members_can_still_be_added_at_creation(self):
        self.connect_a_and_b()
        self.authenticate(self.a_owner)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team_a.team_id),
                "title": "With host folk",
                "is_external": True,
                "guest_team_ids": [str(self.team_b.team_id)],
                "member_user_ids": [str(self.a_editor.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(
            ChannelMember.objects.filter(
                channel_id=res.data["channel"]["id"], user=self.a_editor, is_deleted=False
            ).exists()
        )

    def test_guest_team_people_cannot_be_seeded_at_creation(self):
        """The host does not get to staff the guest's side, even at t=0.

        At creation there is no accepted grant yet, so there is nothing
        for the host to have been delegated. Admitting B's people here
        would be the host deciding B's roster.
        """
        self.connect_a_and_b()
        self.authenticate(self.a_owner)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team_a.team_id),
                "title": "Presumptuous",
                "is_external": True,
                "guest_team_ids": [str(self.team_b.team_id)],
                "member_user_ids": [str(self.b_editor.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(ChannelMember.objects.filter(user=self.b_editor).exists())


class ExternalChatMembershipTests(ExternalChatTestCase):
    def test_guest_manager_admits_their_own_member_long_after_approval(self):
        """The repeatable half of the design, with no host action at all."""
        channel, grant = self.shared_chat()
        self.authenticate(self.b_owner)
        res = self.client.post(
            self.members_url(channel),
            {"user_ids": [str(self.b_viewer.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(
            ChannelMember.objects.filter(
                channel=channel, user=self.b_viewer, is_deleted=False
            ).exists()
        )

    def test_guest_manager_cannot_admit_a_third_teams_person(self):
        """The bound that stops a grant being re-shared onwards."""
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_owner.id], self.b_owner)
        self.authenticate(self.b_owner)
        res = self.client.post(
            self.members_url(channel),
            {"user_ids": [str(self.c_owner.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(ChannelMember.objects.filter(user=self.c_owner).exists())

    def test_a_guest_team_viewer_cannot_admit_anyone(self):
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.b_viewer)
        res = self.client.post(
            self.members_url(channel),
            {"user_ids": [str(self.b_editor.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(ChannelMember.objects.filter(user=self.b_editor).exists())

    def test_a_guest_manager_cannot_add_host_team_people(self):
        """Roster authority is per side, and this is the other side."""
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_owner.id], self.b_owner)
        self.authenticate(self.b_owner)
        res = self.client.post(
            self.members_url(channel),
            {"user_ids": [str(self.a_editor.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(
            ChannelMember.objects.filter(
                channel=channel, user=self.a_editor, is_deleted=False
            ).exists()
        )

    def test_the_role_ceiling_clamps_the_admitted_role(self):
        channel, grant = self.shared_chat(role_ceiling=VIEWER)
        add_external_participants(grant, [self.b_editor.id], self.b_owner)
        row = ChannelMember.objects.get(channel=channel, user=self.b_editor)
        # `viewer` maps onto the channel table's older vocabulary.
        self.assertEqual(row.role, "member")

    def test_a_pending_grant_admits_nobody(self):
        self.connect_a_and_b()
        res = self.create_external_chat([self.team_b.team_id])
        channel = Channel.objects.get(id=res.data["channel"]["id"])
        self.authenticate(self.b_owner)
        res = self.client.post(
            self.members_url(channel),
            {"user_ids": [str(self.b_editor.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_a_grant_on_another_object_does_not_admit_to_this_channel(self):
        """The property this feature would most plausibly get wrong.

        Team B holds an accepted grant on team A's PROJECT, so B's people
        are external participants of team A and `is_team_participant` says
        yes to them. Channel membership still has to be grant-bound to
        this channel, or one share becomes access to every group chat the
        host owns.
        """
        self.active_project_grant()
        self.authenticate(self.a_owner)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team_a.team_id),
                "title": "Host-only room",
                "is_external": True,
            },
            format="json",
        )
        channel = Channel.objects.get(id=res.data["channel"]["id"])
        self.authenticate(self.b_owner)
        res = self.client.post(
            self.members_url(channel),
            {"user_ids": [str(self.b_editor.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(ChannelMember.objects.filter(channel=channel, user=self.b_editor).exists())

    def test_a_stranger_team_is_never_admissible(self):
        channel, grant = self.shared_chat()
        self.authenticate(self.a_owner)
        res = self.client.post(
            self.members_url(channel),
            {"user_ids": [str(self.c_owner.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_internal_channels_are_unaffected(self):
        """The widened gate must not widen anything for internal chats."""
        self.connect_a_and_b()
        self.authenticate(self.a_owner)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team_a.team_id),
                "title": "Internal",
                "member_user_ids": [str(self.a_editor.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        channel = Channel.objects.get(id=res.data["channel"]["id"])
        self.assertFalse(channel.is_external)
        res = self.client.post(
            self.members_url(channel),
            {"user_ids": [str(self.b_owner.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 404)


class ExternalChatRemovalTests(ExternalChatTestCase):
    def test_guest_manager_removes_their_own_member(self):
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_owner.id, self.b_viewer.id], self.b_owner)
        self.authenticate(self.b_owner)
        res = self.client.delete(self.member_url(channel, self.b_viewer))
        self.assertEqual(res.status_code, 204)
        self.assertFalse(
            ChannelMember.objects.filter(
                channel=channel, user=self.b_viewer, is_deleted=False
            ).exists()
        )

    def test_host_owner_can_veto_an_individual_participant(self):
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.a_owner)
        res = self.client.delete(self.member_url(channel, self.b_viewer))
        self.assertEqual(res.status_code, 204)
        self.assertFalse(
            ChannelMember.objects.filter(
                channel=channel, user=self.b_viewer, is_deleted=False
            ).exists()
        )
        # A veto on one person is not a withdrawal of the share.
        grant.refresh_from_db()
        self.assertEqual(grant.status, ShareStatus.ACTIVE)

    def test_a_guest_viewer_cannot_remove_a_colleague(self):
        """A viewer-ceilinged participant has no lever over anyone.

        Ceiling matters here: at the `editor` ceiling both are channel
        editors and channel-level management legitimately applies. This
        asserts the case where neither team role nor channel role grants
        anything.
        """
        channel, grant = self.shared_chat(role_ceiling=VIEWER)
        add_external_participants(grant, [self.b_viewer.id, self.b_editor.id], self.b_owner)
        self.authenticate(self.b_viewer)
        res = self.client.delete(self.member_url(channel, self.b_editor))
        self.assertEqual(res.status_code, 403)
        self.assertTrue(
            ChannelMember.objects.filter(
                channel=channel, user=self.b_editor, is_deleted=False
            ).exists()
        )

    def test_a_guest_participant_can_always_leave(self):
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.b_viewer)
        res = self.client.delete(self.member_url(channel, self.b_viewer))
        self.assertEqual(res.status_code, 204)

    def test_revoking_the_grant_removes_every_participant(self):
        """Revocation must delete rows — nothing re-checks the grant."""
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_owner.id, self.b_viewer.id], self.b_owner)
        revoke_grant(grant, self.a_owner)
        self.assertFalse(
            ChannelMember.objects.filter(
                channel=channel, user__in=[self.b_owner, self.b_viewer], is_deleted=False
            ).exists()
        )

    def test_leaving_the_guest_team_removes_channel_access(self):
        """The hole delegation opens, and the cascade that closes it."""
        from origin.services.team_membership import remove_team_member

        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        remove_team_member(self.team_b.team_id, self.b_viewer.id)
        self.assertFalse(
            ChannelMember.objects.filter(
                channel=channel, user=self.b_viewer, is_deleted=False
            ).exists()
        )


class ExternalChatSharesEndpointTests(ExternalChatTestCase):
    """`GET /channels/{id}/shares/` — who else is in the room, by team."""

    def shares_url(self, channel):
        return f"{CHANNELS}{channel.id}/shares/"

    def test_the_host_sees_the_guest_team_and_its_roster(self):
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.a_owner)
        res = self.client.get(self.shares_url(channel))
        self.assertEqual(res.status_code, 200)
        share = res.data["shares"][0]
        self.assertEqual(share["side"], "given")
        self.assertEqual(share["teamName"], self.team_b.team_name)
        self.assertEqual(
            [p["userId"] for p in share["participants"]],
            [str(self.b_viewer.id)],
        )
        # The host may veto, never staff — so never `canAdmit`.
        self.assertFalse(share["canAdmit"])

    def test_a_guest_manager_may_read_the_share_before_joining_it(self):
        """The bootstrap case: nobody from B is in the chat yet.

        Team-scoped share lookups cannot answer this — B's manager reads
        the chat from A's shell and is no member of A — which is the
        reason this endpoint exists.
        """
        channel, _grant = self.shared_chat()
        self.authenticate(self.b_owner)
        res = self.client.get(self.shares_url(channel))
        self.assertEqual(res.status_code, 200)
        share = res.data["shares"][0]
        self.assertEqual(share["side"], "received")
        self.assertTrue(share["canAdmit"])

    def test_a_guest_viewer_is_told_they_cannot_admit(self):
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.b_viewer)
        res = self.client.get(self.shares_url(channel))
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["shares"][0]["canAdmit"])

    def test_a_guest_does_not_see_another_guest_teams_share(self):
        """Which organisation the other outsiders belong to is not theirs.

        Team C is connected to A and shares the same chat. B's people can
        see C's people in the member list — they are in the room — but the
        team-by-team breakdown stays between each guest and the host.
        """
        from origin.services.team_connection import request_connection, respond_to_connection

        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        conn = request_connection(self.team_a.team_id, self.team_c.team_id, self.a_owner)
        respond_to_connection(conn, self.c_owner, accept=True)
        c_grant = offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_c.team_id,
            object_type=ExternalGrant.ObjectType.CHANNEL,
            object_id=channel.id,
            role_ceiling=EDITOR,
            actor=self.a_owner,
        )
        respond_to_grant(c_grant, self.c_owner, accept=True)

        self.authenticate(self.b_viewer)
        res = self.client.get(self.shares_url(channel))
        self.assertEqual([s["teamId"] for s in res.data["shares"]], [str(self.team_b.team_id)])

        # The host sees every team, because the host consented to each.
        self.authenticate(self.a_owner)
        res = self.client.get(self.shares_url(channel))
        self.assertEqual(len(res.data["shares"]), 2)

    def test_a_stranger_gets_a_404(self):
        channel, _grant = self.shared_chat()
        self.authenticate(self.c_owner)
        res = self.client.get(self.shares_url(channel))
        self.assertEqual(res.status_code, 404)

    def test_an_internal_channel_reports_no_shares(self):
        self.authenticate(self.a_owner)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team_a.team_id),
                "title": "Internal",
            },
            format="json",
        )
        channel = Channel.objects.get(id=res.data["channel"]["id"])
        res = self.client.get(self.shares_url(channel))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["shares"], [])


class ExternalChatVisibilityTests(ExternalChatTestCase):
    def test_an_external_participant_sees_the_chat_in_the_host_team(self):
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.b_viewer)
        res = self.client.get(CHANNELS, {"team_id": str(self.team_a.team_id)})
        self.assertEqual(res.status_code, 200)
        self.assertIn(str(channel.id), [c["id"] for c in res.data["channels"]])

    def test_the_host_team_appears_in_my_teams_for_a_chat_only_participant(self):
        """Without the team shell the chat exists and is unreachable.

        A channel share leaves no `ProjectMembers` row, so the guest-shell
        path that covers project guests misses these people entirely.
        """
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.b_viewer)
        res = self.client.get(MY_TEAMS, {"user_id": str(self.b_viewer.id)})
        self.assertEqual(res.status_code, 200)
        self.assertIn(str(self.team_a.team_id), [str(t["teamId"]) for t in res.data])

    def test_the_host_roster_is_not_exposed_to_an_external_participant(self):
        """The shell is a place to render the chat, not a directory.

        Only co-participants of the shared object, whose identities that
        object's own member list already discloses.
        """
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.b_viewer)
        res = self.client.get(MY_TEAMS, {"user_id": str(self.b_viewer.id)})
        host = next(t for t in res.data if str(t["teamId"]) == str(self.team_a.team_id))
        visible = {str(m["userId"]) for m in host["teamMembers"]}
        self.assertNotIn(str(self.a_viewer.id), visible)
        # The chat's own members are not a secret from its own members.
        self.assertIn(str(self.a_owner.id), visible)

    def test_a_team_holding_a_grant_does_not_appear_for_uninvolved_members(self):
        """Being a colleague of a participant is not participation."""
        channel, grant = self.shared_chat()
        add_external_participants(grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.b_editor)
        res = self.client.get(MY_TEAMS, {"user_id": str(self.b_editor.id)})
        self.assertNotIn(str(self.team_a.team_id), {str(t["teamId"]) for t in res.data})
