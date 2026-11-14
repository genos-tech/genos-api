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

import uuid

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.team_models import ExternalGrant, ShareStatus
from origin.services.external_grants import (
    ExternalGrantError,
    add_external_participants,
    offer_grant,
    respond_to_grant,
)
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
                ChannelMember.objects.create(channel=chat, user=self.a_owner)
                grant = self.offer(chat)
                respond_to_grant(grant, self.b_owner, accept=True)
                self.assertTrue(
                    ChannelMember.objects.filter(
                        channel=chat, user=self.b_owner, is_deleted=False
                    ).exists()
                )

    # -- MIP-1522: the offerer must be IN the chat they lend out -------------------------- #

    def test_a_manager_who_is_not_in_the_group_chat_cannot_offer_it(self):
        for kind in (ChannelKind.GM, ChannelKind.MDM):
            with self.subTest(kind=kind):
                chat = Channel.objects.create(team=self.team_a, kind=kind, title="private room")
                ChannelMember.objects.create(
                    channel=chat, user=self.a_editor
                )  # a_owner is not in it
                with self.assertRaises(ExternalGrantError) as ctx:
                    self.offer(chat)
                # Same answer as a chat that does not exist: it does not say the chat is there.
                self.assertEqual(ctx.exception.code, "bad_object")
                self.assertFalse(
                    ExternalGrant.objects.filter(object_id=str(chat.id)).exists(),
                    "nothing was offered, nothing was notified",
                )

    def test_a_manager_who_left_the_chat_cannot_offer_it(self):
        chat = Channel.objects.create(team=self.team_a, kind=ChannelKind.GM, title="room")
        ChannelMember.objects.create(channel=chat, user=self.a_owner, is_deleted=True)
        with self.assertRaises(ExternalGrantError) as ctx:
            self.offer(chat)
        self.assertEqual(ctx.exception.code, "bad_object")

    def test_a_manager_in_the_chat_can_offer_it(self):
        chat = Channel.objects.create(team=self.team_a, kind=ChannelKind.GM, title="room")
        ChannelMember.objects.create(channel=chat, user=self.a_owner)
        self.assertEqual(self.offer(chat).status, ShareStatus.PENDING)

    def test_over_http_a_manager_outside_the_chat_gets_the_not_found_answer(self):
        chat = Channel.objects.create(team=self.team_a, kind=ChannelKind.GM, title="private room")
        ChannelMember.objects.create(channel=chat, user=self.a_editor)
        outside = self.offer_over_http(chat)
        missing = self.offer_over_http(Channel(id=uuid.uuid4()))
        self.assertEqual(outside.status_code, missing.status_code)
        self.assertEqual(outside.json(), missing.json())

    def test_a_project_share_still_admits_the_guest_to_the_project_chat(self):
        grant = self.active_project_grant()
        self.assertTrue(self._in_project(self.b_owner))
        self.assertEqual(grant.status, "active")


class GrantsMadeBeforeTheRefusal(CrossTeamTestCase):
    """Grants on a DM or a project chat that already exist (written straight into the table).

    A guest row on a DM opens it, so `_write_participant`, the one place cross-team access is
    written, refuses these kinds: accepting, adding a person and raising the ceiling all go
    through it. A project chat's stray row would open nothing (MIP-1480) and is refused too, so
    no stray row is written.
    """

    def setUp(self):
        super().setUp()
        self.connection = self.connect_a_and_b()
        self.pm_chat = Channel.objects.get(kind=ChannelKind.PM, project=self.project)
        self.dm = Channel.objects.create(team=self.team_a, kind=ChannelKind.DM, title="")
        for u in (self.a_editor, self.a_viewer):
            ChannelMember.objects.create(channel=self.dm, user=u)
        Message.objects.create(
            channel=self.dm, sender=self.a_editor, seq=1, body=[], body_text="private dm text"
        )

    def legacy_grant(self, chat, status="pending", ceiling=EDITOR):
        return ExternalGrant.objects.create(
            connection=self.connection,
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.CHANNEL,
            object_id=str(chat.id),
            role_ceiling=ceiling,
            status=status,
            invited_by=self.a_owner,
        )

    def test_accepting_a_pending_grant_on_a_dm_admits_nobody_and_the_guest_reads_nothing(self):
        grant = self.legacy_grant(self.dm)
        with self.assertRaises(ExternalGrantError) as ctx:
            respond_to_grant(grant, self.b_owner, accept=True)
        self.assertEqual(ctx.exception.code, "bad_object")
        self.assertFalse(ChannelMember.objects.filter(channel=self.dm, user=self.b_owner).exists())
        self.authenticate(self.b_owner)
        res = self.client.get(f"/api/v3/channels/{self.dm.id}/messages/")
        self.assertEqual(res.status_code, 404, res.content)

    def test_accepting_a_pending_grant_on_a_project_chat_writes_no_stray_row(self):
        grant = self.legacy_grant(self.pm_chat)
        with self.assertRaises(ExternalGrantError):
            respond_to_grant(grant, self.b_owner, accept=True)
        self.assertFalse(
            ChannelMember.objects.filter(channel=self.pm_chat, user=self.b_owner).exists()
        )

    def test_an_active_grant_on_a_dm_cannot_add_people(self):
        grant = self.legacy_grant(self.dm, status="active")
        with self.assertRaises(ExternalGrantError) as ctx:
            add_external_participants(grant, [self.b_editor.id], self.b_owner)
        self.assertEqual(ctx.exception.code, "bad_object")
        self.assertFalse(ChannelMember.objects.filter(channel=self.dm, user=self.b_editor).exists())

    def test_the_guest_cannot_read_a_project_chat_a_legacy_grant_names(self):
        """Evidence kept from the probe: a stray row on a project chat opens nothing."""
        ChannelMember.objects.create(channel=self.pm_chat, user=self.b_owner)
        self.authenticate(self.b_owner)
        res = self.client.get(f"/api/v3/channels/{self.pm_chat.id}/messages/")
        self.assertEqual(res.status_code, 404, res.content)
