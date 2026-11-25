"""The record of shipped manual texts (MIP-2395, user-guide milestone MIP-2304).

`refresh_guide_notes` replaces a guide copy only while its text is one we shipped. The guide is rendered from
the vendored manual (MIP-2324), so every text the manual has had must be recorded, or an unedited copy of the
PREVIOUS manual text reads as the member's own edit and no manual change ever reaches an existing folder
(found in production on 2026-11-25: 96 notes "left alone" after the manual changed, 16 folders x 6 pages).

Pinned:
* every topic of every vendored manual copy is recorded (a new copy cannot land without its text: run
  `scripts/record_manual_shipped.py`);
* the record file is well formed and names only current topic titles;
* an unedited copy of an earlier manual text is replaced; the same copy is left alone when its text is not
  recorded; an edit of an earlier text is never touched.
"""

import copy
import json
import re
from unittest import mock

from django.test import SimpleTestCase

from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.services import guide_manual, guide_notes, guide_refresh
from origin.tests.test_guide_refresh import RefreshBase

LANGUAGES = ("en", "ja")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class RecordTests(SimpleTestCase):
    def test_every_topic_of_every_vendored_manual_is_recorded(self):
        seen = 0
        for lang in LANGUAGES:
            for title, body in guide_manual.manual_notes(lang) or []:
                self.assertIn(
                    guide_notes.body_fingerprint(body),
                    guide_refresh.shipped_fingerprints(lang, title),
                    f"{lang}: {title!r} is not recorded: run scripts/record_manual_shipped.py",
                )
                seen += 1
        self.assertGreater(seen, 30)

    def test_the_record_is_well_formed_and_names_only_current_titles(self):
        record = guide_refresh._shipped_manual()
        self.assertEqual({k for k in record if k != "_comment"}, set(LANGUAGES))
        for lang in LANGUAGES:
            current = {t["title"] for t in guide_manual.manual_topics(lang)}
            self.assertEqual(set(record[lang]), current, lang)
            for title, entry in record[lang].items():
                self.assertTrue(entry["sha256"], title)
                for fp in entry["sha256"]:
                    self.assertRegex(fp, HEX64)

    def test_the_manual_texts_do_not_replace_the_hand_written_record(self):
        # The old record still answers for the titles it knew.
        for lang in LANGUAGES:
            for title in guide_refresh._shipped()[lang]:
                old = set(guide_refresh._shipped()[lang][title]["sha256"])
                self.assertTrue(old <= guide_refresh.shipped_fingerprints(lang, title), title)


class EarlierManualTextTests(RefreshBase):
    """An earlier manual text is made by changing today's, then recorded (or not) the way the script would."""

    def _earlier(self, lang):
        title, body = next(iter(self.current(lang).items()))
        old = copy.deepcopy(body)
        old[0]["content"][0]["text"] += " (earlier wording)"
        return title, old

    def _record(self, lang, title, body):
        return mock.patch.object(
            guide_refresh,
            "_shipped_manual",
            return_value={lang: {title: {"sha256": [guide_notes.body_fingerprint(body)]}}},
        )

    def test_an_unedited_copy_of_an_earlier_manual_text_is_replaced(self):
        for lang in LANGUAGES:
            title, old = self._earlier(lang)
            folder = self.folder(lang, given=list(self.current(lang)))
            note = self.note(folder, title, old)
            with self._record(lang, title, old):
                dry = guide_refresh.refresh_guide_notes(apply=False)
                self.assertEqual((dry.replaced, dry.edited_left), (1, 0), lang)
                report = guide_refresh.refresh_guide_notes(apply=True)
            self.assertEqual((report.replaced, report.edited_left), (1, 0), lang)
            self.assertEqual(
                self.fp(note), guide_notes.body_fingerprint(self.current(lang)[title]), lang
            )
            PersonalNoteMaster.objects.filter(pk=note.pk).delete()
            folder.delete()

    def test_the_same_copy_is_left_alone_when_its_text_is_not_recorded(self):
        # What happened in production: the record had no manual texts, so an unedited copy looked edited.
        for lang in LANGUAGES:
            title, old = self._earlier(lang)
            folder = self.folder(lang, given=list(self.current(lang)))
            note = self.note(folder, title, old)
            with mock.patch.object(guide_refresh, "_shipped_manual", return_value={}):
                report = guide_refresh.refresh_guide_notes(apply=True)
            self.assertEqual((report.replaced, report.edited_left), (0, 1), lang)
            self.assertEqual(self.fp(note), guide_notes.body_fingerprint(old), lang)
            PersonalNoteMaster.objects.filter(pk=note.pk).delete()
            folder.delete()

    def test_an_edit_of_an_earlier_manual_text_is_never_touched(self):
        for lang in LANGUAGES:
            title, old = self._earlier(lang)
            edited = copy.deepcopy(old)
            edited[0]["content"][0]["text"] += " (my own words)"
            folder = self.folder(lang, given=list(self.current(lang)))
            note = self.note(folder, title, edited)
            before = PersonalNoteMaster.objects.values().get(pk=note.pk)
            with self._record(lang, title, old):
                report = guide_refresh.refresh_guide_notes(apply=True)
            self.assertEqual((report.replaced, report.edited_left), (0, 1), lang)
            self.assertEqual(PersonalNoteMaster.objects.values().get(pk=note.pk), before, lang)
            PersonalNoteMaster.objects.filter(pk=note.pk).delete()
            folder.delete()

    def test_the_record_file_is_json_the_service_can_read(self):
        # Guards the file itself (the lru_cache reads it once per process).
        raw = guide_refresh.SHIPPED_MANUAL_PATH.read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw), guide_refresh._shipped_manual())
