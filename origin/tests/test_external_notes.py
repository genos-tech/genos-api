"""Shared note folders across teams.

Like Phase 3, the claim under test is mostly that nothing new was needed:
an external participant is an ordinary `NoteFolderPermission` row, so
`readable_team_folder_roles`, `get_effective_role` (which the Hocuspocus
collab server consults) and the note-create gate should already treat them
correctly. The folder listing in particular checks explicit grants BEFORE
it checks team membership, which is why a non-member with a row can see the
folder at all.

Two things ARE new here, and they are the interesting cases:

* **The folder must be, and stay, explicitly private.** Public means "every
  member of the host team is an editor" plus a `team:<id>` search sentinel —
  a second, invisible definition of who is in a space the host is lending
  out. Inherited visibility is refused for a sharper reason: an ancestor
  could redefine the share after the fact.
* **An external editor may write notes but not administer the folder.**
  Before this feature every folder editor was a teammate, so the roster and
  settings handlers only ever asked "can you write here". Left alone, that
  would have let an outsider rename the host's folder, add host members to
  it, and evict the host's own people from it.
"""

from origin.models.common.team_models import ExternalGrant, ShareStatus
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.services.external_grants import (
    ExternalGrantError,
    add_external_participants,
    offer_grant,
    revoke_grant,
)
from origin.services.member_roles import VIEWER
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.views.utils.note_folder_role import get_folder_role
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_VIEWER, get_effective_role

FOLDERS = "/api/v2/note/team/folder/"
FOLDER_MEMBERS = "/api/v2/note/team/folder/member/"
TEAM_NOTE_META = "/api/v2/note/team/meta/"
NOTES = "/api/v2/note/personal/"
NOTE_TYPE_PERSONAL = 1


class FolderShareabilityTests(CrossTeamTestCase):
    """What may be offered at all."""

    def _offer(self, folder):
        return offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.NOTE_FOLDER,
            object_id=folder.folder_id,
            role_ceiling=VIEWER,
            actor=self.a_owner,
        )

    def _folder(self, **kwargs):
        return PersonalNoteFolder.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            name=kwargs.pop("name", "Folder"),
            scope=PersonalNoteFolder.SCOPE_TEAM,
            **kwargs,
        )

    def test_a_private_folder_can_be_offered(self):
        self.connect_a_and_b()
        grant = self._offer(self.folder)
        self.assertEqual(grant.status, ShareStatus.PENDING)

    def test_a_public_folder_cannot_be_offered(self):
        self.connect_a_and_b()
        public = self._folder(name="Handbook", visibility=PersonalNoteFolder.VISIBILITY_PUBLIC)
        with self.assertRaises(ExternalGrantError) as caught:
            self._offer(public)
        self.assertEqual(caught.exception.code, "folder_not_private")

    def test_an_inheriting_subfolder_cannot_be_offered(self):
        """Its meaning could change from a folder the host wasn't looking at."""
        self.connect_a_and_b()
        child = self._folder(name="Child", parent_folder_id=self.folder.folder_id)
        with self.assertRaises(ExternalGrantError) as caught:
            self._offer(child)
        self.assertEqual(caught.exception.code, "folder_not_private")

    def test_a_personal_folder_cannot_be_offered(self):
        """No ACL carrier exists for a personal folder — it is owner-only."""
        self.connect_a_and_b()
        personal = PersonalNoteFolder.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            name="Mine",
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE,
        )
        with self.assertRaises(ExternalGrantError) as caught:
            self._offer(personal)
        self.assertEqual(caught.exception.code, "bad_object")


class ExternalFolderAccessTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.grant = self.active_folder_grant()
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        self.other = PersonalNoteFolder.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            name="Internal",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE,
        )

    def test_admission_writes_a_permission_row_naming_the_grant(self):
        """Provenance, so the cascades can find the row without guessing."""
        row = NoteFolderPermission.objects.get(folder=self.folder, user=self.b_viewer)
        self.assertEqual(row.via_group_type, "external_grant")
        self.assertEqual(row.via_group_id, str(self.grant.id))
        self.assertEqual(str(row.team_id), str(self.team_a.team_id))

    def test_the_ceiling_decides_the_folder_role(self):
        add_external_participants(self.grant, [self.b_editor.id], self.b_owner, role=VIEWER)
        self.assertEqual(get_folder_role(self.b_editor.id, self.folder.folder_id), ROLE_VIEWER)

    def test_they_see_the_shared_folder_and_nothing_else(self):
        self.authenticate(self.b_viewer)
        res = self.client.get(
            FOLDERS,
            {"team_id": str(self.team_a.team_id), "user_id": str(self.b_viewer.id)},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual([f["folderId"] for f in res.data], [self.folder.folder_id])

    def test_a_colleague_who_was_never_admitted_sees_nothing(self):
        """Their team holds the grant. That is not access."""
        self.authenticate(self.b_editor)
        res = self.client.get(
            FOLDERS,
            {"team_id": str(self.team_a.team_id), "user_id": str(self.b_editor.id)},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, [])

    def test_they_see_notes_filed_in_the_shared_folder(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            folder_id=self.folder.folder_id,
            title="Kickoff",
            body="",
        )
        self.authenticate(self.b_viewer)
        res = self.client.get(
            TEAM_NOTE_META,
            {"team_id": str(self.team_a.team_id), "user_id": str(self.b_viewer.id)},
        )
        self.assertEqual([n["noteId"] for n in res.data], [note.note_id])

    def test_a_note_in_the_folder_resolves_their_role_for_the_collab_gate(self):
        """`get_effective_role` is what Hocuspocus asks before opening a doc."""
        note = PersonalNoteMaster.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            folder_id=self.folder.folder_id,
            title="Kickoff",
            body="",
        )
        self.assertEqual(
            get_effective_role(self.b_viewer.id, NOTE_TYPE_PERSONAL, note.note_id),
            ROLE_EDITOR,
        )

    def test_an_editor_participant_can_write_a_note_in_the_folder(self):
        """The requirement in one test: both teams keep notes in the folder."""
        self.authenticate(self.b_viewer)
        res = self.client.post(
            NOTES,
            {
                "team_id": str(self.team_a.team_id),
                "user_id": str(self.b_viewer.id),
                "title": "From the other side",
                "body": "",
                "folder_id": self.folder.folder_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertTrue(
            PersonalNoteMaster.objects.filter(
                folder_id=self.folder.folder_id, owner=self.b_viewer
            ).exists()
        )

    def test_a_viewer_participant_cannot_write_a_note_in_the_folder(self):
        grant = self.active_folder_grant(role_ceiling=VIEWER, folder=self.other)
        add_external_participants(grant, [self.b_editor.id], self.b_owner)
        self.authenticate(self.b_editor)
        res = self.client.post(
            NOTES,
            {
                "team_id": str(self.team_a.team_id),
                "user_id": str(self.b_editor.id),
                "title": "Should not land",
                "body": "",
                "folder_id": self.other.folder_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_revoking_removes_the_row_and_the_folder_from_their_list(self):
        revoke_grant(self.grant, self.a_owner)
        self.assertFalse(
            NoteFolderPermission.objects.filter(folder=self.folder, user=self.b_viewer).exists()
        )
        self.authenticate(self.b_viewer)
        res = self.client.get(
            FOLDERS,
            {"team_id": str(self.team_a.team_id), "user_id": str(self.b_viewer.id)},
        )
        self.assertEqual(res.data, [])


class SharedFolderStaysPrivateTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.grant = self.active_folder_grant()
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)
        self.authenticate(self.a_owner)

    def _patch(self, **body):
        return self.client.put(
            FOLDERS,
            {
                "team_id": str(self.team_a.team_id),
                "user_id": str(self.a_owner.id),
                "folder_id": self.folder.folder_id,
                **body,
            },
            format="json",
        )

    def test_it_cannot_be_made_public_while_shared(self):
        res = self._patch(visibility=PersonalNoteFolder.VISIBILITY_PUBLIC)
        self.assertEqual(res.status_code, 400)
        self.folder.refresh_from_db()
        self.assertEqual(self.folder.visibility, PersonalNoteFolder.VISIBILITY_PRIVATE)

    def test_it_can_be_made_public_once_the_share_ends(self):
        """The rail is about live shares, not about the folder forever."""
        revoke_grant(self.grant, self.a_owner)
        res = self._patch(visibility=PersonalNoteFolder.VISIBILITY_PUBLIC)
        self.assertEqual(res.status_code, 200)

    def test_a_pending_share_is_enough_to_hold_it_private(self):
        """The host has already committed; the guest just hasn't answered."""
        second = PersonalNoteFolder.objects.create(
            team=self.team_a,
            owner=self.a_owner,
            name="Offered",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE,
        )
        offer_grant(
            owner_team_id=self.team_a.team_id,
            guest_team_id=self.team_b.team_id,
            object_type=ExternalGrant.ObjectType.NOTE_FOLDER,
            object_id=second.folder_id,
            role_ceiling=VIEWER,
            actor=self.a_owner,
        )
        res = self.client.put(
            FOLDERS,
            {
                "team_id": str(self.team_a.team_id),
                "user_id": str(self.a_owner.id),
                "folder_id": second.folder_id,
                "visibility": PersonalNoteFolder.VISIBILITY_PUBLIC,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_renaming_is_still_allowed(self):
        """The rail is about visibility, not a general freeze."""
        res = self._patch(name="Renamed")
        self.assertEqual(res.status_code, 200)


class ExternalsCannotAdministerTheFolderTests(CrossTeamTestCase):
    """An external EDITOR writes notes; the folder itself is not theirs."""

    def setUp(self):
        super().setUp()
        self.grant = self.active_folder_grant()
        add_external_participants(self.grant, [self.b_viewer.id], self.b_owner)

    def test_they_cannot_rename_the_hosts_folder(self):
        self.authenticate(self.b_viewer)
        res = self.client.put(
            FOLDERS,
            {
                "team_id": str(self.team_a.team_id),
                "user_id": str(self.b_viewer.id),
                "folder_id": self.folder.folder_id,
                "name": "Ours now",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.folder.refresh_from_db()
        self.assertEqual(self.folder.name, "Shared Folder")

    def test_they_cannot_add_host_members_to_it(self):
        """`_grant_members` would accept these ids — the actor is the problem."""
        self.authenticate(self.b_viewer)
        res = self.client.post(
            FOLDER_MEMBERS,
            {
                "team_id": str(self.team_a.team_id),
                "folder_id": self.folder.folder_id,
                "user_ids": [str(self.a_viewer.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(
            NoteFolderPermission.objects.filter(folder=self.folder, user=self.a_viewer).exists()
        )

    def test_they_cannot_evict_a_host_member(self):
        NoteFolderPermission.objects.create(
            team=self.team_a,
            folder=self.folder,
            user=self.a_viewer,
            role_id=ROLE_EDITOR,
        )
        self.authenticate(self.b_viewer)
        # Query string, not a body: this handler reads `request.GET`, and a
        # DELETE body would arrive as nothing at all and 400 on validation.
        res = self.client.delete(
            f"{FOLDER_MEMBERS}?team_id={self.team_a.team_id}"
            f"&folder_id={self.folder.folder_id}"
            f"&target_user_id={self.a_viewer.id}"
        )
        self.assertEqual(res.status_code, 403)
        self.assertTrue(
            NoteFolderPermission.objects.filter(folder=self.folder, user=self.a_viewer).exists()
        )

    def test_a_host_manager_still_can(self):
        """The control: the guard must not have closed the host's own path."""
        self.authenticate(self.a_owner)
        res = self.client.post(
            FOLDER_MEMBERS,
            {
                "team_id": str(self.team_a.team_id),
                "folder_id": self.folder.folder_id,
                "user_ids": [str(self.a_viewer.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(
            NoteFolderPermission.objects.filter(folder=self.folder, user=self.a_viewer).exists()
        )

    def test_the_host_cannot_invite_the_guest_teams_people_directly(self):
        """Only the guest team staffs its own side — invariant 5.

        `_grant_members` silently drops non-members rather than erroring,
        which is why this asserts on the row and not the status code.
        """
        self.authenticate(self.a_owner)
        res = self.client.post(
            FOLDER_MEMBERS,
            {
                "team_id": str(self.team_a.team_id),
                "folder_id": self.folder.folder_id,
                "user_ids": [str(self.b_editor.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(
            NoteFolderPermission.objects.filter(folder=self.folder, user=self.b_editor).exists()
        )

    def test_the_host_keeps_its_veto_over_an_individual(self):
        """Ejecting one person must not require ending the whole share."""
        from origin.services.external_grants import remove_external_participants

        removed = remove_external_participants(self.grant, [self.b_viewer.id], self.a_owner)
        self.assertEqual(removed, 1)
        self.grant.refresh_from_db()
        self.assertEqual(self.grant.status, ShareStatus.ACTIVE)
