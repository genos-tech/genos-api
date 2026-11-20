"""MIP-1713: `purge_orphaned_team_rows` also removes My Notes notes, folders and recents whose team
AND owner are gone.

"team IS NULL" alone is not the rule: a new account's "Start here" note has no team on purpose
(`start_here_note.create_for`). An orphan is made the way the database makes one: a row that had
a team and an owner, then both go (SET_NULL), here with `.update` and once with the real deletes.
OpenSearch is never touched: `purge.os_helpers.bulk` is patched, the `RagChunk` bookkeeping is real.
"""

import shutil
import tempfile
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.mention_group_models import MentionGroupMaster
from origin.models.common.team_models import TeamMaster
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.personal_note_models import (
    PersonalNoteAttachmentFact,
    PersonalNoteFolder,
    PersonalNoteMaster,
)
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine import purge
from origin.search_engine.models import NoteSummary, RagChunk
from origin.services import orphaned_team_rows, start_here_note
from origin.services.orphaned_team_rows import purge_orphaned_team_rows
from origin.views.utils.note_version import snapshot_note_version

User = get_user_model()
BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "private words"}]}]


class PurgeOrphanedMyNotesTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media = tempfile.mkdtemp(prefix="mip1713-media-")
        cls._override = override_settings(MEDIA_ROOT=cls._media)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        shutil.rmtree(cls._media, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner1713", email="owner1713@example.com", password="x"
        )
        self.other = User.objects.create_user(
            username="other1713", email="other1713@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="Live", team_email="live1713@example.com", owner=self.other
        )
        self.gone = TeamMaster.objects.create(
            team_name="Gone", team_email="gone1713@example.com", owner=self.other
        )
        self.bulk = mock.MagicMock(return_value=(0, []))
        for patcher in (
            mock.patch.object(purge.os_helpers, "bulk", self.bulk),
            mock.patch.object(purge, "get_client", return_value=mock.MagicMock()),
            mock.patch.object(purge, "get_index_alias", return_value="test-alias"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- helpers -------------------------------------------------------------------------

    def _note(self, *, team, owner, title="Private", folder=None, parent=None, grant=True):
        """A note with everything the product hangs off one: its owner grant, a version, a
        summary, a search chunk, and what another person holds about it."""
        note = PersonalNoteMaster.objects.create(
            team=team,
            owner=owner,
            title=title,
            body=BODY,
            folder_id=folder.folder_id if folder else None,
            parent_note_id=parent.note_id if parent else None,
        )
        nid = note.note_id
        if grant and owner is not None:
            NotePermissionMaster.objects.create(
                team=team, user=owner, note_id=nid, note_type=1, role_id=1
            )
        snapshot_note_version(
            team=team, editor=owner, note_type=1, note_id=nid, title=title, body=BODY
        )
        NoteSummary.objects.create(
            team_id=str(self.gone.team_id), note_type=1, note_id=nid, summary_text="a summary"
        )
        RagChunk.objects.create(
            chunk_id=f"note:personal:{nid}:body",
            entity_type="note",
            entity_id=f"note:personal:{nid}",
            chunk_type="note_body",
            team_id=self.gone.team_id,
            text_hash="h",
            embedding_model="m",
        )
        RagChunk.objects.create(
            chunk_id=f"note_summary:1:{nid}:s",
            entity_type="note_summary",
            entity_id=f"note_summary:1:{nid}",
            chunk_type="summary",
            team_id=self.gone.team_id,
            text_hash="h",
            embedding_model="m",
        )
        NoteRecentMaster.objects.create(team=self.team, user=self.other, note_type=1, note_id=nid)
        NoteFavoriteMaster.objects.create(team=self.team, user=self.other, note_type=1, note_id=nid)
        Activity.objects.create(
            team=self.team,
            recipient=self.other,
            actor=self.other,
            activity_type=ActivityType.MENTION,
            surface_type=6,
            meta={"noteId": nid},
        )
        return note

    def _folder(self, *, team, owner, name="Guide", scope="personal", parent=None):
        return PersonalNoteFolder.objects.create(
            team=team,
            owner=owner,
            name=name,
            scope=scope,
            parent_folder_id=parent.folder_id if parent else None,
        )

    def _orphan(self, *rows):
        """What deleting the team and then the owner did to a row: SET_NULL, nothing else."""
        for row in rows:
            fields = {"team": None}
            if hasattr(row, "owner"):
                fields["owner"] = None
            type(row).objects.filter(pk=row.pk).update(**fields)
            if isinstance(row, PersonalNoteMaster):
                # The same two deletes null the rows that point at the note's people and team:
                # the owner's grant, and the versions the owner wrote (their FKs are SET_NULL too).
                NotePermissionMaster.objects.filter(note_type=1, note_id=row.note_id).update(
                    team=None, user=None
                )
                NoteVersionMaster.objects.filter(note_type=1, note_id=row.note_id).update(
                    team=None, editor=None
                )
            row.refresh_from_db()
        return rows[0] if len(rows) == 1 else rows

    def _traces(self, nid):
        return {
            "perm": NotePermissionMaster.objects.filter(note_type=1, note_id=nid).count(),
            "version": NoteVersionMaster.objects.filter(note_type=1, note_id=nid).count(),
            "summary": NoteSummary.objects.filter(note_type=1, note_id=nid).count(),
            "recent": NoteRecentMaster.objects.filter(note_type=1, note_id=nid).count(),
            "favorite": NoteFavoriteMaster.objects.filter(note_type=1, note_id=nid).count(),
            "activity": Activity.objects.filter(surface_type=6, meta__noteId=nid).count(),
            "chunks": RagChunk.objects.filter(
                entity_id__in=[f"note:personal:{nid}", f"note_summary:1:{nid}"]
            ).count(),
        }

    def _run(self, *args):
        out = StringIO()
        call_command("purge_orphaned_team_rows", *args, stdout=out)
        return out.getvalue()

    def _bulk_deleted_ids(self):
        ids = set()
        for call in self.bulk.call_args_list:
            for action in call.args[1]:
                self.assertEqual(action["_op_type"], "delete")
                ids.add(action["_id"])
        return ids

    # --- the rule ------------------------------------------------------------------------

    def test_an_orphan_note_goes_with_every_satellite(self):
        note = self._orphan(self._note(team=self.gone, owner=self.owner))
        nid = note.note_id
        self.assertTrue(all(self._traces(nid).values()), self._traces(nid))

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.personal_notes, 1)
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=nid).exists())
        self.assertEqual(self._traces(nid), dict.fromkeys(self._traces(nid), 0))

    def test_the_search_chunks_of_an_orphan_note_are_purged(self):
        note = self._orphan(self._note(team=self.gone, owner=self.owner))
        kept = self._note(team=self.team, owner=self.owner, title="Kept")
        nid = note.note_id

        purge_orphaned_team_rows(apply=True)

        self.assertEqual(
            self._bulk_deleted_ids(), {f"note:personal:{nid}:body", f"note_summary:1:{nid}:s"}
        )
        self.assertEqual(self._traces(nid)["chunks"], 0)
        self.assertEqual(self._traces(kept.note_id)["chunks"], 2)

    def test_a_note_with_a_team_stays_even_without_an_owner(self):
        """The owner is gone but the team is alive: it is the team's note, not this command's."""
        note = self._note(team=self.team, owner=self.owner)
        PersonalNoteMaster.objects.filter(pk=note.pk).update(owner=None)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.personal_notes, 0)
        self.assertEqual(report.notes_kept_reachable, 0)
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())
        self.assertTrue(all(self._traces(note.note_id).values()))

    def test_a_note_with_no_team_but_a_live_owner_stays(self):
        """A team-less personal note is legitimate: it is a new account's "Start here" note."""
        start = start_here_note._create(self.owner, "en")
        self.assertIsNotNone(start)
        self.assertIsNone(start.team_id)
        mine = self._note(team=self.gone, owner=self.owner, title="Mine")
        PersonalNoteMaster.objects.filter(pk=mine.pk).update(team=None)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.personal_notes, 0)
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=start.note_id).exists())
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=mine.note_id).exists())
        self.assertTrue(
            NotePermissionMaster.objects.filter(note_type=1, note_id=start.note_id).exists()
        )
        self.assertTrue(all(self._traces(mine.note_id).values()))

    def test_a_note_with_no_team_and_a_live_owner_stays_even_without_a_grant_row(self):
        """The owner rule stands on its own: it must not lean on the owner's grant row."""
        note = self._note(team=self.gone, owner=self.owner, grant=False)
        PersonalNoteMaster.objects.filter(pk=note.pk).update(team=None)
        self.assertFalse(NotePermissionMaster.objects.filter(note_id=note.note_id).exists())

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual((report.personal_notes, report.notes_kept_reachable), (0, 0))
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())

    def test_the_demo_teardown_leaves_no_note_folder_or_recent_for_this_command(self):
        """Why no NEW ones from the demo path: the teardown (with the MIP-1702 sweep) removes the
        guide notes and folders a team joiner gets, and the recents, before the team and the user
        go. Pinned here so the purge has nothing to find after one."""
        from origin.services.demo_seeder import delete_demo_team_data  # noqa: PLC0415
        from origin.services.guide_notes import seed_guide_notes  # noqa: PLC0415

        self.assertTrue(seed_guide_notes(self.owner, self.gone))
        note = PersonalNoteMaster.objects.filter(team=self.gone).first()
        NoteRecentMaster.objects.create(
            team=self.gone, user=self.owner, note_type=1, note_id=note.note_id
        )
        NoteRecentMaster.objects.create(
            team=self.gone, user=self.other, note_type=1, note_id=note.note_id
        )

        delete_demo_team_data(self.gone.team_id)
        self.owner.delete()

        self.assertEqual(PersonalNoteMaster.objects.filter(team__isnull=True).count(), 0)
        self.assertEqual(PersonalNoteFolder.objects.filter(team__isnull=True).count(), 0)
        self.assertEqual(NoteRecentMaster.objects.filter(team__isnull=True).count(), 0)
        self.assertEqual(purge_orphaned_team_rows(apply=False).total, 0)

    def test_what_a_note_costs_to_delete_does_not_grow_with_the_notes(self):
        """The satellite rows go in one pass (`batched_note_cleanup`), not three DELETEs a note."""

        def cost(count):
            notes = [
                self._orphan(self._note(team=self.gone, owner=self.owner)) for _ in range(count)
            ]
            self.assertEqual(len(notes), count)
            with mock.patch.object(orphaned_team_rows, "purge_notes_from_index"):
                with CaptureQueriesContext(connection) as queries:
                    purge_orphaned_team_rows(apply=True)
            return len(queries)

        self.assertEqual(cost(2), cost(6))

    def test_an_ownerless_note_somebody_can_still_open_is_kept_and_counted(self):
        shared = self._orphan(self._note(team=self.gone, owner=self.owner, title="Shared"))
        NotePermissionMaster.objects.create(
            team=None, user=self.other, note_id=shared.note_id, note_type=1, role_id=3
        )
        team_folder = self._folder(team=self.team, owner=self.other, scope="team", name="Team")
        filed = self._note(team=self.gone, owner=self.owner, title="Filed", folder=team_folder)
        self._orphan(filed)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.personal_notes, 0)
        self.assertEqual(report.notes_kept_reachable, 2)
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=shared.note_id).exists())
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=filed.note_id).exists())
        self.assertIn("note(s) with no team and no owner KEPT", self._run())

    def test_the_children_of_an_orphan_note_go_with_it(self):
        root = self._note(team=self.gone, owner=self.owner, title="Root")
        child = self._note(team=self.gone, owner=self.owner, title="Child", parent=root)
        self._orphan(root, child)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.personal_notes, 2)
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=child.note_id).exists())

    def test_made_by_the_real_deletes_not_by_update(self):
        """Delete the team, then the owner, as a hand-run cleanup would."""
        note = self._note(team=self.gone, owner=self.owner)
        folder = self._folder(team=self.gone, owner=self.owner)
        TeamMaster.objects.filter(pk=self.gone.pk).delete()
        self.owner.delete()
        note.refresh_from_db()
        folder.refresh_from_db()
        self.assertIsNone(note.team_id)
        self.assertIsNone(note.owner_id)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual((report.personal_notes, report.personal_folders), (1, 1))
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())
        self.assertEqual(self._traces(note.note_id), dict.fromkeys(self._traces(note.note_id), 0))

    # --- folders -------------------------------------------------------------------------

    def test_an_orphan_folder_goes_and_a_live_one_stays(self):
        gone = self._orphan(self._folder(team=self.gone, owner=self.owner))
        live = self._folder(team=self.team, owner=self.other, name="Live")
        NoteRecentMaster.objects.all().delete()

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.personal_folders, 1)
        self.assertFalse(PersonalNoteFolder.objects.filter(folder_id=gone.folder_id).exists())
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=live.folder_id).exists())

    def test_a_folder_goes_with_its_notes_and_its_subfolders(self):
        top = self._folder(team=self.gone, owner=self.owner, name="Guide")
        sub = self._folder(team=self.gone, owner=self.owner, name="Sub", parent=top)
        note = self._note(team=self.gone, owner=self.owner, folder=sub)
        self._orphan(top, sub, note)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual((report.personal_notes, report.personal_folders), (1, 2))
        self.assertEqual(PersonalNoteFolder.objects.count(), 0)

    def test_the_search_chunks_of_an_orphan_folder_are_purged(self):
        folder = self._orphan(self._folder(team=self.gone, owner=self.owner))
        RagChunk.objects.create(
            chunk_id=f"note_folder:{folder.folder_id}:c",
            entity_type="note_folder",
            entity_id=f"note_folder:{folder.folder_id}",
            chunk_type="note_folder",
            team_id=self.gone.team_id,
            text_hash="h",
            embedding_model="m",
        )

        purge_orphaned_team_rows(apply=True)

        self.assertEqual(self._bulk_deleted_ids(), {f"note_folder:{folder.folder_id}:c"})
        self.assertFalse(RagChunk.objects.filter(entity_type="note_folder").exists())

    def test_a_team_scope_folder_is_never_an_orphan_folder(self):
        folder = self._orphan(
            self._folder(team=self.gone, owner=self.owner, scope="team", name="Team")
        )

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.personal_folders, 0)
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).exists())
        self.assertIn("origin.PersonalNoteFolder.team: 1", "\n".join(self._run().splitlines()))

    def test_a_folder_that_still_holds_a_note_we_keep_is_kept_with_its_parents(self):
        top = self._folder(team=self.gone, owner=self.owner, name="Top")
        sub = self._folder(team=self.gone, owner=self.owner, name="Sub", parent=top)
        # a note with a live owner (so it is kept) filed in the orphan folder
        keeper = self._note(team=self.gone, owner=self.other, folder=sub)
        PersonalNoteMaster.objects.filter(pk=keeper.pk).update(team=None)
        self._orphan(top, sub)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.personal_folders, 0)
        self.assertEqual(report.folders_kept_in_use, 2)
        self.assertEqual(PersonalNoteFolder.objects.count(), 2)

    def test_an_orphan_folder_with_a_surviving_subfolder_is_kept(self):
        top = self._folder(team=self.gone, owner=self.owner, name="Top")
        sub = self._folder(team=self.team, owner=self.other, name="Sub", parent=top)
        self._orphan(top)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual((report.personal_folders, report.folders_kept_in_use), (0, 1))
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=top.folder_id).exists())
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=sub.folder_id).exists())

    def test_a_folder_somebody_holds_a_grant_on_is_kept(self):
        folder = self._orphan(self._folder(team=self.gone, owner=self.owner))
        NoteFolderPermission.objects.create(team=None, folder=folder, user=self.other, role_id=3)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual((report.personal_folders, report.folders_kept_in_use), (0, 1))

    # --- recents -------------------------------------------------------------------------

    def test_only_a_recent_with_no_team_and_no_user_goes(self):
        both = NoteRecentMaster.objects.create(
            team=self.gone, user=self.owner, note_type=1, note_id=900001
        )
        no_user = NoteRecentMaster.objects.create(
            team=self.team, user=self.owner, note_type=1, note_id=900002
        )
        no_team = NoteRecentMaster.objects.create(
            team=self.gone, user=self.other, note_type=1, note_id=900003
        )
        NoteRecentMaster.objects.filter(pk=both.pk).update(team=None, user=None)
        NoteRecentMaster.objects.filter(pk=no_user.pk).update(user=None)
        NoteRecentMaster.objects.filter(pk=no_team.pk).update(team=None)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.recent_notes, 1)
        self.assertFalse(NoteRecentMaster.objects.filter(pk=both.pk).exists())
        self.assertTrue(NoteRecentMaster.objects.filter(pk=no_user.pk).exists())
        self.assertTrue(NoteRecentMaster.objects.filter(pk=no_team.pk).exists())
        out = self._run()
        self.assertIn("origin.NoteRecentMaster.team: 1", out)  # the one with a live user

    # --- attachments ---------------------------------------------------------------------

    def _attachment(self, note):
        row = PersonalNoteAttachmentFact(note=note, uploader=self.owner)
        row.note_attachment_url.save("secret.txt", ContentFile(b"secret"), save=True)
        return row

    def test_the_uploaded_files_of_an_orphan_note_go_with_it(self):
        note = self._note(team=self.gone, owner=self.owner)
        row = self._attachment(note)
        path = row.note_attachment_url.name
        self.assertTrue(default_storage.exists(path))
        kept_note = self._note(team=self.team, owner=self.owner, title="Kept")
        kept_row = self._attachment(kept_note)
        self._orphan(note)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.note_attachments, 1)
        self.assertFalse(PersonalNoteAttachmentFact.objects.filter(pk=row.pk).exists())
        self.assertFalse(default_storage.exists(path))
        self.assertTrue(PersonalNoteAttachmentFact.objects.filter(pk=kept_row.pk).exists())
        self.assertTrue(default_storage.exists(kept_row.note_attachment_url.name))

    def test_a_file_that_cannot_be_deleted_is_reported_not_fatal(self):
        note = self._orphan(self._note(team=self.gone, owner=self.owner))
        self._attachment(note)

        with mock.patch.object(default_storage, "delete", side_effect=OSError("storage down")):
            report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.note_attachment_file_errors, 1)
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())

    # --- the command: dry run, second run, rollback -------------------------------------

    def _one_of_each(self):
        note = self._orphan(self._note(team=self.gone, owner=self.owner))
        folder = self._orphan(self._folder(team=self.gone, owner=self.owner))
        recent = NoteRecentMaster.objects.create(
            team=self.gone, user=self.owner, note_type=1, note_id=900009
        )
        NoteRecentMaster.objects.filter(pk=recent.pk).update(team=None, user=None)
        attachment = self._attachment(note)
        return note, folder, recent, attachment

    def test_a_dry_run_counts_and_writes_nothing(self):
        note, folder, recent, attachment = self._one_of_each()
        before = self._traces(note.note_id)
        self.bulk.reset_mock()

        out = self._run()

        self.assertIn("1 My Notes note(s) would be deleted", out)
        self.assertIn("1 note attachment(s) would be deleted", out)
        self.assertIn("1 My Notes folder(s) would be deleted", out)
        self.assertIn("1 recently-opened row(s) would be deleted", out)
        self.assertIn("dry run", out)
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).exists())
        self.assertTrue(NoteRecentMaster.objects.filter(pk=recent.pk).exists())
        self.assertTrue(PersonalNoteAttachmentFact.objects.filter(pk=attachment.pk).exists())
        self.assertTrue(default_storage.exists(attachment.note_attachment_url.name))
        self.assertEqual(self._traces(note.note_id), before)
        self.bulk.assert_not_called()

    def test_apply_deletes_and_a_second_run_finds_nothing(self):
        self._one_of_each()

        out = self._run("--apply", "--yes")
        self.assertIn("1 My Notes note(s) deleted", out)
        self.assertIn("1 My Notes folder(s) deleted", out)
        self.assertIn("1 recently-opened row(s) deleted", out)

        again = self._run("--apply", "--yes")
        self.assertIn("0 My Notes note(s) deleted", again)
        self.assertIn("0 My Notes folder(s) deleted", again)
        self.assertIn("0 recently-opened row(s) deleted", again)
        self.assertIn("0 note attachment(s) deleted", again)
        self.assertEqual(purge_orphaned_team_rows(apply=False).total, 0)

    def test_a_failure_part_way_rolls_everything_back(self):
        """Even the rows deleted BEFORE the notes (a mention group) come back."""
        group = MentionGroupMaster.objects.create(
            team=self.gone, group_name="design", created_by=self.other
        )
        MentionGroupMaster.objects.filter(pk=group.pk).update(team=None)
        note, folder, recent, attachment = self._one_of_each()
        before = self._traces(note.note_id)

        with mock.patch.object(
            orphaned_team_rows, "forget_deleted_notes", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                purge_orphaned_team_rows(apply=True)

        self.assertTrue(MentionGroupMaster.objects.filter(group_id=group.group_id).exists())
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())
        self.assertTrue(PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).exists())
        self.assertTrue(NoteRecentMaster.objects.filter(pk=recent.pk).exists())
        self.assertTrue(PersonalNoteAttachmentFact.objects.filter(pk=attachment.pk).exists())
        self.assertTrue(default_storage.exists(attachment.note_attachment_url.name))
        self.assertEqual(self._traces(note.note_id), before)
        # and nothing went to the index or the disk: those run after the commit
        self.bulk.assert_not_called()

    def test_a_search_cluster_failure_does_not_undo_the_delete(self):
        note = self._orphan(self._note(team=self.gone, owner=self.owner))
        self.bulk.side_effect = RuntimeError("opensearch down")

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.personal_notes, 1)
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())
        # the tracking rows stay, so the reindex cron's orphan sweep retries them
        self.assertEqual(self._traces(note.note_id)["chunks"], 2)

    # --- the survey ----------------------------------------------------------------------

    def test_the_survey_lists_the_notes_the_rule_kept_and_not_the_ones_it_deletes(self):
        start = start_here_note._create(self.owner, "en")
        orphan = self._orphan(self._note(team=self.gone, owner=self.owner))

        report = purge_orphaned_team_rows(apply=False)

        self.assertEqual(report.personal_notes, 1)
        self.assertEqual(report.survey.get("origin.PersonalNoteMaster.team"), 1)
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=orphan.note_id).exists())
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=start.note_id).exists())

    def test_rows_of_a_live_team_are_never_touched(self):
        note = self._note(team=self.team, owner=self.other)
        folder = self._folder(team=self.team, owner=self.other)
        recent = NoteRecentMaster.objects.filter(note_id=note.note_id).first()
        shared_in_a_folder = self._note(team=self.team, owner=self.other, folder=folder)

        report = purge_orphaned_team_rows(apply=True)

        self.assertEqual(report.total, 0)
        self.assertTrue(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())
        self.assertTrue(PersonalNoteMaster.objects.filter(pk=shared_in_a_folder.pk).exists())
        self.assertTrue(PersonalNoteFolder.objects.filter(pk=folder.pk).exists())
        self.assertTrue(NoteRecentMaster.objects.filter(pk=recent.pk).exists())
        self.assertTrue(all(self._traces(note.note_id).values()))
