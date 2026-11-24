"""`refresh_guide_notes`: existing guide folders get today's guide (MIP-2325, user-guide milestone MIP-2304).

Pinned:
* the record of shipped texts is complete and consistent: every body in the fixture hashes to the
  fingerprint the service reads;
* an unedited copy of EVERY text the guide ever shipped, for every title, in both editions, is replaced;
  a copy renamed in the manual is replaced and retitled;
* an EDITED copy is never touched (text, title, timestamp, versions, collab push) and is counted;
* a note missing from a folder is added, unless the folder was given it and the member deleted it;
  a folder with no record is judged by its age;
* a dry run writes nothing and prints counts only; a second run changes nothing; --team scopes.
"""

import copy
import json
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase
from django.utils import timezone

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.services import guide_notes, guide_refresh
from origin.services.collab_push import PushOutcome
from origin.tests.test_base import BaseAPITestCase

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "guide_notes_shipped.json").read_text(encoding="utf-8")
)
FOLDER = {"en": guide_notes.GUIDE_FOLDER_NAME, "ja": guide_notes.JAPANESE_GUIDE_FOLDER_NAME}
OLD_AI = "Genos AI (Spotlight): ask, act, and stay ahead"
NEW_AI = "Genos AI: ask, act, and stay ahead"


class ShippedRecordTests(SimpleTestCase):
    def test_every_fixture_body_hashes_to_the_fingerprint_the_service_reads(self):
        seen = 0
        for locale, titles in FIXTURE.items():
            for title, entries in titles.items():
                fingerprints = guide_refresh.shipped_fingerprints(locale, title)
                for entry in entries:
                    self.assertEqual(guide_notes.body_fingerprint(entry["body"]), entry["sha256"])
                    self.assertIn(entry["sha256"], fingerprints)
                    seen += 1
        self.assertGreater(seen, 50)

    def test_the_file_and_the_fixture_list_the_same_texts(self):
        shipped = guide_refresh._shipped()
        for locale, titles in shipped.items():
            self.assertEqual(set(titles), set(FIXTURE[locale]))
            for title, info in titles.items():
                self.assertEqual(
                    sorted(info["sha256"]), sorted(e["sha256"] for e in FIXTURE[locale][title])
                )

    def test_every_title_of_the_hand_written_guide_has_a_record(self):
        for locale, (_, notes) in guide_notes._EDITIONS.items():
            renamed_to = set(guide_refresh.RENAMED_TITLES[locale].values())
            for title, _body in notes:
                # A title the manual renamed is recorded under the title it shipped with.
                if title not in renamed_to:
                    self.assertIn(title, guide_refresh._shipped()[locale], title)

    def test_every_shipped_title_is_current_or_renamed_to_a_current_title(self):
        for locale in ("en", "ja"):
            current = {t for t, _ in guide_notes.current_guide_notes(locale)}
            renames = guide_refresh.RENAMED_TITLES[locale]
            for title in guide_refresh._shipped()[locale]:
                self.assertIn(renames.get(title, title), current, title)

    def test_the_renamed_old_title_is_a_shipped_title(self):
        for locale, renames in guide_refresh.RENAMED_TITLES.items():
            for old in renames:
                self.assertIn(old, guide_refresh._shipped()[locale])


class RefreshBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(
            "origin.services.guide_refresh.push_note_body_now", return_value=PushOutcome.SENT
        )
        self.push = patcher.start()
        self.addCleanup(patcher.stop)

    def folder(self, locale="en", given=None, owner=None, team=None, created=None):
        folder = PersonalNoteFolder.objects.create(
            team=team or self.team,
            owner=owner or self.user,
            name=FOLDER[locale],
            guide_titles_given=given,
        )
        if created is not None:
            PersonalNoteFolder.objects.filter(pk=folder.pk).update(ts_created_at=created)
            folder.refresh_from_db()
        return folder

    def note(self, folder, title, body, owner=None):
        return PersonalNoteMaster.objects.create(
            team_id=folder.team_id,
            owner=owner or self.user,
            title=title,
            body=body,
            folder_id=folder.folder_id,
        )

    def current(self, locale):
        return dict(guide_notes.current_guide_notes(locale))

    def fp(self, note):
        note.refresh_from_db()
        return guide_notes.body_fingerprint(note.body)

    def full_folder(self, locale="en", **kw):
        """A folder holding today's text of every note, and the record of that."""
        current = self.current(locale)
        folder = self.folder(locale, given=list(current), **kw)
        notes = {t: self.note(folder, t, copy.deepcopy(b)) for t, b in current.items()}
        return folder, notes


