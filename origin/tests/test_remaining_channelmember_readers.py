"""MIP-1523: the last raw `ChannelMember` readers, and the personal-note comment fan-out.

A GM, DM or MDM membership IS its `ChannelMember` row. Only a project chat (PM) mirrors the
project, so only there can a stray row (a person who is not on the project) wrongly count.
Each reader was checked against that:

  * kept raw, each pinned below: the GM-join approval (`_gm_channel_by_uuid` only resolves a GM),
    the DM partner name on the notes list (a DM), `chat_note_roles` (writes roles per kind, and a
    PM note gets its creator only), `resolve_gm_role` (every caller refuses a PM chat first);
  * narrowed: `rosters_of_held_objects` on a CHANNEL grant, which feeds the people directory and
    guest visibility. A legacy grant on a project chat (`_channel_shareable_or_raise` refuses new
    ones) must not make a stray row a co-participant;
  * `rows_for_grant` / `participant_ids` stay raw on purpose: they are the rows a grant WROTE, and
    revoking walks them to delete every one.

And a comment on a personal note filed in a TEAM folder now reaches the folder's readers, not
only the note's owner and explicit grants: the same set `get_effective_role` lets open the note.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Activity, ChannelKind, ChannelMember
from origin.models.common.team_models import ExternalGrant, ShareStatus, TeamMembers
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.services.external_grants import (
    participant_ids,
    revoke_grant,
    rosters_of_held_objects,
)
from origin.services.member_roles import EDITOR
from origin.services.team_connection import get_connection
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.tests.test_chat_note_one_rule import ChatNoteFixtures
from origin.views.common.inbox_views import _gm_channel_by_uuid
from origin.views.utils.chat_note_target import chat_note_roles
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_OWNER, get_effective_role

User = get_user_model()
COMMENT_URL = "/api/v3/activities/surface-comment/"
SURFACE_PERSONAL_NOTE = 6


class KeptRawReaders(ChatNoteFixtures):
    """GM/DM/MDM-only readers: the raw row is the membership. Pinned, not changed."""

    def setUp(self):
        super().setUp()
        self.stray = User.objects.create_user(
            username="stray", email="stray@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.stray)
        self.project = self.project_of("KRR", self.reader)
        self.pm = self.pm_chat(self.project)
        ChannelMember.objects.update_or_create(channel=self.pm, user=self.stray)

    def test_the_gm_join_approval_never_resolves_a_project_chat(self):
        self.assertIsNone(_gm_channel_by_uuid(self.pm.id))
        self.assertEqual(_gm_channel_by_uuid(self.gm(self.reader).id).kind, ChannelKind.GM)

    def test_a_project_chat_note_gets_its_creator_only_whatever_rows_the_chat_holds(self):
        roles = chat_note_roles(channel=self.pm, creator_id=self.reader.id)
        self.assertEqual(roles, {str(self.reader.id): ROLE_OWNER})
        self.assertNotIn(str(self.stray.id), roles)

    def test_every_member_endpoint_refuses_a_project_chat_before_it_reads_a_role(self):
        # `resolve_gm_role` reads the raw row, so it must never be reached with a PM chat.
        self.authenticate(self.reader)
        url = f"/api/v3/channels/{self.pm.id}/members/{self.stray.id}/"
        self.assertEqual(
            self.client.patch(url, {"member_role": "editor"}, format="json").status_code, 400
        )
        self.assertEqual(self.client.delete(url).status_code, 400)
        self.assertTrue(
            ChannelMember.objects.filter(
                channel=self.pm, user=self.stray, is_deleted=False
            ).exists()
        )


class LegacyProjectChatGrant(CrossTeamTestCase):
    """A channel grant on a PROJECT chat can no longer be offered (MIP-1496) but old ones exist:
    their stray guest row must not make the guest a co-participant of the host's people."""

    def setUp(self):
        super().setUp()
        self.connect_a_and_b()
        self.pm = (
            self.project.channels.get(kind=ChannelKind.PM)
            if hasattr(self.project, "channels")
            else None
        )
        if self.pm is None:
            from origin.models.chat.unified_models import Channel

            self.pm = Channel.objects.get(kind=ChannelKind.PM, project=self.project)
        # the host's real member, and a guest team person with the stray row the grant wrote
        ProjectMembers.objects.get_or_create(
            team=self.team_a, project=self.project, attendee=self.a_editor
        )
        ChannelMember.objects.update_or_create(channel=self.pm, user=self.b_viewer)
        # a guest who was really put on the project (a real member of the project chat)
        ProjectMembers.objects.get_or_create(
            team=self.team_a, project=self.project, attendee=self.b_editor
        )
        self.grant = ExternalGrant.objects.create(
            connection=get_connection(self.team_a.team_id, self.team_b.team_id),
            owner_team=self.team_a,
            guest_team=self.team_b,
            object_type=ExternalGrant.ObjectType.CHANNEL,
            object_id=str(self.pm.id),
            role_ceiling=EDITOR,
            status=ShareStatus.ACTIVE,
            invited_by=self.a_owner,
            approved_by=self.b_owner,
        )

    def test_a_stray_guest_row_is_not_on_the_object(self):
        rosters = rosters_of_held_objects([self.grant], self.b_viewer.id)
        self.assertEqual(rosters, {})

    def test_the_roster_of_a_real_member_leaves_the_stray_row_out(self):
        roster = {
            str(u) for u in rosters_of_held_objects([self.grant], self.b_editor.id)[self.grant.id]
        }
        self.assertIn(str(self.b_editor.id), roster)
        self.assertIn(str(self.a_editor.id), roster)
        self.assertNotIn(str(self.b_viewer.id), roster)

    def test_revoking_still_deletes_the_stray_row_the_grant_wrote(self):
        self.assertIn(str(self.b_viewer.id), participant_ids(self.grant))
        revoke_grant(self.grant)
        self.assertFalse(
            ChannelMember.objects.filter(
                channel=self.pm, user=self.b_viewer, is_deleted=False
            ).exists()
        )


