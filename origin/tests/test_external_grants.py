"""Object grants, and the delegated roster they authorize.

Two things are being defended here, and they pull in opposite directions.

The **delegation** must genuinely work: a guest-team manager has to be
able to add somebody long after the grant was approved, with the host
doing nothing at all. If that ever needs a host action, the feature has
regressed into the per-person approval it was designed to avoid.

The **bounds** on delegation must hold: only members of the guest team,
never above the role ceiling, and access must disappear on revoke or on
leaving the guest team. Because nothing re-checks a grant at read time,
each of those is a permanent hole if it leaks even once.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import ExternalGrant, ShareStatus
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.project.prj_models import ProjectMembers
from origin.services.external_grants import (
    ExternalGrantError,
    add_external_participants,
    grant_admitting,
    offer_grant,
    participant_ids,
    remove_external_participants,
    respond_to_grant,
    revoke_grant,
    set_role_ceiling,
)
from origin.services.member_roles import EDITOR, GUEST, VIEWER
from origin.services.team_connection import revoke_connection
from origin.services.team_membership import remove_team_member
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.views.utils.scope_guards import is_external_participant, is_guest


class GrantLifecycleTests(CrossTeamTestCase):
    def _offer(self, **kwargs):
        params = {
            "owner_team_id": self.team_a.team_id,
            "guest_team_id": self.team_b.team_id,
            "object_type": ExternalGrant.ObjectType.PROJECT,
            "object_id": self.project.project_id,
            "role_ceiling": EDITOR,
            "actor": self.a_owner,
        }
        params.update(kwargs)
        return offer_grant(**params)

    def test_a_grant_requires_an_active_connection(self):
        with self.assertRaises(ExternalGrantError) as ctx:
            self._offer()
        self.assertEqual(ctx.exception.code, "not_connected")

    def test_a_pending_connection_is_not_enough(self):
        from origin.services.team_connection import request_connection

        request_connection(self.team_a.team_id, self.team_b.team_id, self.a_owner)
        with self.assertRaises(ExternalGrantError) as ctx:
            self._offer()
        self.assertEqual(ctx.exception.code, "not_connected")

    def test_a_host_cannot_lend_out_another_teams_object(self):
        """The claim in the request is checked against the real owner."""
        self.connect_a_and_b()
        with self.assertRaises(ExternalGrantError) as ctx:
            self._offer(object_id=self.foreign_project.project_id)
        self.assertEqual(ctx.exception.code, "not_owned")

    def test_a_nonexistent_object_is_refused(self):
        self.connect_a_and_b()
        with self.assertRaises(ExternalGrantError) as ctx:
            self._offer(object_id=99999999)
        self.assertEqual(ctx.exception.code, "bad_object")

    def test_only_a_host_manager_may_offer(self):
        self.connect_a_and_b()
        with self.assertRaises(ExternalGrantError) as ctx:
            self._offer(actor=self.a_viewer)
        self.assertEqual(ctx.exception.code, "not_a_manager")

    def test_only_a_guest_manager_may_accept(self):
        self.connect_a_and_b()
        grant = self._offer()
        with self.assertRaises(ExternalGrantError) as ctx:
            respond_to_grant(grant, self.b_viewer, accept=True)
        self.assertEqual(ctx.exception.code, "not_a_manager")

        # The host cannot accept on the guest's behalf either.
        with self.assertRaises(ExternalGrantError) as ctx:
            respond_to_grant(grant, self.a_owner, accept=True)
        self.assertEqual(ctx.exception.code, "not_a_manager")

    def test_accepting_admits_the_approver_and_nobody_else(self):
        """Acceptance walks ONE person through the door: the one who opened it.

        It used to admit nobody, which read well as a rule and left the
        share unusable — the roster that admits people is reached through
        the object, and nobody could open the object. The approver is the
        person we know wants in, and stopping there keeps a fifty-person
        team out of another company's project.
        """
        self.active_project_grant()
        self.assertEqual(
            [str(uid) for uid in ProjectMembers.objects.values_list("attendee_id", flat=True)],
            [str(self.b_owner.id)],
        )

    def test_re_offering_after_a_revoke_reuses_the_row(self):
        grant = self.active_project_grant()
        revoke_grant(grant, self.a_owner)
        again = self._offer()
        self.assertEqual(again.id, grant.id)
        self.assertEqual(again.status, ShareStatus.PENDING)
        self.assertIsNone(again.approved_by)

    def test_only_a_host_manager_may_change_the_ceiling(self):
        grant = self.active_project_grant(role_ceiling=VIEWER)
        with self.assertRaises(ExternalGrantError) as ctx:
            set_role_ceiling(grant, EDITOR, self.b_owner)
        self.assertEqual(ctx.exception.code, "not_a_manager")
        set_role_ceiling(grant, EDITOR, self.a_owner)
        grant.refresh_from_db()
        self.assertEqual(grant.role_ceiling, EDITOR)


class DelegatedRosterTests(CrossTeamTestCase):
    """The repeatable half: the guest team runs its own participant list."""

    def test_a_guest_manager_admits_their_own_people(self):
        grant = self.active_project_grant()
        admitted = add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        self.assertEqual(admitted, [str(self.b_viewer.id)])

        row = ProjectMembers.objects.get(project=self.project, attendee=self.b_viewer)
        self.assertEqual(row.member_role, GUEST)
        self.assertEqual(str(row.team_id), str(self.team_a.team_id))

    def test_adding_needs_no_host_action_at_any_later_time(self):
        """The point of one-time approval, stated as a test.

        Nothing the host does sits between acceptance and this add — the
        grant is not touched, and no approval row is created or consumed.
        """
        grant = self.active_project_grant()
        before = grant.ts_updated_at

        add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        add_external_participants(grant, [self.b_owner.id], self.b_editor)

        grant.refresh_from_db()
        self.assertEqual(grant.ts_updated_at, before)
        self.assertEqual(
            sorted(participant_ids(grant)), sorted([str(self.b_viewer.id), str(self.b_owner.id)])
        )

    def test_a_guest_viewer_cannot_admit_anyone(self):
        grant = self.active_project_grant()
        with self.assertRaises(ExternalGrantError) as ctx:
            add_external_participants(grant, [self.b_viewer.id], self.b_viewer)
        self.assertEqual(ctx.exception.code, "not_a_manager")
        self.assertFalse(self._in_project(self.b_viewer))

    def test_the_grant_cannot_be_re_shared_to_a_third_party(self):
        """The bound that stops one grant becoming transitive access."""
        grant = self.active_project_grant()
        with self.assertRaises(ExternalGrantError) as ctx:
            add_external_participants(grant, [self.c_owner.id], self.b_editor)
        self.assertEqual(ctx.exception.code, "not_guest_member")
        self.assertFalse(self._in_project(self.c_owner))

    def test_a_host_member_is_not_admissible_as_a_guest_participant(self):
        grant = self.active_project_grant()
        with self.assertRaises(ExternalGrantError) as ctx:
            add_external_participants(grant, [self.a_viewer.id], self.b_editor)
        self.assertEqual(ctx.exception.code, "not_guest_member")

    def test_a_pending_grant_admits_nobody(self):
        self.connect_a_and_b()
        grant = offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.PROJECT,
            object_id=self.project.project_id,
            role_ceiling=EDITOR,
            actor=self.a_owner,
        )
        with self.assertRaises(ExternalGrantError) as ctx:
            add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        self.assertEqual(ctx.exception.code, "not_active")

    def test_a_batch_containing_an_outsider_admits_nobody(self):
        """Atomic, so a mixed list cannot half-succeed."""
        grant = self.active_project_grant()
        with self.assertRaises(ExternalGrantError):
            add_external_participants(grant, [self.b_viewer.id, self.c_owner.id], self.b_editor)
        self.assertFalse(self._in_project(self.b_viewer))
        self.assertFalse(self._in_project(self.c_owner))

    def test_the_guest_team_may_remove_its_own_people(self):
        grant = self.active_project_grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        self.assertEqual(remove_external_participants(grant, [self.b_viewer.id], self.b_editor), 1)
        self.assertFalse(self._in_project(self.b_viewer))

    def test_the_host_keeps_a_veto_over_an_individual(self):
        grant = self.active_project_grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        self.assertEqual(remove_external_participants(grant, [self.b_viewer.id], self.a_owner), 1)
        self.assertFalse(self._in_project(self.b_viewer))

    def test_the_host_cannot_add_the_guest_teams_people(self):
        """Host has the veto, guest has the administration."""
        grant = self.active_project_grant()
        with self.assertRaises(ExternalGrantError) as ctx:
            add_external_participants(grant, [self.b_viewer.id], self.a_owner)
        self.assertEqual(ctx.exception.code, "not_a_manager")

    def test_an_outsider_can_neither_add_nor_remove(self):
        grant = self.active_project_grant()
        with self.assertRaises(ExternalGrantError):
            add_external_participants(grant, [self.b_viewer.id], self.c_owner)
        with self.assertRaises(ExternalGrantError):
            remove_external_participants(grant, [self.b_viewer.id], self.c_owner)


class RoleCeilingTests(CrossTeamTestCase):
    def _folder_grant(self, role_ceiling):
        self.connect_a_and_b()
        grant = offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.NOTE_FOLDER,
            object_id=self.folder.folder_id,
            role_ceiling=role_ceiling,
            actor=self.a_owner,
        )
        return respond_to_grant(grant, self.b_owner, accept=True)

    def test_a_viewer_ceiling_clamps_an_editor_request(self):
        grant = self._folder_grant(VIEWER)
        add_external_participants(grant, [self.b_viewer.id], self.b_editor, role=EDITOR)
        row = NoteFolderPermission.objects.get(folder=self.folder, user=self.b_viewer)
        self.assertEqual(row.role_id, 3)  # viewer, not the requested editor

    def test_an_editor_ceiling_allows_editor(self):
        grant = self._folder_grant(EDITOR)
        add_external_participants(grant, [self.b_viewer.id], self.b_editor, role=EDITOR)
        row = NoteFolderPermission.objects.get(folder=self.folder, user=self.b_viewer)
        self.assertEqual(row.role_id, 2)
        self.assertEqual(row.via_group_type, "external_grant")
        self.assertEqual(row.via_group_id, str(grant.id))

    def test_a_bogus_ceiling_is_refused_at_offer_time(self):
        self.connect_a_and_b()
        with self.assertRaises(ExternalGrantError) as ctx:
            offer_grant(
                owner_team_id=self.team_a.team_id,
                guest_team_id=self.team_b.team_id,
                object_type=ExternalGrant.ObjectType.PROJECT,
                object_id=self.project.project_id,
                role_ceiling="owner",
                actor=self.a_owner,
            )
        self.assertEqual(ctx.exception.code, "bad_role")

    def test_a_project_participant_is_always_a_guest_whatever_the_ceiling(self):
        """`editor` on a project would confer member management."""
        grant = self.active_project_grant(role_ceiling=EDITOR)
        add_external_participants(grant, [self.b_viewer.id], self.b_editor, role=EDITOR)
        row = ProjectMembers.objects.get(project=self.project, attendee=self.b_viewer)
        self.assertEqual(row.member_role, GUEST)


class RevocationCascadeTests(CrossTeamTestCase):
    """Nothing re-checks a grant at read time, so revoking must delete rows."""

    def test_revoking_a_grant_deletes_the_participation_rows(self):
        grant = self.active_project_grant()
        add_external_participants(grant, [self.b_viewer.id, self.b_owner.id], self.b_editor)

        self.assertEqual(revoke_grant(grant, self.a_owner), 2)
        grant.refresh_from_db()
        self.assertEqual(grant.status, ShareStatus.REVOKED)
        self.assertEqual(ProjectMembers.objects.count(), 0)

    def test_the_guest_team_may_also_hand_a_grant_back(self):
        grant = self.active_project_grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        # Two rows: the viewer just admitted, and the approver, who was
        # admitted by accepting the offer.
        self.assertEqual(revoke_grant(grant, self.b_owner), 2)

    def test_an_outsider_cannot_revoke_a_grant(self):
        grant = self.active_project_grant()
        with self.assertRaises(ExternalGrantError) as ctx:
            revoke_grant(grant, self.c_owner)
        self.assertEqual(ctx.exception.code, "not_a_manager")

    def test_revoking_the_connection_cascades_to_every_grant_inside_it(self):
        grant = self.active_project_grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_editor)

        folder_grant = offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.NOTE_FOLDER,
            object_id=self.folder.folder_id,
            role_ceiling=EDITOR,
            actor=self.a_owner,
        )
        respond_to_grant(folder_grant, self.b_owner, accept=True)
        add_external_participants(folder_grant, [self.b_viewer.id], self.b_editor)

        withdrawn = revoke_connection(grant.connection, self.a_owner)
        # Four: an admitted viewer plus the approver, on each of two grants.
        self.assertEqual(withdrawn, 4)
        self.assertEqual(ProjectMembers.objects.count(), 0)
        self.assertEqual(NoteFolderPermission.objects.count(), 0)
        for row in ExternalGrant.objects.all():
            self.assertEqual(row.status, ShareStatus.REVOKED)

    def test_adding_after_a_revoke_is_refused(self):
        grant = self.active_project_grant()
        revoke_grant(grant, self.a_owner)
        with self.assertRaises(ExternalGrantError) as ctx:
            add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        self.assertEqual(ctx.exception.code, "not_active")

    def test_leaving_the_guest_team_gives_up_the_hosts_data(self):
        """The hole delegation opens, and the cascade that closes it."""
        grant = self.active_project_grant()
        add_external_participants(grant, [self.b_viewer.id, self.b_owner.id], self.b_editor)

        withdrawn = remove_team_member(self.team_b.team_id, self.b_viewer.id)

        self.assertEqual(withdrawn, 1)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.b_viewer).exists()
        )
        # Everyone else's access is untouched.
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.b_owner).exists()
        )

    def test_leaving_via_the_endpoint_runs_the_cascade(self):
        grant = self.active_project_grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_editor)

        self.authenticate(self.b_viewer)
        res = self.client.post(
            "/api/v2/team/leave/",
            {"team_id": str(self.team_b.team_id), "attendee_id": str(self.b_viewer.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(self._in_project(self.b_viewer))


class ChannelParticipationTests(CrossTeamTestCase):
    """The channel writer, whose table soft-deletes instead of deleting."""

    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team_a,
            kind=ChannelKind.GM,
            title="External Chat",
            owner=self.a_owner,
            is_private=True,
        )

    def _grant(self):
        self.connect_a_and_b()
        grant = offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.CHANNEL,
            object_id=self.channel.id,
            role_ceiling=EDITOR,
            actor=self.a_owner,
        )
        return respond_to_grant(grant, self.b_owner, accept=True)

    def test_admitting_writes_a_channel_member_row(self):
        grant = self._grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_editor, role=VIEWER)
        row = ChannelMember.objects.get(channel=self.channel, user=self.b_viewer)
        self.assertEqual(row.role, "member")
        self.assertFalse(row.is_deleted)

    def test_removing_soft_deletes_and_re_adding_revives_the_row(self):
        grant = self._grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        remove_external_participants(grant, [self.b_viewer.id], self.b_editor)

        row = ChannelMember.objects.get(channel=self.channel, user=self.b_viewer)
        self.assertTrue(row.is_deleted)
        # The approver keeps their own row throughout — removing one person
        # is not revoking the share.
        self.assertEqual(participant_ids(grant), [str(self.b_owner.id)])

        add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        self.assertEqual(
            ChannelMember.objects.filter(channel=self.channel, user=self.b_viewer).count(), 1
        )
        self.assertEqual(set(participant_ids(grant)), {str(self.b_viewer.id), str(self.b_owner.id)})

    def test_grant_admitting_identifies_the_route_in(self):
        grant = self._grant()
        found = grant_admitting(ExternalGrant.ObjectType.CHANNEL, self.channel.id, self.b_viewer.id)
        self.assertEqual(found.id, grant.id)
        # A host-team member reaches the channel by their own membership.
        self.assertIsNone(
            grant_admitting(ExternalGrant.ObjectType.CHANNEL, self.channel.id, self.a_viewer.id)
        )
        self.assertIsNone(
            grant_admitting(ExternalGrant.ObjectType.CHANNEL, self.channel.id, self.c_owner.id)
        )


class ExternalParticipantGuardTests(CrossTeamTestCase):
    def test_an_admissible_outsider_is_an_external_participant(self):
        self.active_project_grant()
        self.assertTrue(is_external_participant(self.team_a.team_id, self.b_viewer.id))
        self.assertFalse(is_external_participant(self.team_a.team_id, self.c_owner.id))

    def test_a_host_member_is_never_an_external_participant(self):
        self.active_project_grant()
        self.assertFalse(is_external_participant(self.team_a.team_id, self.a_viewer.id))
        self.assertFalse(is_external_participant(self.team_a.team_id, self.a_owner.id))

    def test_a_shared_project_participant_is_also_a_guest(self):
        """The overlap that makes every existing guest narrowing apply."""
        grant = self.active_project_grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        self.assertTrue(is_guest(self.team_a.team_id, self.b_viewer.id))

    def test_malformed_input_answers_no(self):
        self.assertFalse(is_external_participant(None, self.b_viewer.id))
        self.assertFalse(is_external_participant(self.team_a.team_id, None))


class ParticipantEndpointTests(CrossTeamTestCase):
    def test_the_guest_team_manages_the_roster_over_http(self):
        grant = self.active_project_grant()
        self.authenticate(self.b_editor)

        res = self.client.post(
            "/api/v2/team/share/participants/",
            {"grant_id": str(grant.id), "user_ids": [str(self.b_viewer.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["admitted"], [str(self.b_viewer.id)])

        res = self.client.get(f"/api/v2/team/share/participants/?grant_id={grant.id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            {p["userId"] for p in res.data["participants"]},
            {str(self.b_viewer.id), str(self.b_owner.id)},
        )

        res = self.client.delete(
            "/api/v2/team/share/participants/",
            {"grant_id": str(grant.id), "user_ids": [str(self.b_viewer.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["removed"], 1)

    def test_the_host_can_read_the_roster_of_its_own_object(self):
        grant = self.active_project_grant()
        add_external_participants(grant, [self.b_viewer.id], self.b_editor)
        self.authenticate(self.a_viewer)
        res = self.client.get(f"/api/v2/team/share/participants/?grant_id={grant.id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data["participants"]), 2)

    def test_an_outsider_reading_the_roster_gets_404(self):
        grant = self.active_project_grant()
        self.authenticate(self.c_owner)
        res = self.client.get(f"/api/v2/team/share/participants/?grant_id={grant.id}")
        self.assertEqual(res.status_code, 404)

    def test_offer_and_accept_over_http(self):
        self.connect_a_and_b()
        self.authenticate(self.a_owner)
        res = self.client.post(
            "/api/v2/team/share/",
            {
                "team_id": str(self.team_a.team_id),
                "guest_team_id": str(self.team_b.team_id),
                "object_type": "project",
                "object_id": str(self.project.project_id),
                "role_ceiling": EDITOR,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data["side"], "given")
        grant_id = res.data["grantId"]

        self.authenticate(self.b_owner)
        res = self.client.post(
            "/api/v2/team/share/respond/",
            {"grant_id": grant_id, "accept": True},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["side"], "received")
        self.assertEqual(res.data["status"], ShareStatus.ACTIVE)

        res = self.client.get(f"/api/v2/team/share/?team_id={self.team_b.team_id}")
        self.assertEqual(len(res.data["shares"]), 1)

    def test_offering_another_teams_object_is_404(self):
        self.connect_a_and_b()
        self.authenticate(self.a_owner)
        res = self.client.post(
            "/api/v2/team/share/",
            {
                "team_id": str(self.team_a.team_id),
                "guest_team_id": str(self.team_b.team_id),
                "object_type": "project",
                "object_id": str(self.foreign_project.project_id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