class ReplaceTests(RefreshBase):
    def test_an_unedited_copy_of_every_shipped_text_of_every_title_is_replaced(self):
        for locale in ("en", "ja"):
            folder = self.folder(locale, given=list(self.current(locale)))
            old = []
            for title, entries in FIXTURE[locale].items():
                for entry in entries:
                    if (
                        title in self.current(locale)
                        or title in guide_refresh.RENAMED_TITLES[locale]
                    ):
                        old.append((title, self.note(folder, title, copy.deepcopy(entry["body"]))))
            self.assertGreater(len(old), 20)

        report = guide_refresh.refresh_guide_notes(apply=True)

        self.assertEqual(report.edited_left, 0)
        self.assertEqual(report.changed_underneath, 0)
        for locale in ("en", "ja"):
            current = self.current(locale)
            renames = guide_refresh.RENAMED_TITLES[locale]
            for note in PersonalNoteMaster.objects.filter(
                folder_id__in=PersonalNoteFolder.objects.filter(name=FOLDER[locale]).values(
                    "folder_id"
                )
            ):
                self.assertIn(note.title, current)
                self.assertNotIn(note.title, renames)
                self.assertEqual(self.fp(note), guide_notes.body_fingerprint(current[note.title]))
        self.assertEqual(self.push.call_count, report.replaced)
        self.assertEqual(report.pushes, {"sent": report.replaced})

    def test_the_replaced_text_is_kept_as_a_version_and_pushed(self):
        folder = self.folder("en", given=list(self.current("en")))
        title = "Tasks and projects"
        old = FIXTURE["en"][title][0]["body"]
        note = self.note(folder, title, copy.deepcopy(old))

        guide_refresh.refresh_guide_notes(apply=True)

        versions = list(
            NoteVersionMaster.objects.filter(note_type=1, note_id=note.note_id).order_by(
                "version_no"
            )
        )
        self.assertEqual(len(versions), 2)
        self.assertEqual(
            guide_notes.body_fingerprint(versions[0].body), guide_notes.body_fingerprint(old)
        )
        note.refresh_from_db()
        self.assertEqual(versions[1].body, note.body)
        pushed = [c.args for c in self.push.call_args_list if c.args[1] == note.note_id]
        self.assertEqual(pushed, [("personal", note.note_id, note.body)])

    def test_an_unedited_copy_of_a_renamed_note_is_replaced_and_retitled_not_duplicated(self):
        folder = self.folder("en", given=[OLD_AI])
        note = self.note(folder, OLD_AI, copy.deepcopy(FIXTURE["en"][OLD_AI][0]["body"]))

        guide_refresh.refresh_guide_notes(apply=True)

        note.refresh_from_db()
        self.assertEqual(note.title, NEW_AI)
        self.assertEqual(self.fp(note), guide_notes.body_fingerprint(self.current("en")[NEW_AI]))
        self.assertEqual(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id, title=NEW_AI).count(), 1
        )
        self.assertFalse(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id, title=OLD_AI).exists()
        )

    def test_an_edited_copy_of_a_renamed_note_is_left_and_the_new_title_is_not_added(self):
        folder = self.folder("en", given=[OLD_AI])
        body = copy.deepcopy(FIXTURE["en"][OLD_AI][0]["body"])
        body[0]["content"][0]["text"] += " (mine)"
        note = self.note(folder, OLD_AI, body)

        guide_refresh.refresh_guide_notes(apply=True)

        note.refresh_from_db()
        self.assertEqual((note.title, note.body), (OLD_AI, body))
        self.assertFalse(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id, title=NEW_AI).exists()
        )

    def test_the_hand_written_text_with_the_oauth_section_counts_as_shipped(self):
        # A member seeded after MIP-1558 with OAuth on holds a body that is in no git revision.
        title = "Connecting Genos to other tools: API, MCP, and realtime"
        with (
            self.settings(MCP_OAUTH_ENABLED=True, API_PUBLIC_BASE_URL="https://api.example.com"),
            mock.patch("origin.services.guide_manual.load_manual", return_value=None),
        ):
            seeded = dict(guide_notes.current_guide_notes("en"))[title]
        with self.settings(MCP_OAUTH_ENABLED=True, API_PUBLIC_BASE_URL="https://api.example.com"):
            self.assertIn(
                guide_notes.body_fingerprint(seeded),
                guide_refresh.shipped_fingerprints("en", title),
            )


