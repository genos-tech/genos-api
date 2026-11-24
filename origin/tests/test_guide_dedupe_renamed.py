"""`refresh_guide_notes --dedupe-renamed`: the cleanup of the 2026-11-24 duplicate Japanese guide notes.

Pinned: an unedited old-title copy is removed only when the folder already holds the current-title note;
an edited copy, a copy a person saved a version of, a shared one and a folder without the new note are
left (and counted); a dry run writes nothing; a second run changes nothing; counts only; scoped by team.
"""

import copy
from unittest import mock

from django.core.management import call_command

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.services import guide_refresh
from origin.tests.test_guide_refresh import FIXTURE, RefreshBase

OLD = "よくある問題の解決方法"
NEW = "よくある困りごとの対処法"


class DedupeTests(RefreshBase):
    def pair(self, new_present=True, **kw):
        folder = self.folder("ja", given=[OLD, NEW], **kw)
        old = self.note(folder, OLD, copy.deepcopy(FIXTURE["ja"][OLD][0]["body"]))
        if new_present:
            self.note(folder, NEW, copy.deepcopy(self.current("ja")[NEW]))
        return folder, old

    def exists(self, note):
        return PersonalNoteMaster.objects.filter(note_id=note.note_id).exists()

    def test_an_unedited_old_copy_beside_the_new_note_is_removed_with_its_rows(self):
        folder, old = self.pair()
        NotePermissionMaster.objects.create(
            team_id=folder.team_id,
            user_id=folder.owner_id,
            note_id=old.note_id,
            note_type=1,
            role_id=1,
        )
        report = guide_refresh.dedupe_renamed_guide_notes(apply=True)
        self.assertEqual((report.removed, report.edited_left, report.awaiting_rename), (1, 0, 0))
        self.assertFalse(self.exists(old))
        self.assertFalse(
            NotePermissionMaster.objects.filter(note_id=old.note_id, note_type=1).exists()
        )
        self.assertEqual(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id, title=NEW).count(), 1
        )

    def test_an_edited_old_copy_is_untouched_and_counted(self):
        _folder, old = self.pair()
        body = copy.deepcopy(old.body)
        body[0]["content"][0]["text"] += " (mine)"
        PersonalNoteMaster.objects.filter(pk=old.pk).update(body=body)
        report = guide_refresh.dedupe_renamed_guide_notes(apply=True)
        self.assertEqual((report.removed, report.edited_left), (0, 1))
        self.assertTrue(self.exists(old))

    def test_a_folder_without_the_new_note_is_left_for_the_normal_rename(self):
        _folder, old = self.pair(new_present=False)
        report = guide_refresh.dedupe_renamed_guide_notes(apply=True)
        self.assertEqual((report.removed, report.awaiting_rename), (0, 1))
        self.assertTrue(self.exists(old))
        guide_refresh.refresh_guide_notes(apply=True)
        old.refresh_from_db()
        self.assertEqual(old.title, NEW)

    def test_a_copy_a_person_saved_a_version_of_is_left(self):
        _folder, old = self.pair()
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=1,
            note_id=old.note_id,
            version_no=1,
            editor=self.user,
            title=OLD,
            body=old.body,
        )
        report = guide_refresh.dedupe_renamed_guide_notes(apply=True)
        self.assertEqual((report.removed, report.history_left), (0, 1))
        self.assertTrue(self.exists(old))

    def test_a_copy_with_a_child_note_is_left(self):
        _folder, old = self.pair()
        child = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="child", body=[], parent_note_id=old.note_id
        )
        report = guide_refresh.dedupe_renamed_guide_notes(apply=True)
        self.assertEqual((report.removed, report.history_left), (0, 1))
        self.assertTrue(self.exists(old) and self.exists(child))

    def test_a_dry_run_writes_nothing_and_a_second_run_changes_nothing(self):
        _folder, old = self.pair()
        dry = guide_refresh.dedupe_renamed_guide_notes()
        self.assertEqual(dry.removed, 1)
        self.assertTrue(self.exists(old))
        guide_refresh.dedupe_renamed_guide_notes(apply=True)
        again = guide_refresh.dedupe_renamed_guide_notes(apply=True)
        self.assertEqual((again.removed, again.edited_left, again.awaiting_rename), (0, 0, 0))

    def test_a_note_edited_between_the_scan_and_the_delete_is_kept(self):
        _folder, old = self.pair()
        real = guide_refresh._only_ours

        def edit_then_check(note_id, owner_id):
            body = copy.deepcopy(old.body)
            body[0]["content"][0]["text"] += " (late)"
            PersonalNoteMaster.objects.filter(pk=old.pk).update(body=body)
            return real(note_id, owner_id)

        with mock.patch.object(guide_refresh, "_only_ours", edit_then_check):
            report = guide_refresh.dedupe_renamed_guide_notes(apply=True)
        self.assertEqual((report.removed, report.changed_underneath), (0, 1))
        self.assertTrue(self.exists(old))

    def test_a_team_scope_only_touches_that_team(self):
        _folder, old = self.pair()
        report = guide_refresh.dedupe_renamed_guide_notes(
            apply=True, team_id="00000000-0000-4000-8000-000000000000"
        )
        self.assertEqual((report.folders, report.removed), (0, 0))
        self.assertTrue(self.exists(old))

    def test_the_command_prints_counts_only(self):
        _folder, old = self.pair()
        out = mock.MagicMock()
        call_command("refresh_guide_notes", "--dedupe-renamed", stdout=out)
        shown = "".join(c.args[0] + "\n" for c in out.write.call_args_list)
        self.assertIn("1 duplicate old-title note(s) to remove", shown)
        self.assertIn("DRY RUN", shown)
        self.assertNotIn(OLD, shown)
        self.assertNotIn(str(old.note_id), shown)
        self.assertTrue(self.exists(old))
        call_command("refresh_guide_notes", "--dedupe-renamed", "--apply", stdout=mock.MagicMock())
        self.assertFalse(self.exists(old))
