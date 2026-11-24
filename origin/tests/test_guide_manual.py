"""The guide notes are rendered from the vendored manual (MIP-2324).

Pinned here: the copy is valid and in step with `scripts/sync_manual.py`; new members get the manual's
text (and its topics that the hand-written guide never had); a language without a copy keeps the
hand-written guide; the company notes given to existing members are the manual's; the sync script
validates before it writes and reports a copy that is behind; and the Spanish and Chinese "Start here"
menu paths are the app's own words.
"""

import importlib.util
import json
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.services import guide_manual, guide_notes, start_here_note
from origin.services.guide_company_notes import COMPANY_NOTES
from origin.tests.test_base import BaseAPITestCase

ROOT = Path(__file__).resolve().parent.parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "sync_manual", ROOT / "scripts" / "sync_manual.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sync_manual = _load_script()


def _text(blocks) -> str:
    return "\n".join(
        "".join(item.get("text", "") for item in block.get("content") or []) for block in blocks
    )


class VendoredCopyTests(SimpleTestCase):
    def test_every_copy_is_valid(self):
        copies = sync_manual.read_copy()
        self.assertIn("en", copies)
        for lang, raw in copies.items():
            self.assertEqual(sync_manual.validate(lang, raw), [], lang)

    def test_each_topic_renders_to_a_non_empty_note(self):
        for lang in sync_manual.read_copy():
            notes = guide_manual.manual_notes(lang)
            self.assertEqual(len(notes), len(guide_manual.manual_topics(lang)))
            for title, body in notes:
                self.assertTrue(body, f"{lang}: {title!r} rendered empty")
                self.assertTrue(any(b.get("type") == "heading" for b in body), title)

    def test_a_language_without_a_copy_has_no_manual(self):
        self.assertIsNone(guide_manual.manual_notes("xx"))

    def test_the_company_notes_the_guide_gives_existing_members_are_in_the_manual(self):
        titles = {title for title, _ in guide_manual.manual_notes("en")}
        for title, _ in COMPANY_NOTES["en"]:
            self.assertIn(title, titles)

    def test_the_connect_notes_the_refresh_command_looks_for_are_in_the_manual(self):
        titles = {title for title, _ in guide_manual.manual_notes("en")}
        for title in guide_notes._CONNECT_ANCHORS["en"]:
            self.assertIn(title, titles)


class CurrentGuideNotesTests(SimpleTestCase):
    def test_a_language_with_a_copy_is_rendered_from_it(self):
        self.assertEqual(
            [title for title, _ in guide_notes.current_guide_notes("en")],
            [t["title"] for t in guide_manual.manual_topics("en")],
        )

    def test_a_language_without_a_copy_keeps_the_hand_written_guide(self):
        with mock.patch.object(guide_manual, "load_manual", return_value=None):
            notes = guide_notes.current_guide_notes("ja")
        self.assertEqual([t for t, _ in notes], [t for t, _ in guide_notes.JAPANESE_GUIDE_NOTES])

    def test_the_manual_replaces_the_hand_written_connect_section_it_does_not_double_it(self):
        notes = dict(guide_notes.current_guide_notes("en"))
        body = notes["Connecting Genos to other tools: API, MCP, and realtime"]
        headings = [
            _text([b]) for b in body if b.get("type") == "heading" and "no API key" in _text([b])
        ]
        self.assertEqual(len(headings), 1)