class LeaveEditedTests(RefreshBase):
    def test_an_edited_copy_is_never_touched_and_is_counted(self):
        folder = self.folder("en", given=list(self.current("en")))
        edited = []
        for i, title in enumerate(("Tasks and projects", "Teams, members, and roles")):
            body = copy.deepcopy(FIXTURE["en"][title][0]["body"])
            body[0]["content"][0]["text"] += " (my note)"
            edited.append(self.note(folder, title, body))
        emptied = self.note(folder, "Fixes for specific problems", [])
        nulled = self.note(folder, "Settings: every option, and where to find it", [])
        PersonalNoteMaster.objects.filter(pk=nulled.pk).update(body=None)
        nested = self.note(folder, "Keyboard shortcuts, in full", [])
        body = copy.deepcopy(FIXTURE["en"]["Keyboard shortcuts, in full"][0]["body"])
        body[1]["children"] = [copy.deepcopy(body[2])]
        PersonalNoteMaster.objects.filter(pk=nested.pk).update(body=body)
        mine = [*edited, emptied, nulled, nested]
        snap = {n.pk: PersonalNoteMaster.objects.values().get(pk=n.pk) for n in mine}

        report = guide_refresh.refresh_guide_notes(apply=True)

        self.assertEqual(report.edited_left, len(mine))
        for pk, row in snap.items():
            self.assertEqual(PersonalNoteMaster.objects.values().get(pk=pk), row)
        self.assertFalse(NoteVersionMaster.objects.filter(note_id__in=snap).exists())
        self.assertFalse([c for c in self.push.call_args_list if c.args[1] in snap])

    def test_a_note_edited_between_the_scan_and_the_write_is_left_alone(self):
        folder = self.folder("en")
        title = "Tasks and projects"
        note = self.note(folder, title, [])
        edited = copy.deepcopy(FIXTURE["en"][title][0]["body"])
        edited[0]["content"][0]["text"] += "!"
        PersonalNoteMaster.objects.filter(pk=note.pk).update(body=edited)

        changed = guide_refresh._replace(
            note.pk, title, title, guide_refresh.shipped_fingerprints("en", title), [{"id": "x"}]
        )

        self.assertFalse(changed)
        note.refresh_from_db()
        self.assertEqual(note.body, edited)

    def test_a_note_outside_a_guide_folder_or_with_another_title_is_never_looked_at(self):
        other = PersonalNoteFolder.objects.create(team=self.team, owner=self.user, name="Mine")
        outside = self.note(
            other,
            "Tasks and projects",
            copy.deepcopy(FIXTURE["en"]["Tasks and projects"][0]["body"]),
        )
        folder = self.folder("en", given=list(self.current("en")))
        renamed = self.note(
            folder, "My copy", copy.deepcopy(FIXTURE["en"]["Tasks and projects"][0]["body"])
        )
        before = [PersonalNoteMaster.objects.values().get(pk=n.pk) for n in (outside, renamed)]

        guide_refresh.refresh_guide_notes(apply=True)

        after = [PersonalNoteMaster.objects.values().get(pk=n.pk) for n in (outside, renamed)]
        self.assertEqual(before, after)


