"""Account deletion keeps the Team Notes the leaver wrote (MIP-1782).

`PersonalNoteMaster` and `PersonalNoteFolder` are ONE table each for two spaces: My Notes
(`scope="personal"`, private) and Team Notes (`scope="team"`, shared). Erasure used to delete
everything the leaver owned in both, so a teammate lost the note they read, and the folder
grants and tree under a deleted team folder broke. The rule under test:

* My Notes notes and folders of the leaver: deleted, as before.
* A Team Notes note, and the folder it sits in, that anyone else can still read: kept. The note
  keeps its `owner`, which now reads "Deleted user" (the account is anonymised, never removed),
  and its history shows the same name.
* A team folder with nobody else on it and nobody else's content in it is the leaver's private
  data and goes with them.
* The folder (the team's, the ACL carrier) gets a live owner: a co-owner already on it, else
  the team owner.
* A team that closes with the leaver has nobody to keep notes for: unchanged.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.personal_note_models import (
    PersonalNoteAttachmentFact,
    PersonalNoteFolder,
    PersonalNoteMaster,
)
from origin.models.note.version_note_models import NoteVersionMaster
from origin.services.account_deletion import DELETED_USERNAME, delete_account
from origin.services.account_export import build_export
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_folder_role import get_folder_role
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER, get_effective_role

User = get_user_model()

TEAM_META_URL = "/api/v2/note/team/meta/"
TEAM_FOLDER_URL = "/api/v2/note/team/folder/"

SCOPE_TEAM = PersonalNoteFolder.SCOPE_TEAM


class TeamNotesFixture(BaseAPITestCase):
    """`user2` is the leaver, `user` owns the team, `user3` is a third member."""

    def setUp(self):
        super().setUp()
        self.leaver = self.user2
        self.owner = self.user
        self.mate = User.objects.create_user(
            username="mate", email="mate@example.com", password="matepass123"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.mate)

    def folder(self, name, *, owner=None, visibility=None, parent=None, grants=()):
        owner = owner or self.leaver
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=owner,
            name=name,
            parent_folder_id=parent.folder_id if parent else None,
            scope=SCOPE_TEAM,
            visibility=visibility,
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=owner, role_id=ROLE_OWNER
        )
        for user, role in grants:
            NoteFolderPermission.objects.create(
                team=self.team, folder=folder, user=user, role_id=role
            )
        return folder

    def note(self, title, *, owner=None, folder=None, parent=None):
        return PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.leaver,
            title=title,
            body=[],
            folder_id=folder.folder_id if folder else None,
            parent_note_id=parent.note_id if parent else None,
        )

    def exists(self, note):
        return PersonalNoteMaster.objects.filter(note_id=note.note_id).exists()


class TeamNotesSurviveTests(TeamNotesFixture):
    def setUp(self):
        super().setUp()
        self.public = self.folder("Handbook", visibility="public")
        self.team_note = self.note("Runbook", folder=self.public)

    def test_a_team_note_the_leaver_wrote_survives_and_teammates_still_read_it(self):
        delete_account(self.leaver)

        self.assertTrue(self.exists(self.team_note))
        for reader in (self.owner, self.mate):
            self.assertIsNotNone(
                get_effective_role(reader.id, 1, self.team_note.note_id),
                f"{reader.username} lost the note",
            )

    def test_the_note_is_attributed_to_deleted_user(self):
        delete_account(self.leaver)
        self.team_note.refresh_from_db()
        # The account row stays, anonymised: that is the attribution (privacy page).
        self.assertEqual(self.team_note.owner.username, DELETED_USERNAME)

    def test_the_team_notes_list_names_deleted_user_as_author(self):
        delete_account(self.leaver)
        self.authenticate(self.mate)
        res = self.client.get(
            TEAM_META_URL, {"team_id": self.team.team_id, "user_id": str(self.mate.id)}
        )
        self.assertEqual(res.status_code, 200)
        rows = {n["title"]: n for n in res.data}
        self.assertEqual(rows["Runbook"]["ownerName"], DELETED_USERNAME)
        # And it is not the leaver's name in anyone's folder list either.
        res = self.client.get(
            TEAM_FOLDER_URL, {"team_id": self.team.team_id, "user_id": str(self.mate.id)}
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data)
        self.assertNotIn("otheruser", json.dumps(res.data, default=str))

    def test_the_leavers_my_notes_are_still_deleted(self):
        personal_folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.leaver, name="Mine", scope="personal"
        )
        loose = self.note("loose")
        filed = self.note("filed")
        PersonalNoteMaster.objects.filter(pk=filed.pk).update(folder_id=personal_folder.folder_id)
        child = self.note("child of mine", parent=loose)

        delete_account(self.leaver)

        for gone in (loose, filed, child):
            self.assertFalse(self.exists(gone), gone.title)
        self.assertFalse(PersonalNoteFolder.objects.filter(pk=personal_folder.pk).exists())
        self.assertTrue(self.exists(self.team_note))

    def test_teammates_notes_in_the_leavers_folder_stay_reachable(self):
        """The folder was hard-deleted with the leaver's, which broke the chain every
        teammate's note and subfolder resolved through."""
        theirs = self.note("Mate note", owner=self.mate, folder=self.public)
        sub = self.folder("Mate sub", owner=self.mate, parent=self.public)
        deeper = self.note("Deeper", owner=self.mate, folder=sub)

        delete_account(self.leaver)

        self.assertTrue(PersonalNoteFolder.objects.filter(pk=self.public.pk).exists())
        self.assertTrue(PersonalNoteFolder.objects.filter(pk=sub.pk).exists())
        for note in (theirs, deeper):
            self.assertIsNotNone(get_effective_role(self.owner.id, 1, note.note_id))
        self.assertIsNotNone(get_folder_role(self.owner.id, sub.folder_id))

    def test_a_teammates_child_under_the_leavers_note_keeps_its_parent(self):
        child = self.note("Mate child", owner=self.mate, parent=self.team_note)
        delete_account(self.leaver)
        self.assertTrue(self.exists(child))
        self.assertTrue(self.exists(self.team_note))

    def test_the_leavers_child_under_a_teammates_team_note_survives(self):
        """A child note has no folder of its own: it is a team note because its root is."""
        root = self.note("Mate root", owner=self.mate, folder=self.public)
        child = self.note("Leaver child", owner=self.leaver, parent=root)
        delete_account(self.leaver)
        self.assertTrue(self.exists(child))

    def test_other_peoples_grants_on_a_kept_note_stay_and_the_leavers_go(self):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.leaver,
            note_id=self.team_note.note_id,
            note_type=1,
            role_id=ROLE_OWNER,
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.mate,
            note_id=self.team_note.note_id,
            note_type=1,
            role_id=ROLE_EDITOR,
        )
        delete_account(self.leaver)
        rows = NotePermissionMaster.objects.filter(note_id=self.team_note.note_id, note_type=1)
        self.assertEqual({str(r.user_id) for r in rows}, {str(self.mate.id)})

    def test_what_teammates_hold_about_a_kept_note_stays(self):
        NoteFavoriteMaster.objects.create(
            team=self.team, user=self.mate, note_type=1, note_id=self.team_note.note_id
        )
        delete_account(self.leaver)
        self.assertTrue(
            NoteFavoriteMaster.objects.filter(
                user=self.mate, note_type=1, note_id=self.team_note.note_id
            ).exists()
        )

    def test_the_files_inside_a_kept_note_stay(self):
        row = PersonalNoteAttachmentFact.objects.create(
            note=self.team_note, uploader=self.leaver, note_attachment_url="notes/x/pic.png"
        )
        gone = self.note("mine")
        gone_row = PersonalNoteAttachmentFact.objects.create(
            note=gone, uploader=self.leaver, note_attachment_url="notes/y/mine.png"
        )
        delete_account(self.leaver)
        self.assertTrue(PersonalNoteAttachmentFact.objects.filter(pk=row.pk).exists())
        self.assertFalse(PersonalNoteAttachmentFact.objects.filter(pk=gone_row.pk).exists())