class PersonalNoteCommentFanOut(ChatNoteFixtures):
    """The recipients of a comment on a personal note are the people who can open it."""

    def setUp(self):
        super().setUp()
        self.member2 = User.objects.create_user(
            username="member2", email="member2@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.member2)
        self.everyone = [self.owner, self.reader, self.member2]
        self.everyone += [
            m.attendee for m in TeamMembers.objects.filter(team=self.team, is_deleted=False)
        ]

    def folder(
        self, *, visibility=None, parent=None, scope=PersonalNoteFolder.SCOPE_TEAM, owner=None
    ):
        return PersonalNoteFolder.objects.create(
            team=self.team,
            owner=owner or self.owner,
            name="F",
            scope=scope,
            visibility=visibility,
            parent_folder_id=parent.folder_id if parent else None,
        )

    def note_in(self, folder=None):
        note = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.owner,
            title="Diary",
            body=[],
            folder_id=folder.folder_id if folder else None,
        )
        NotePermissionMaster.objects.create(
            team=self.team, user=self.owner, note_type=1, note_id=note.note_id, role_id=ROLE_OWNER
        )
        return note

    def comment(self, note):
        self.authenticate(self.owner)
        res = self.client.post(
            COMMENT_URL,
            {
                "surface_type": SURFACE_PERSONAL_NOTE,
                "team_id": str(self.team.team_id),
                "entity_key": f"comment-participant:{note.note_id}:x",
                "meta": {"noteId": note.note_id, "noteType": NOTE_TYPE_PERSONAL},
                "mentioned_user_ids": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return {a["recipientUserId"] for a in res.json()["activities"]}

    def readers(self, note):
        """Everyone on the team who can open the note, minus the commenter (the owner)."""
        return {
            str(u.id)
            for u in {x.id: x for x in self.everyone}.values()
            if str(u.id) != str(self.owner.id)
            and get_effective_role(u.id, NOTE_TYPE_PERSONAL, note.note_id, self.team.team_id)
            is not None
        }

    def assert_recipients_are_the_readers(self, note, expected):
        got = self.comment(note)
        self.assertEqual(got, self.readers(note))
        self.assertEqual(got, {str(u.id) for u in expected})

    def test_a_public_team_folder_notifies_the_team(self):
        note = self.note_in(self.folder(visibility=PersonalNoteFolder.VISIBILITY_PUBLIC))
        self.assert_recipients_are_the_readers(note, [self.reader, self.member2, self.user])

    def test_a_private_folder_notifies_its_grantees_only(self):
        folder = self.folder(visibility=PersonalNoteFolder.VISIBILITY_PRIVATE)
        NoteFolderPermission.objects.create(
            team=self.team, folder_id=folder.folder_id, user=self.reader, role_id=ROLE_OWNER
        )
        note = self.note_in(folder)
        self.assert_recipients_are_the_readers(note, [self.reader])

    def test_a_child_that_inherits_reaches_its_parents_grantees(self):
        parent = self.folder(visibility=PersonalNoteFolder.VISIBILITY_PRIVATE)
        NoteFolderPermission.objects.create(
            team=self.team, folder_id=parent.folder_id, user=self.reader, role_id=ROLE_OWNER
        )
        child = self.folder(parent=parent)
        note = self.note_in(child)
        self.assert_recipients_are_the_readers(note, [self.reader])

    def test_a_folder_owner_who_is_not_the_note_owner_is_notified(self):
        folder = self.folder(visibility=PersonalNoteFolder.VISIBILITY_PRIVATE, owner=self.member2)
        note = self.note_in(folder)
        self.assert_recipients_are_the_readers(note, [self.member2])

    def test_a_personal_folder_or_no_folder_stays_owner_and_grants(self):
        mine = self.folder(scope=PersonalNoteFolder.SCOPE_PERSONAL)
        for note in (self.note_in(mine), self.note_in(None)):
            self.assertEqual(self.comment(note), set())
        shared = self.note_in(None)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.reader,
            note_type=1,
            note_id=shared.note_id,
            role_id=ROLE_OWNER,
        )
        self.assertEqual(self.comment(shared), {str(self.reader.id)})

    def test_someone_who_left_the_team_is_not_notified(self):
        folder = self.folder(visibility=PersonalNoteFolder.VISIBILITY_PRIVATE)
        NoteFolderPermission.objects.create(
            team=self.team, folder_id=folder.folder_id, user=self.reader, role_id=ROLE_OWNER
        )
        TeamMembers.objects.filter(team=self.team, attendee=self.reader).update(is_deleted=True)
        self.assertEqual(self.comment(self.note_in(folder)), set())
        self.assertFalse(Activity.objects.filter(recipient=self.reader).exists())