class SeedingFromTheManualTests(BaseAPITestCase):
    def test_a_new_member_gets_the_manuals_text_in_english(self):
        with mock.patch("origin.services.demo_seeder.kick_off_demo_reindex"):
            guide_notes.seed_guide_notes(self.user, self.team)
        folder = PersonalNoteFolder.objects.get(
            team=self.team, owner=self.user, name=guide_notes.GUIDE_FOLDER_NAME
        )
        titles = set(
            PersonalNoteMaster.objects.filter(folder_id=folder.folder_id).values_list(
                "title", flat=True
            )
        )
        # "Settings A-Z" exists only in the manual, never in the hand-written guide.
        self.assertIn("Settings A-Z", titles)
        self.assertEqual(titles, {t["title"] for t in guide_manual.manual_topics("en")})

    def test_add_company_notes_writes_the_manuals_version(self):
        with mock.patch("origin.services.demo_seeder.kick_off_demo_reindex"):
            guide_notes.seed_guide_notes(self.user, self.team)
        title = COMPANY_NOTES["en"][0][0]
        PersonalNoteMaster.objects.filter(owner=self.user, title=title).delete()
        report = guide_notes.add_company_guide_notes(apply=True)
        self.assertEqual(len(report.added), 1)
        note = PersonalNoteMaster.objects.get(owner=self.user, title=title)
        expected = dict(guide_manual.manual_notes("en"))[title]
        self.assertEqual(
            guide_notes.body_fingerprint(note.body), guide_notes.body_fingerprint(expected)
        )


class SyncScriptTests(SimpleTestCase):
    def _manual(self, lang="en", **over):
        data = {"language": lang, "topics": [{"id": "a", "title": "A", "markdown": "## A\n\nx"}]}
        data.update(over)
        return json.dumps(data).encode()

    def test_the_checked_in_copy_is_current_with_itself(self):
        self.assertEqual(sync_manual.main(["--source", str(sync_manual.TARGET_DIR), "--check"]), 0)

    def test_validate_rejects_a_wrong_language_empty_topics_and_duplicates(self):
        self.assertTrue(sync_manual.validate("en", b"not json"))
        self.assertTrue(sync_manual.validate("en", self._manual("ja")))
        self.assertTrue(sync_manual.validate("en", self._manual(topics=[])))
        dup = {"id": "a", "title": "A", "markdown": "x"}
        self.assertTrue(sync_manual.validate("en", self._manual(topics=[dup, dup])))
        self.assertTrue(
            sync_manual.validate(
                "en", self._manual(topics=[{"id": "a", "title": "", "markdown": "x"}])
            )
        )
        self.assertEqual(sync_manual.validate("en", self._manual()), [])

    def test_differences_names_missing_changed_and_removed_languages(self):
        a, b = self._manual(), self._manual(topics=[{"id": "b", "title": "B", "markdown": "y"}])
        self.assertEqual(sync_manual.differences({"en": a}, {"en": a}), [])
        self.assertIn("differs", sync_manual.differences({"en": a}, {"en": b})[0])
        self.assertIn("not copied yet", sync_manual.differences({"en": a}, {})[0])
        self.assertIn("no longer exports", sync_manual.differences({}, {"ja": a})[0])

    def test_a_behind_copy_fails_the_check_and_sync_fixes_it_without_touching_a_bad_export(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            (Path(src) / "manual.en.json").write_bytes(self._manual())
            with mock.patch.object(sync_manual, "TARGET_DIR", Path(dst)):
                self.assertEqual(sync_manual.main(["--source", src, "--check"]), 1)
                self.assertEqual(sync_manual.main(["--source", src]), 0)
                self.assertEqual(sync_manual.main(["--source", src, "--check"]), 0)
                (Path(src) / "manual.en.json").write_bytes(b"{}")
                self.assertEqual(sync_manual.main(["--source", src]), 2)
                self.assertEqual((Path(dst) / "manual.en.json").read_bytes(), self._manual())


class StartHereMenuPathTests(SimpleTestCase):
    """The menu names are the ones the frontend dictionaries show in that language."""

    def test_spanish_and_chinese_use_the_apps_own_words(self):
        self.assertEqual(
            start_here_note._COPY["es"]["menu"], "Ajustes > Desarrolladores > Apps conectadas"
        )
        self.assertEqual(start_here_note._COPY["zh"]["menu"], "设置 > 开发者 > 已连接的应用")