class AddTests(RefreshBase):
    def test_a_note_the_folder_was_never_given_is_added_with_its_owner_permission(self):
        folder, notes = self.full_folder("en")
        title = "Settings A-Z"
        notes[title].delete()
        PersonalNoteFolder.objects.filter(pk=folder.pk).update(
            guide_titles_given=[t for t in self.current("en") if t != title]
        )

        report = guide_refresh.refresh_guide_notes(apply=True)

        self.assertEqual(report.added, 1)
        self.assertEqual(report.deleted_left, 0)
        added = PersonalNoteMaster.objects.get(folder_id=folder.folder_id, title=title)
        self.assertEqual(self.fp(added), guide_notes.body_fingerprint(self.current("en")[title]))
        self.assertTrue(
            NotePermissionMaster.objects.filter(
                note_id=added.note_id, note_type=1, user=self.user, role_id=1
            ).exists()
        )
        folder.refresh_from_db()
        self.assertIn(title, folder.guide_titles_given)

    def test_a_note_the_member_deleted_is_not_brought_back_on_this_run_or_the_next(self):
        folder, notes = self.full_folder("en")
        title = "Tasks and projects"
        notes[title].delete()

        for _ in range(2):
            report = guide_refresh.refresh_guide_notes(apply=True)
            self.assertEqual(report.added, 0)
            self.assertEqual(report.deleted_left, 1)
        self.assertFalse(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id, title=title).exists()
        )

    def test_a_new_manual_topic_is_added_once_then_deleting_it_sticks(self):
        folder, notes = self.full_folder("en")
        title = "Settings A-Z"
        notes[title].delete()
        PersonalNoteFolder.objects.filter(pk=folder.pk).update(
            guide_titles_given=[t for t in self.current("en") if t != title]
        )
        guide_refresh.refresh_guide_notes(apply=True)
        PersonalNoteMaster.objects.filter(folder_id=folder.folder_id, title=title).delete()

        report = guide_refresh.refresh_guide_notes(apply=True)

        self.assertEqual((report.added, report.deleted_left), (0, 1))

    def test_a_folder_with_no_record_is_judged_by_its_age(self):
        # Old folder: made before the company notes shipped (2026-11-18), so it never had them.
        old = self.folder("en", created=timezone.now() - timedelta(days=200))
        # Recent folder: made after everything old shipped, so a missing old note was deleted.
        recent = self.folder("en", owner=self.user2, created=timezone.now() - timedelta(days=1))
        company = "Organising a large workspace: one team, many departments"
        for folder, owner in ((old, self.user), (recent, self.user2)):
            for title, body in self.current("en").items():
                if title != company:
                    self.note(folder, title, copy.deepcopy(body), owner=owner)

        report = guide_refresh.refresh_guide_notes(apply=True)

        # old: the company notes it never had, and the manual topics that never shipped.
        self.assertTrue(
            PersonalNoteMaster.objects.filter(folder_id=old.folder_id, title=company).exists()
        )
        self.assertFalse(
            PersonalNoteMaster.objects.filter(folder_id=recent.folder_id, title=company).exists()
        )
        self.assertGreater(report.added, 0)
        self.assertEqual(report.deleted_left, 1)
        old.refresh_from_db()
        self.assertIn(company, old.guide_titles_given)

    def test_seeding_records_the_titles_the_folder_was_given(self):
        guide_notes.seed_guide_notes(self.user, self.team, force=True)
        for locale in ("en", "ja"):
            folder = PersonalNoteFolder.objects.get(
                owner=self.user, team=self.team, name=FOLDER[locale]
            )
            self.assertEqual(folder.guide_titles_given, list(self.current(locale)))
        self.assertEqual(guide_refresh.refresh_guide_notes().added, 0)


