"""HTML notes, the service (MIP-2214): the cap, the scan, the placeholder, the rate limit, cleanup.

What is pinned:

- the size cap is in BYTES, is refused with a sentence that says what to do, and exactly the cap passes;
- the scan finds each thing the viewer blocks and gives a fix for each, finds nothing in a page that
  follows the guide, reads the visible text and the title (an SVG tooltip is not the title), and
  never lets a script's text into the visible text;
- the placeholder body is plain text (nothing a page says becomes a chip or a link) and the EXISTING
  chunker indexes it, so search finds an HTML note with no chunker change;
- each save is a revision, the oldest are dropped past the retention, an identical page still counts;
- 30 saves an hour per person, counted per person;
- every way a note row goes takes its pages with it (`forget_note_satellites`, the model delete, a
  batched delete).
"""

from __future__ import annotations

from django.core.cache import cache
from django.test import SimpleTestCase

from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.html_note_models import NoteHtmlRevision
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.search_engine.chunkers.note_chunker import iter_personal_note_chunks
from origin.services import html_notes
from origin.services.note_satellites import batched_note_cleanup, forget_note_satellites
from origin.tests.test_base import BaseAPITestCase

GOOD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekly report</title>
<style>body{font-family:system-ui;background:url(data:image/png;base64,AAAA)}</style></head>
<body><h1>Weekly report</h1><p>Closed 12 tasks.</p>
<svg viewBox="0 0 10 10"><title>tooltip only</title><text x="1" y="5">chart label</text></svg>
<script>const rows=[1,2,3]; document.title += ' ' + rows.length;</script></body></html>"""


def codes(scan: html_notes.HtmlScan) -> set[str]:
    return {w["code"] for w in scan.warnings}


def page(body: str, head: str = "") -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>T</title>{head}</head><body>{body}</body></html>"
    )


class SizeCapTests(SimpleTestCase):
    def test_exactly_the_cap_passes_and_one_byte_more_is_refused(self):
        at_cap = "x" * html_notes.MAX_HTML_BYTES
        self.assertEqual(html_notes.check_size(at_cap), html_notes.MAX_HTML_BYTES)
        with self.assertRaises(html_notes.HtmlNoteRefused):
            html_notes.check_size(at_cap + "x")

    def test_the_cap_counts_bytes_not_characters(self):
        # 3 bytes per character: a short-looking page can be over the cap.
        wide = "あ" * (html_notes.MAX_HTML_BYTES // 3 + 1)
        self.assertLess(len(wide), html_notes.MAX_HTML_BYTES)
        with self.assertRaises(html_notes.HtmlNoteRefused):
            html_notes.check_size(wide)

    def test_the_refusal_says_what_to_do_not_just_no(self):
        with self.assertRaises(html_notes.HtmlNoteRefused) as ctx:
            html_notes.check_size("x" * (html_notes.MAX_HTML_BYTES + 1))
        message = str(ctx.exception)
        self.assertIn("512 KB", message)
        self.assertIn("Nothing was saved", message)
        self.assertIn("Make it smaller", message)
        self.assertIn("200 KB", message)

    def test_a_lone_surrogate_does_not_crash_the_count(self):
        self.assertGreater(html_notes.byte_size("a\ud800b"), 0)

    def test_a_page_that_is_not_a_string_or_is_blank_is_refused_with_the_fix(self):
        for bad in (None, "", "   ", 5, ["<p>"]):
            with self.subTest(bad=bad), self.assertRaises(html_notes.HtmlNoteRefused) as ctx:
                html_notes.prepare(bad)
            self.assertIn("`html` is required", str(ctx.exception))


class ScanTests(SimpleTestCase):
    def test_a_page_that_follows_the_guide_has_nothing_to_fix(self):
        scan = html_notes.scan_html(GOOD_PAGE)
        self.assertEqual(scan.warnings, [])
        self.assertEqual(scan.title, "Weekly report")

    def test_each_blocked_thing_is_found_and_has_a_fix(self):
        cases = {
            "external_script": page('<script src="https://cdn.example.com/chart.js"></script>'),
            "external_css": page(
                "", '<link rel="stylesheet" href="https://cdn.example.com/tailwind.css">'
            ),
            "external_font": page(
                "",
                '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter">',
            ),
            "remote_image": page('<img src="https://example.com/a.png">'),
            "external_frame": page('<iframe src="https://example.com/x"></iframe>'),
            "network_call": page("<script>fetch('/api/data').then(r => r.json())</script>"),
            "form": page('<form action="/submit"><input name="q"></form>'),
            "window_open": page("<script>window.open('https://example.com')</script>"),
            "navigation": page("<script>location.href = 'https://example.com'</script>"),
            "storage": page("<script>localStorage.setItem('a', 1)</script>"),
        }
        for code, html in cases.items():
            with self.subTest(code=code):
                scan = html_notes.scan_html(html)
                self.assertIn(code, codes(scan), scan.warnings)
                found = next(w for w in scan.warnings if w["code"] == code)
                self.assertTrue(found["message"] and found["fix"])
                self.assertGreaterEqual(found["count"], 1)

    def test_more_ways_to_the_network_and_storage_are_found(self):
        cases = {
            "network_call": [
                "<script>new XMLHttpRequest()</script>",
                "<script>new WebSocket('wss://x')</script>",
                "<script>navigator.sendBeacon('/x')</script>",
                "<script>new EventSource('/x')</script>",
            ],
            "storage": [
                "<script>sessionStorage.x = 1</script>",
                "<script>document.cookie = 'a=b'</script>",
                "<script>indexedDB.open('db')</script>",
            ],
            "navigation": [
                "<script>window.location.assign('/x')</script>",
                "<script>top.location.replace('/x')</script>",
                "<script>document.location = '/x'</script>",
            ],
            "remote_image": [
                '<img srcset="a.png 1x, https://example.com/b.png 2x">',
                '<video poster="https://example.com/p.png"></video>',
                '<div style="background:url(https://example.com/a.png)"></div>',
                "<style>.a{background:url('https://example.com/a.png')}</style>",
            ],
            "external_font": [
                "<style>@font-face{font-family:F;src:url(https://example.com/f.woff2)}</style>",
                "<style>@import url('https://fonts.googleapis.com/css?family=X');</style>",
            ],
            "external_css": ["<style>@import 'https://example.com/a.css';</style>"],
        }
        for code, fragments in cases.items():
            for fragment in fragments:
                with self.subTest(code=code, fragment=fragment):
                    self.assertIn(code, codes(html_notes.scan_html(page(fragment))))

    def test_meta_refresh_is_a_navigation(self):
        scan = html_notes.scan_html(
            page("", '<meta http-equiv="refresh" content="0;url=https://example.com">')
        )
        self.assertIn("navigation", codes(scan))

    def test_inline_things_are_not_warned_about(self):
        scan = html_notes.scan_html(
            page(
                '<img src="data:image/png;base64,AAAA"><a href="#top">top</a>'
                "<script>if (a == b) { location.hash = 'x'; var h = location.href == 'y'; }</script>"
            )
        )
        self.assertEqual(scan.warnings, [])

    def test_a_warning_quotes_what_to_look_for_and_counts_repeats(self):
        scan = html_notes.scan_html(
            page(
                '<script src="https://a.example/1.js"></script>'
                '<script src="https://a.example/2.js"></script>'
                '<script src="https://a.example/3.js"></script>'
                '<script src="https://a.example/4.js"></script>'
            )
        )
        found = next(w for w in scan.warnings if w["code"] == "external_script")
        self.assertEqual(found["count"], 4)
        self.assertEqual(len(found["examples"]), html_notes.MAX_EXAMPLES)
        self.assertEqual(found["examples"][0], "https://a.example/1.js")

    def test_a_long_url_is_shortened_in_the_example(self):
        scan = html_notes.scan_html(page(f'<img src="https://x.example/{"a" * 500}.png">'))
        example = scan.warnings[0]["examples"][0]
        self.assertLessEqual(len(example), 80)

    def test_missing_doctype_and_title_are_advice_too(self):
        scan = html_notes.scan_html("<p>hi</p>")
        self.assertEqual(codes(scan), {"no_doctype", "no_title"})

    def test_the_title_is_the_first_outside_an_svg_and_text_skips_scripts_and_styles(self):
        scan = html_notes.scan_html(GOOD_PAGE)
        self.assertEqual(scan.title, "Weekly report")
        self.assertIn("Closed 12 tasks.", scan.text)
        self.assertIn("chart label", scan.text)
        self.assertNotIn("tooltip only", scan.text)
        self.assertNotIn("rows.length", scan.text)
        self.assertNotIn("font-family", scan.text)

    def test_an_unclosed_head_does_not_swallow_the_body_text(self):
        scan = html_notes.scan_html("<!doctype html><head><title>T</title><body><p>kept</p>")
        self.assertIn("kept", scan.text)

    def test_text_keeps_lines_and_collapses_spaces(self):
        scan = html_notes.scan_html(page("<h1>A   b</h1><p>c</p><p>d</p><br><br><br><p>e</p>"))
        self.assertEqual(scan.text, "A b\nc\nd\ne")

    def test_markup_the_parser_cannot_finish_still_gives_what_was_read(self):
        scan = html_notes.scan_html(page("<p>start</p><div <<<>>> <script"))
        self.assertIn("start", scan.text)

    def test_a_big_hostile_page_is_scanned_quickly(self):
        import time

        nasty = page(
            ("<script>" + "fetch(" * 2000 + "</script>") * 50 + "<p>" + "a" * 200_000 + "</p>"
        )
        started = time.monotonic()
        html_notes.scan_html(nasty)
        self.assertLess(time.monotonic() - started, 5)


class PrepareTests(SimpleTestCase):
    def test_title_comes_from_the_argument_then_the_page(self):
        self.assertEqual(html_notes.prepare(GOOD_PAGE).title, "Weekly report")
        self.assertEqual(html_notes.prepare(GOOD_PAGE, title="  Mine ").title, "Mine")

    def test_no_title_anywhere_is_refused_with_the_fix(self):
        with self.assertRaises(html_notes.HtmlNoteRefused) as ctx:
            html_notes.prepare("<p>hi</p>")
        self.assertIn("pass `title`", str(ctx.exception))

    def test_a_missing_summary_is_advice_not_an_error(self):
        without = html_notes.prepare(GOOD_PAGE)
        self.assertIn("no_summary", codes(without.scan))
        with_summary = html_notes.prepare(GOOD_PAGE, summary="  A weekly   status page. ")
        self.assertNotIn("no_summary", codes(with_summary.scan))
        self.assertEqual(with_summary.summary, "A weekly status page.")

    def test_hash_and_size_are_of_the_utf8_bytes(self):
        prepared = html_notes.prepare(GOOD_PAGE, summary="s")
        self.assertEqual(prepared.size, len(GOOD_PAGE.encode()))
        import hashlib

        self.assertEqual(prepared.sha256, hashlib.sha256(GOOD_PAGE.encode()).hexdigest())

    def test_suggestions_are_the_fixes_in_order(self):
        scan = html_notes.scan_html(page('<img src="https://x/a.png"><form></form>'))
        self.assertEqual(
            html_notes.suggestions_for(scan.warnings), [w["fix"] for w in scan.warnings]
        )


def block_text(block) -> str:
    return "".join(part.get("text", "") for part in block.get("content", []))


class PlaceholderTests(SimpleTestCase):
    def test_label_summary_then_each_line_of_text_as_plain_paragraphs(self):
        body = html_notes.placeholder_body("Report", "Weekly status.", "line one\nline two")
        self.assertEqual(
            [block_text(b) for b in body],
            ["HTML page: Report", "Weekly status.", "line one", "line two"],
        )
        self.assertTrue(all(b["type"] == "paragraph" for b in body))
        self.assertTrue(all(b["id"] for b in body))

    def test_nothing_the_page_says_becomes_a_chip_or_a_link(self):
        evil = "[click](task:1) @everyone #MIP-1 **bold** <b>x</b> [[note:2]]"
        body = html_notes.placeholder_body("T", evil, evil)
        for block in body:
            for part in block["content"]:
                self.assertEqual(part["type"], "text")
        self.assertIn(evil, [block_text(b) for b in body])

    def test_a_long_page_is_capped_and_says_so(self):
        text = "\n".join(f"line {i}" for i in range(20_000))
        body = html_notes.placeholder_body("T", "", text)
        total = sum(len(block_text(b)) for b in body)
        self.assertLess(total, html_notes.MAX_TEXT_CHARS + 500)
        self.assertIn("more text than this preview holds", block_text(body[-1]))

    def test_blank_lines_make_no_blocks(self):
        body = html_notes.placeholder_body("", "", "a\n\n\nb")
        self.assertEqual([block_text(b) for b in body], ["HTML page", "a", "b"])


class RateLimitTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_thirty_an_hour_then_a_refusal_that_says_when(self):
        for _ in range(html_notes.SAVES_PER_HOUR):
            html_notes.check_rate("user-a")
        with self.assertRaises(html_notes.HtmlNoteRefused) as ctx:
            html_notes.check_rate("user-a")
        message = str(ctx.exception)
        self.assertIn("30", message)
        self.assertIn("Nothing was saved", message)
        self.assertIn("minutes", message)

    def test_it_is_counted_per_person(self):
        for _ in range(html_notes.SAVES_PER_HOUR):
            html_notes.check_rate("user-a")
        html_notes.check_rate("user-b")  # does not raise


class _Notes(BaseAPITestCase):
    def make(self, title="Page", html=GOOD_PAGE, summary="s"):
        prepared = html_notes.prepare(html, title=title, summary=summary)
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title=prepared.title, body=prepared.body
        )
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=note.note_id, note_type=1, role_id=1
        )
        html_notes.mark_html(1, note.note_id)
        html_notes.append_revision(1, note.note_id, str(self.user.id), prepared)
        return PersonalNoteMaster.objects.get(note_id=note.note_id), prepared


class RevisionTests(_Notes):
    def test_the_first_save_is_revision_one_and_marks_the_note(self):
        note, prepared = self.make()
        self.assertEqual(note.body_format, "html")
        rev = html_notes.latest_revision(1, note.note_id)
        self.assertEqual((rev.rev, rev.size, rev.sha256), (1, prepared.size, prepared.sha256))
        self.assertEqual(rev.html, GOOD_PAGE)
        self.assertEqual(rev.created_by_id, self.user.id)

    def test_every_save_adds_a_revision_even_for_the_same_page(self):
        note, prepared = self.make()
        html_notes.append_revision(1, note.note_id, str(self.user.id), prepared)
        html_notes.append_revision(1, note.note_id, str(self.user.id), prepared)
        revs = list(
            NoteHtmlRevision.objects.filter(note_type=1, note_id=note.note_id).order_by("rev")
        )
        self.assertEqual([r.rev for r in revs], [1, 2, 3])

    def test_only_the_newest_revisions_are_kept(self):
        note, prepared = self.make()
        for _ in range(html_notes.KEEP_REVISIONS + 5):
            html_notes.append_revision(1, note.note_id, str(self.user.id), prepared)
        revs = list(
            NoteHtmlRevision.objects.filter(note_type=1, note_id=note.note_id)
            .order_by("rev")
            .values_list("rev", flat=True)
        )
        self.assertEqual(len(revs), html_notes.KEEP_REVISIONS)
        self.assertEqual(revs[-1], html_notes.KEEP_REVISIONS + 6)

    def test_revisions_of_another_note_or_family_are_not_touched(self):
        note, prepared = self.make()
        other, _ = self.make(title="Other")
        for _ in range(html_notes.KEEP_REVISIONS + 3):
            html_notes.append_revision(1, note.note_id, str(self.user.id), prepared)
        self.assertEqual(NoteHtmlRevision.objects.filter(note_id=other.note_id).count(), 1)

    def test_replace_page_updates_the_note_history_and_title(self):
        from origin.models.note.version_note_models import NoteVersionMaster

        note, _ = self.make(title="Old")
        newer = html_notes.prepare(page("<p>new text</p>"), title="New", summary="n")
        html_notes.replace_page(
            note_type=1, note=note, user=self.user, prepared=newer, keep_title=False
        )
        note.refresh_from_db()
        self.assertEqual((note.title, note.body_format), ("New", "html"))
        self.assertIn("new text", [block_text(b) for b in note.body])
        self.assertEqual(html_notes.latest_revision(1, note.note_id).rev, 2)
        self.assertTrue(
            NoteVersionMaster.objects.filter(note_type=1, note_id=note.note_id).exists()
        )

    def test_replace_page_can_keep_the_title(self):
        note, _ = self.make(title="Keep me")
        newer = html_notes.prepare(page("<p>x</p>"), summary="n")
        html_notes.replace_page(
            note_type=1, note=note, user=self.user, prepared=newer, keep_title=True
        )
        note.refresh_from_db()
        self.assertEqual(note.title, "Keep me")

    def test_body_format_of_a_note_and_of_a_missing_one(self):
        note, _ = self.make()
        self.assertEqual(html_notes.body_format_of(1, note.note_id), "html")
        plain = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="p", body=[]
        )
        self.assertEqual(html_notes.body_format_of(1, plain.note_id), "blocknote")
        self.assertEqual(html_notes.body_format_of(1, 99999999), "blocknote")


class SearchSeesThePlaceholderTests(_Notes):
    """The placeholder is how search finds an HTML note: the EXISTING chunker indexes it."""

    def test_the_personal_note_chunker_indexes_summary_and_visible_text(self):
        note, _ = self.make(
            title="Burndown",
            html=page("<h1>Sprint burndown</h1><p>Velocity dropped in week three.</p>"),
            summary="A burndown chart of sprint 15.",
        )
        batches = [
            b for b in iter_personal_note_chunks() if b.entity_id == f"note:personal:{note.note_id}"
        ]
        self.assertEqual(len(batches), 1)
        text = "\n".join(c.search_text for c in batches[0].chunks)
        self.assertIn("Burndown", text)
        self.assertIn("A burndown chart of sprint 15.", text)
        self.assertIn("Velocity dropped in week three.", text)

    def test_the_acl_on_the_chunks_is_the_notes_own(self):
        note, _ = self.make()
        batch = next(
            b for b in iter_personal_note_chunks() if b.entity_id == f"note:personal:{note.note_id}"
        )
        self.assertEqual(batch.chunks[0].acl_user_ids, [str(self.user.id)])

    def test_script_text_is_not_searchable(self):
        note, _ = self.make(
            html=page("<p>visible</p><script>const secretToken = 'abc123';</script>")
        )
        batch = next(
            b for b in iter_personal_note_chunks() if b.entity_id == f"note:personal:{note.note_id}"
        )
        text = "\n".join(c.search_text for c in batch.chunks)
        self.assertIn("visible", text)
        self.assertNotIn("secretToken", text)


class PurgeTests(_Notes):
    """A note's pages die with it, on every path a note row can go."""

    def count(self, note_id):
        return NoteHtmlRevision.objects.filter(note_type=1, note_id=note_id).count()

    def test_forget_note_satellites_removes_the_pages(self):
        note, _ = self.make()
        self.assertEqual(self.count(note.note_id), 1)
        forget_note_satellites(1, [note.note_id])
        self.assertEqual(self.count(note.note_id), 0)

    def test_deleting_the_note_row_removes_the_pages(self):
        note, _ = self.make()
        PersonalNoteMaster.objects.filter(note_id=note.note_id).delete()
        self.assertEqual(self.count(note.note_id), 0)

    def test_a_batched_delete_removes_the_pages_of_every_note_in_one_pass(self):
        first, _ = self.make(title="a")
        second, _ = self.make(title="b")
        with batched_note_cleanup():
            PersonalNoteMaster.objects.filter(note_id__in=[first.note_id, second.note_id]).delete()
            # recorded, not yet deleted
            self.assertEqual(NoteHtmlRevision.objects.count(), 2)
        self.assertEqual(NoteHtmlRevision.objects.count(), 0)

    def test_another_notes_pages_and_another_family_with_the_same_number_stay(self):
        keep, _ = self.make(title="keep")
        gone, _ = self.make(title="gone")
        NoteHtmlRevision.objects.create(
            note_type=2, note_id=gone.note_id, rev=1, html="<p>", sha256="x", size=3
        )
        forget_note_satellites(1, [gone.note_id])
        self.assertEqual(self.count(keep.note_id), 1)
        self.assertEqual(
            NoteHtmlRevision.objects.filter(note_type=2, note_id=gone.note_id).count(), 1
        )

    def test_the_account_erasure_shape_removes_the_pages(self):
        # `account_deletion` deletes a person's notes inside `batched_note_cleanup`.
        note, _ = self.make()
        with batched_note_cleanup():
            PersonalNoteMaster.objects.filter(owner=self.user).delete()
        self.assertEqual(self.count(note.note_id), 0)

    def test_a_revision_outlives_a_deleted_author_and_forgets_who_saved_it(self):
        note, _ = self.make()
        NoteHtmlRevision.objects.filter(note_id=note.note_id).update(created_by=self.user2)
        self.user2.delete()
        rev = NoteHtmlRevision.objects.get(note_id=note.note_id)
        self.assertIsNone(rev.created_by_id)
