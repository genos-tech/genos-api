"""Who you can put in a channel with you.

`ChannelListView.post` verified that the *caller* belonged to the team it
named, and then resolved the counterparties out of `User.objects` with no
scope at all. So any authenticated user could open a DM with any user in
the entire install by id — creating a real channel, a real
`ChannelMember` row for someone in a different tenant, and a message
surface into it. `_create_group` and `ChannelMembersView.post` had the
same shape with a list of ids instead of one.

That is a cross-tenant write, not just a read: the recipient gets a
channel they never asked for, from someone whose organisation they have
no relationship with.

Guests are admitted deliberately — they hold `ProjectMembers` rows and no
`TeamMembers` row, and a guest you share a project with is a legitimate
person to message. `is_team_participant` is the predicate that says so;
`is_team_member` would have denied them.

Every refusal is the same 404 the endpoint already returns for an unknown
id, so none of these can be used to test which user ids exist.
"""

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.member_roles import GUEST
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

CHANNELS = "/api/v3/channels/"


class ChannelCounterpartyScopeTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # A complete stranger: their own team, no overlap with self.team.
        self.outsider = User.objects.create_user(
            username="ccsout", email="ccsout@example.com", password="pw"
        )
        self.outsider_team = TeamMaster.objects.create(
            team_name="CCS Outsider", team_email="ccsout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)

    # ── DM ────────────────────────────────────────────────────────────

    def test_cannot_open_a_dm_with_someone_outside_the_team(self):
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": str(self.outsider.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(ChannelMember.objects.filter(user=self.outsider).exists())

    def test_an_outsider_cannot_open_a_dm_into_our_team(self):
        """The other direction — the caller check already covered this,
        but it is the half people assume is the whole fix."""
        self.authenticate(self.outsider)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": str(self.user.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_a_teammate_dm_still_works(self):
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": str(self.user2.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    def test_the_self_dm_still_works(self):
        """Single-member scratch channel behind the todo/calendar panes —
        it would be an easy casualty of a counterparty check."""
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": str(self.user.id),
            },
            format="json",
        )
        self.assertIn(res.status_code, (200, 201))

    def test_a_stranger_and_an_unknown_id_are_indistinguishable(self):
        self.authenticate(self.user)
        bodies = [
            {"other_user_id": str(self.outsider.id)},
            {"other_user_id": "8b1f3c2e-0000-4000-8000-000000000000"},
        ]
        codes = []
        for extra in bodies:
            res = self.client.post(
                CHANNELS,
                {"kind": ChannelKind.DM, "team_id": str(self.team.team_id), **extra},
                format="json",
            )
            codes.append(res.status_code)
        self.assertEqual(codes[0], codes[1], 404)

    def test_a_malformed_user_id_is_404_not_500(self):
        """`id` is a UUIDField: a bad value raises `ValidationError` out
        of `to_python`, which is NOT a `ValueError` and was uncaught."""
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": "not-a-uuid",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_a_removed_teammate_can_no_longer_be_dmed(self):
        membership = TeamMembers.objects.get(team=self.team, attendee=self.user2)
        membership.is_deleted = True
        membership.save(update_fields=["is_deleted"])
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": str(self.user2.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    # ── GM / MDM create ───────────────────────────────────────────────

    def test_cannot_create_a_group_containing_an_outsider(self):
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team.team_id),
                "title": "Planted",
                "member_user_ids": [str(self.user2.id), str(self.outsider.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(Channel.objects.filter(title="Planted").exists())

    def test_one_bad_id_rejects_the_whole_request(self):
        """All-or-nothing: silently dropping the outsider would build a
        channel whose member list differs from what was asked for, and
        would disclose by omission which ids were rejected."""
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team.team_id),
                "title": "Partial",
                "member_user_ids": [str(self.user2.id), str(self.outsider.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(ChannelMember.objects.filter(user=self.user2).exists())

    def test_a_group_of_teammates_still_works(self):
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team.team_id),
                "title": "Legit",
                "member_user_ids": [str(self.user2.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        channel = Channel.objects.get(title="Legit")
        self.assertEqual(ChannelMember.objects.filter(channel=channel).count(), 2)

    def test_an_empty_member_list_still_works(self):
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team.team_id),
                "title": "Solo",
                "member_user_ids": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    def test_a_malformed_id_in_the_list_is_404_not_500(self):
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team.team_id),
                "title": "Bad",
                "member_user_ids": ["not-a-uuid"],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    # ── adding members to an existing channel ─────────────────────────

    def test_cannot_add_an_outsider_to_an_existing_channel(self):
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Ours")
        ChannelMember.objects.create(channel=channel, user=self.user, role="owner")
        self.authenticate(self.user)
        res = self.client.post(
            f"{CHANNELS}{channel.id}/members/",
            {"user_ids": [str(self.outsider.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(ChannelMember.objects.filter(channel=channel, user=self.outsider).exists())

    def test_adding_a_teammate_still_works(self):
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Ours")
        ChannelMember.objects.create(channel=channel, user=self.user, role="owner")
        self.authenticate(self.user)
        res = self.client.post(
            f"{CHANNELS}{channel.id}/members/",
            {"user_ids": [str(self.user2.id)]},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201))
        self.assertTrue(ChannelMember.objects.filter(channel=channel, user=self.user2).exists())

    def test_add_members_is_scoped_to_the_channels_team_not_the_callers(self):
        """`_get_channel_for_user` proves the caller is in the channel; the
        team that matters for the new members is the channel's."""
        other_team = TeamMaster.objects.create(
            team_name="CCS Second", team_email="ccs2@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user)
        neighbour = User.objects.create_user(
            username="ccsneighbour", email="ccsneighbour@example.com", password="pw"
        )
        TeamMembers.objects.create(team=other_team, attendee=neighbour)

        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="First team")
        ChannelMember.objects.create(channel=channel, user=self.user, role="owner")
        self.authenticate(self.user)
        res = self.client.post(
            f"{CHANNELS}{channel.id}/members/",
            {"user_ids": [str(neighbour.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 404)


class GuestsAreReachableTests(BaseAPITestCase):
    """A guest holds ProjectMembers rows and no TeamMembers row.

    Gating counterparties on `is_team_member` would have made every
    external collaborator unmessageable — a feature regression dressed as
    a security fix. `is_team_participant` exists for exactly this.
    """

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Client work", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.guest = User.objects.create_user(
            username="ccsguest", email="ccsguest@agency.example", password="pw"
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.guest, member_role=GUEST
        )

    def test_a_guest_is_not_a_team_member(self):
        """Guards the premise — if this ever becomes false the test below
        stops proving anything."""
        self.assertFalse(TeamMembers.objects.filter(team=self.team, attendee=self.guest).exists())

    def test_a_member_can_dm_a_guest(self):
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.DM,
                "team_id": str(self.team.team_id),
                "other_user_id": str(self.guest.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    def test_a_guest_can_be_added_to_a_group(self):
        self.authenticate(self.user)
        res = self.client.post(
            CHANNELS,
            {
                "kind": ChannelKind.GM,
                "team_id": str(self.team.team_id),
                "title": "With guest",
                "member_user_ids": [str(self.guest.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
