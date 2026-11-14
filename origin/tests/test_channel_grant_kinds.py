"""MIP-1496: which chats can an `ExternalGrant` of type CHANNEL name?

`_resolve_object_team` only asks "which team owns this channel", so `offer_grant` (and the
`POST /api/v2/team/share/` endpoint) accepted ANY chat of the host team, whatever its kind. A
CHANNEL grant admits people by writing a `ChannelMember` row. That row is the membership for a
group chat, but:

  * a PROJECT (PM) chat is the project's people; its channel row only mirrors that (MIP-1480), so
    a guest holding the row got a share that opens nothing (a dead share, plus a stray row every
    raw-row reader in the code base then has to remember to ignore). A project is shared by a
    PROJECT grant, which writes the real `ProjectMembers` row;
  * a direct message is fixed to its pair (`ChannelMembersView.post` refuses to grow one), so a
    guest row would make a third person in a two-person chat.

The add-members endpoint already refuses both kinds; the share path must obey the same rule.
Only a group chat (GM) or a multi-person DM (MDM) can be shared by a channel grant.
"""

from __future__ import annotations

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.team_models import ExternalGrant
from origin.services.external_grants import ExternalGrantError, offer_grant, respond_to_grant
from origin.services.member_roles import EDITOR
from origin.tests.cross_team_fixtures import CrossTeamTestCase

SHARE_URL = "/api/v2/team/share/"


class ChannelGrantKinds(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.connect_a_and_b()
        self.pm_chat = Channel.objects.get(kind=ChannelKind.PM, project=self.project)
        self.dm = Channel.objects.create(team=self.team_a, kind=ChannelKind.DM, title="")
        for u in (self.a_editor, self.a_viewer):
            ChannelMember.objects.create(channel=self.dm, user=u)

    def offer(self, channel):
        return offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.CHANNEL,
            object_id=channel.id,
            role_ceiling=EDITOR,
            actor=self.a_owner,
        )

    def offer_over_http(self, channel):
        self.authenticate(self.a_owner)
        return self.client.post(
            SHARE_URL,
            {
                "team_id": str(self.team_a.team_id),
                "guest_team_id": str(self.team_b.team_id),
                "object_type": "channel",
                "object_id": str(channel.id),
            },
            format="json",
        )

    # -- the refusals -------------------------------------------------------------------- #

    def test_a_project_chat_cannot_be_offered_as_a_channel_share(self):
        with self.assertRaises(ExternalGrantError) as ctx:
            self.offer(self.pm_chat)
        self.assertEqual(ctx.exception.code, "bad_object")
        self.assertFalse(ExternalGrant.objects.filter(object_id=str(self.pm_chat.id)).exists())

    def test_a_direct_message_cannot_be_offered_either(self):
        with self.assertRaises(ExternalGrantError) as ctx:
            self.offer(self.dm)
        self.assertEqual(ctx.exception.code, "bad_object")
        self.assertFalse(ExternalGrant.objects.filter(object_id=str(self.dm.id)).exists())

    def test_the_share_endpoint_refuses_them_like_an_object_that_does_not_exist(self):
        missing = Channel(id="00000000-0000-0000-0000-000000000001")
        for chat in (self.pm_chat, self.dm):
            with self.subTest(kind=chat.kind):
                refused = self.offer_over_http(chat)
                nothing = self.offer_over_http(missing)
                self.assertEqual(refused.status_code, nothing.status_code, refused.content)
                self.assertEqual(refused.status_code, 404)
                self.assertEqual(refused.json(), nothing.json())
        self.assertFalse(
            ExternalGrant.objects.filter(object_type=ExternalGrant.ObjectType.CHANNEL).exists()
        )

    # -- what still works ---------------------------------------------------------------- #

    def test_a_group_chat_and_a_multi_person_dm_can_still_be_shared(self):
        for kind in (ChannelKind.GM, ChannelKind.MDM):
            with self.subTest(kind=kind):
                chat = Channel.objects.create(team=self.team_a, kind=kind, title="room")
                grant = self.offer(chat)
                respond_to_grant(grant, self.b_owner, accept=True)
                self.assertTrue(
                    ChannelMember.objects.filter(
                        channel=chat, user=self.b_owner, is_deleted=False
                    ).exists()
                )

    def test_a_project_share_still_admits_the_guest_to_the_project_chat(self):
        grant = self.active_project_grant()
        self.assertTrue(self._in_project(self.b_owner))
        self.assertEqual(grant.status, "active")


class GuestReadsAfterAnAcceptedShare(CrossTeamTestCase):
    """A grant on a project chat made before the refusal existed (written straight into the
    table) is inert: the guest's approver holds a stray row, which opens nothing (MIP-1480).

    Not pinned for a direct message: such a row WOULD still open it, which is why the offer
    is refused now; rows made earlier need a data audit, not a code path.
    """

    def setUp(self):
        super().setUp()
        self.connection = self.connect_a_and_b()
        self.pm_chat = Channel.objects.get(kind=ChannelKind.PM, project=self.project)
        Message.objects.create(
            channel=self.pm_chat, sender=self.a_editor, seq=1, body=[], body_text="host only text"
        )
        grant = ExternalGrant.objects.create(
            connection=self.connection,
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.CHANNEL,
            object_id=str(self.pm_chat.id),
            role_ceiling=EDITOR,
            status="pending",
            invited_by=self.a_owner,
        )
        respond_to_grant(grant, self.b_owner, accept=True)

    def read(self, chat):
        self.authenticate(self.b_owner)
        return self.client.get(f"/api/v3/channels/{chat.id}/messages/")

    def test_the_guest_cannot_read_the_project_chat(self):
        self.assertEqual(self.read(self.pm_chat).status_code, 404)