class VersionHistoryTests(TeamNotesFixture):
    def setUp(self):
        super().setUp()
        self.public = self.folder("Handbook", visibility="public")
        self.team_note = self.note("Runbook", folder=self.public)

    def _version(self, note, no, editor, via=None):
        return NoteVersionMaster.objects.create(
            team=self.team,
            note_type=1,
            note_id=note.note_id,
            version_no=no,
            editor=editor,
            title=note.title,
            body=[],
            via=via,
        )

    def test_history_of_a_kept_note_stays_and_names_deleted_user(self):
        mine = self._version(
            self.team_note, 1, self.leaver, via={"kind": "api_key", "key_name": "my laptop"}
        )
        theirs = self._version(self.team_note, 2, self.mate)
        delete_account(self.leaver)

        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual(mine.editor.username, DELETED_USERNAME)
        # The key's name is the leaver's own label: it goes, the version stays.
        self.assertIsNone(mine.via)
        self.assertEqual(theirs.editor_id, self.mate.id)

    def test_history_of_a_deleted_note_goes_with_it(self):
        loose = self.note("loose")
        self._version(loose, 1, self.leaver)
        delete_account(self.leaver)
        self.assertFalse(NoteVersionMaster.objects.filter(note_id=loose.note_id).exists())


class FolderOwnershipTests(TeamNotesFixture):
    def test_a_co_owner_already_on_the_folder_takes_it_over(self):
        public = self.folder("Handbook", visibility="public", grants=[(self.mate, ROLE_OWNER)])
        delete_account(self.leaver)

        public.refresh_from_db()
        self.assertEqual(str(public.owner_id), str(self.mate.id))
        self.assertEqual(get_folder_role(self.mate.id, public.folder_id), ROLE_OWNER)
        self.assertFalse(
            NoteFolderPermission.objects.filter(folder=public, user=self.leaver).exists()
        )

    def test_without_a_co_owner_the_team_owner_takes_it_over(self):
        public = self.folder("Handbook", visibility="public", grants=[(self.mate, ROLE_EDITOR)])
        delete_account(self.leaver)

        public.refresh_from_db()
        self.assertEqual(str(public.owner_id), str(self.owner.id))
        self.assertEqual(get_folder_role(self.owner.id, public.folder_id), ROLE_OWNER)
        # The editor keeps exactly what they had.
        self.assertEqual(get_folder_role(self.mate.id, public.folder_id), ROLE_EDITOR)
        self.assertFalse(
            NoteFolderPermission.objects.filter(folder=public, user=self.leaver).exists()
        )

    def test_the_hand_over_is_in_the_audit_trail(self):
        public = self.folder("Handbook", visibility="public")
        delete_account(self.leaver)
        event = SecurityEvent.objects.filter(
            action=SecurityAction.OWNER_TRANSFERRED, target_id=str(public.folder_id)
        ).get()
        self.assertEqual(event.target_type, "note_folder")
        self.assertEqual(event.metadata["via"], "account_deleted")

    def test_the_leavers_grants_on_other_peoples_folders_go(self):
        theirs = self.folder(
            "Mate folder", owner=self.mate, visibility="public", grants=[(self.leaver, ROLE_EDITOR)]
        )
        delete_account(self.leaver)
        self.assertFalse(
            NoteFolderPermission.objects.filter(folder=theirs, user=self.leaver).exists()
        )
        self.assertTrue(PersonalNoteFolder.objects.filter(pk=theirs.pk).exists())

    def test_a_private_folder_with_nobody_else_on_it_is_the_leavers_and_goes(self):
        private = self.folder("My drafts", visibility="private")
        inside = self.note("draft", folder=private)
        delete_account(self.leaver)
        self.assertFalse(PersonalNoteFolder.objects.filter(pk=private.pk).exists())
        self.assertFalse(self.exists(inside))

    def test_a_private_folder_shared_with_a_teammate_stays(self):
        private = self.folder("Shared", visibility="private", grants=[(self.mate, ROLE_EDITOR)])
        inside = self.note("shared note", folder=private)
        delete_account(self.leaver)
        self.assertTrue(PersonalNoteFolder.objects.filter(pk=private.pk).exists())
        self.assertTrue(self.exists(inside))
        self.assertIsNotNone(get_effective_role(self.mate.id, 1, inside.note_id))

    def test_a_private_subfolder_keeps_its_ancestors(self):
        """Deleting the private parent would break the chain under the kept child."""
        parent = self.folder("Parent", visibility="private")
        child = self.folder(
            "Child", visibility="private", parent=parent, grants=[(self.mate, ROLE_EDITOR)]
        )
        delete_account(self.leaver)
        self.assertTrue(PersonalNoteFolder.objects.filter(pk=parent.pk).exists())
        self.assertTrue(PersonalNoteFolder.objects.filter(pk=child.pk).exists())

    def test_foreign_content_keeps_a_private_folder(self):
        private = self.folder("Mixed", visibility="private")
        theirs = self.note("Mate note", owner=self.mate, folder=private)
        delete_account(self.leaver)
        self.assertTrue(PersonalNoteFolder.objects.filter(pk=private.pk).exists())
        self.assertTrue(self.exists(theirs))