class RunTests(RefreshBase):
    def _stale(self):
        folder = self.folder("en", given=list(self.current("en")))
        note = self.note(
            folder,
            "Tasks and projects",
            copy.deepcopy(FIXTURE["en"]["Tasks and projects"][0]["body"]),
        )
        return folder, note

    def test_a_dry_run_writes_nothing_and_counts(self):
        folder, note = self._stale()
        before = PersonalNoteMaster.objects.values().get(pk=note.pk)
        folder_before = PersonalNoteFolder.objects.values().get(pk=folder.pk)

        report = guide_refresh.refresh_guide_notes(apply=False)

        self.assertEqual(report.replaced, 1)
        self.assertEqual(report.added, 0)  # it was given every other note and deleted them
        self.assertGreater(report.deleted_left, 0)
        self.assertEqual(PersonalNoteMaster.objects.values().get(pk=note.pk), before)
        self.assertEqual(PersonalNoteFolder.objects.values().get(pk=folder.pk), folder_before)
        self.assertEqual(PersonalNoteMaster.objects.count(), 1)
        self.assertEqual(self.push.call_count, 0)
        self.assertFalse(NoteVersionMaster.objects.exists())

    def test_a_second_run_changes_nothing(self):
        folder, note = self._stale()
        guide_refresh.refresh_guide_notes(apply=True)
        stamp = PersonalNoteMaster.objects.values().get(pk=note.pk)
        count, pushes = PersonalNoteMaster.objects.count(), self.push.call_count

        report = guide_refresh.refresh_guide_notes(apply=True)

        self.assertEqual((report.replaced, report.added, report.edited_left), (0, 0, 0))
        self.assertEqual(PersonalNoteMaster.objects.values().get(pk=note.pk), stamp)
        self.assertEqual(PersonalNoteMaster.objects.count(), count)
        self.assertEqual(self.push.call_count, pushes)

    def test_a_team_scope_only_touches_that_team(self):
        _, mine = self._stale()
        other_team = type(self.team).objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        other_folder = self.folder("en", given=list(self.current("en")), team=other_team)
        other = self.note(
            other_folder,
            "Tasks and projects",
            copy.deepcopy(FIXTURE["en"]["Tasks and projects"][0]["body"]),
        )
        before = PersonalNoteMaster.objects.values().get(pk=other.pk)

        report = guide_refresh.refresh_guide_notes(apply=True, team_id=self.team.team_id)

        self.assertEqual(report.folders, 1)
        self.assertEqual(PersonalNoteMaster.objects.values().get(pk=other.pk), before)
        self.assertNotEqual(
            self.fp(mine),
            guide_notes.body_fingerprint(FIXTURE["en"]["Tasks and projects"][0]["body"]),
        )

    def test_more_folders_than_one_batch_are_all_processed(self):
        with mock.patch.object(guide_refresh, "BATCH_FOLDERS", 2):
            for i in range(5):
                owner = type(self.user).objects.create_user(
                    email=f"batch{i}@example.com", password="x", username=f"batch{i}"
                )
                folder = self.folder("en", given=list(self.current("en")), owner=owner)
                self.note(
                    folder,
                    "Tasks and projects",
                    copy.deepcopy(FIXTURE["en"]["Tasks and projects"][0]["body"]),
                    owner=owner,
                )
            report = guide_refresh.refresh_guide_notes(apply=True)
        self.assertEqual((report.folders, report.replaced), (5, 5))

    def test_the_command_is_a_dry_run_unless_apply_and_prints_counts_only(self):
        folder, note = self._stale()
        out = mock.MagicMock()
        call_command("refresh_guide_notes", stdout=out)
        shown = "".join(c.args[0] + "\n" for c in out.write.call_args_list)
        self.assertIn("DRY RUN", shown)
        self.assertIn("1 unedited note(s) to replace", shown)
        self.assertNotIn("Tasks and projects", shown)
        self.assertNotIn(str(note.note_id), shown)
        self.assertEqual(PersonalNoteMaster.objects.count(), 1)

        out = mock.MagicMock()
        call_command("refresh_guide_notes", "--apply", stdout=out)
        shown = "".join(c.args[0] + "\n" for c in out.write.call_args_list)
        self.assertIn("APPLIED", shown)
        self.assertIn("1 unedited note(s) replaced", shown)
        self.assertEqual(PersonalNoteMaster.objects.count(), 1)  # deleted ones are not added
