"""MIP-2344: an agent reading an HTML note gets a diagram's description, and a diagram without one
earns a fixable warning (never a refusal)."""

from django.test import SimpleTestCase

from origin.services import html_notes

HEAD = '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>T</title></head>'


def page(body: str) -> str:
    return f"{HEAD}<body>{body}</body></html>"


def codes(scan: html_notes.HtmlScan) -> set[str]:
    return {w["code"] for w in scan.warnings}


class TestDiagramTextIsRead(SimpleTestCase):
    def test_svg_title_and_desc_are_in_the_text(self):
        scan = html_notes.scan_html(
            page(
                "<svg viewBox='0 0 9 9'><title>Request path</title>"
                "<desc>frontend -&gt; genos-api -&gt; Postgres</desc><text>box</text></svg>"
            )
        )
        self.assertIn("Request path", scan.text)
        self.assertIn("frontend -> genos-api -> Postgres", scan.text)
        self.assertEqual(scan.title, "T")  # the page title is still the first one outside an svg

    def test_aria_label_of_svg_figure_and_canvas_is_in_the_text(self):
        scan = html_notes.scan_html(
            page(
                "<svg aria-label='svg says a to b'></svg>"
                "<figure aria-label='figure says c to d'><p>x</p></figure>"
                "<canvas aria-label='canvas says e to f'></canvas>"
            )
        )
        for words in ("svg says a to b", "figure says c to d", "canvas says e to f"):
            self.assertIn(words, scan.text)

    def test_figcaption_is_in_the_text(self):
        scan = html_notes.scan_html(
            page("<figure><svg></svg><figcaption>a feeds b</figcaption></figure>")
        )
        self.assertIn("a feeds b", scan.text)

    def test_the_stored_placeholder_body_carries_it(self):
        scan = html_notes.scan_html(page("<svg><title>a to b</title></svg>"))
        body = html_notes.placeholder_body("T", "summary", scan.text)
        self.assertIn("a to b", str(body))


class TestUndescribedDiagramWarning(SimpleTestCase):
    def test_a_bare_svg_or_canvas_warns_with_a_fix(self):
        for body in ("<svg viewBox='0 0 1 1'><text>x</text></svg>", "<canvas></canvas>"):
            scan = html_notes.scan_html(page(body))
            self.assertIn("diagram_no_description", codes(scan), body)
            warning = next(w for w in scan.warnings if w["code"] == "diagram_no_description")
            self.assertIn("Postgres", warning["fix"])
            self.assertIn("structure", warning["fix"])

    def test_each_kind_of_description_clears_it(self):
        for body in (
            "<svg><title>a to b</title></svg>",
            "<svg><desc>a to b</desc></svg>",
            "<svg aria-label='a to b'></svg>",
            "<canvas aria-label='a to b'></canvas>",
            "<figure><canvas></canvas><figcaption>a to b</figcaption></figure>",
            "<figure aria-label='a to b'><svg></svg></figure>",
        ):
            self.assertNotIn(
                "diagram_no_description", codes(html_notes.scan_html(page(body))), body
            )

    def test_an_empty_label_is_not_a_description_and_a_page_without_diagrams_is_clean(self):
        self.assertIn(
            "diagram_no_description",
            codes(html_notes.scan_html(page("<svg aria-label='  '><title> </title></svg>"))),
        )
        self.assertNotIn("diagram_no_description", codes(html_notes.scan_html(page("<p>hi</p>"))))

    def test_only_the_undescribed_diagram_is_counted(self):
        scan = html_notes.scan_html(page("<svg><title>ok</title></svg><svg></svg><svg></svg>"))
        warning = next(w for w in scan.warnings if w["code"] == "diagram_no_description")
        self.assertEqual(warning["count"], 2)

    def test_a_nested_svg_title_describes_the_inner_svg_only(self):
        scan = html_notes.scan_html(page("<svg><svg><title>inner</title></svg></svg>"))
        warning = next(w for w in scan.warnings if w["code"] == "diagram_no_description")
        self.assertEqual(warning["count"], 1)

    def test_hostile_nesting_stays_bounded_and_does_not_raise(self):
        scan = html_notes.scan_html(page("<svg><title>" * 5000 + "<figure>" * 5000))
        self.assertLessEqual(len(scan.text), html_notes.MAX_TEXT_CHARS * 3)