class ClosingTeamTests(TeamNotesFixture):
    def test_a_team_that_closes_with_the_leaver_keeps_nothing(self):
        solo = TeamMaster.objects.create(
            team_name="Solo", team_email="solo@example.com", owner=self.leaver
        )
        TeamMembers.objects.create(team=solo, attendee=self.leaver)
        folder = PersonalNoteFolder.objects.create(
            team=solo, owner=self.leaver, name="F", scope=SCOPE_TEAM, visibility="public"
        )
        note = PersonalNoteMaster.objects.create(
            team=solo, owner=self.leaver, title="n", body=[], folder_id=folder.folder_id
        )
        # Leave the shared team first, so the leaver solely owns only the solo team.
        TeamMembers.objects.filter(team=self.team, attendee=self.leaver).update(is_deleted=True)

        delete_account(self.leaver)

        self.assertFalse(PersonalNoteMaster.objects.filter(pk=note.pk).exists())
        self.assertFalse(PersonalNoteFolder.objects.filter(pk=folder.pk).exists())


class SearchAndAtomicityTests(TeamNotesFixture):
    def setUp(self):
        super().setUp()
        self.public = self.folder("Handbook", visibility="public")
        self.kept = self.note("Runbook", folder=self.public)
        self.gone = self.note("mine")

    def test_search_drops_only_the_deleted_notes(self):
        with (
            mock.patch("origin.services.account_deletion.purge_notes_from_index") as purge,
            mock.patch("origin.search_engine.purge.purge_note_folders"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            delete_account(self.leaver)
        purged = {int(n) for call in purge.call_args_list for n in call.args[0]}
        self.assertEqual(purged, {self.gone.note_id})

    def test_a_failure_rolls_everything_back_and_touches_no_index(self):
        with (
            mock.patch("origin.services.account_deletion.purge_notes_from_index") as purge,
            mock.patch("origin.search_engine.purge.purge_note_folders"),
            mock.patch.object(type(self.leaver), "save", side_effect=RuntimeError("database down")),
            self.captureOnCommitCallbacks(execute=True),
        ):
            with self.assertRaises(RuntimeError):
                delete_account(self.leaver)

        purge.assert_not_called()
        self.public.refresh_from_db()
        self.assertEqual(str(self.public.owner_id), str(self.leaver.id))
        self.assertTrue(
            NoteFolderPermission.objects.filter(folder=self.public, user=self.leaver).exists()
        )
        self.assertTrue(self.exists(self.kept))
        self.assertTrue(self.exists(self.gone))
        self.assertFalse(
            SecurityEvent.objects.filter(action=SecurityAction.OWNER_TRANSFERRED).exists()
        )


class ExportMatchesDeletionTests(TeamNotesFixture):
    def test_export_labels_each_note_with_the_space_deletion_uses(self):
        public = self.folder("Handbook", visibility="public")
        self.note("Runbook", folder=public)
        self.note("mine")

        notes = {n["title"]: n for n in build_export(self.leaver)["personal_notes"]}

        self.assertEqual(notes["mine"]["space"], "personal")
        self.assertEqual(notes["Runbook"]["space"], "team")
        self.assertEqual(notes["Runbook"]["folder"], "Handbook")
