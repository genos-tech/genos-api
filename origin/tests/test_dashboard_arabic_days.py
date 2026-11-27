"""An Arabic dashboard page says "N days late" and "N days left" as a phrase with the right plural
form, not as "label: number" (MIP-2467)."""

from __future__ import annotations

from datetime import date, timedelta

from django.test import SimpleTestCase

from origin.services.dashboard_strings import text
from origin.tests.test_dashboard_blocks import TODAY, draw


class ArabicDayCountTests(SimpleTestCase):
    def test_days_late_takes_each_plural_form(self):
        said = {n: text("ar", "days_late", n=n) for n in (1, 2, 3, 10, 11, 99, 100, 103)}
        self.assertEqual(
            said,
            {
                1: "تأخير يوم واحد",
                2: "تأخير يومين",
                3: "تأخير 3 أيام",
                10: "تأخير 10 أيام",
                11: "تأخير 11 يومًا",
                99: "تأخير 99 يومًا",
                100: "تأخير 100 يوم",
                103: "تأخير 103 أيام",
            },
        )

    def test_days_left_takes_each_plural_form(self):
        said = [text("ar", "days_left", n=n) for n in (1, 2, 6, 11)]
        self.assertEqual(said, ["متبقٍ يوم واحد", "متبقٍ يومان", "متبقٍ 6 أيام", "متبقٍ 11 يومًا"])

    def test_the_other_languages_still_have_one_and_many(self):
        self.assertEqual(text("en", "days_late", n=1), "1 day late")
        self.assertEqual(text("en", "days_late", n=2), "2 days late")
        self.assertEqual(text("ja", "days_late", n=2), "2日超過")

    def test_the_timeline_of_an_arabic_page_says_it(self):
        today: date = TODAY
        rows = [
            {"text": f"task {n}", "due": (today - timedelta(days=n)).isoformat()}
            for n in (1, 2, 11)
        ]
        page = draw([{"type": "timeline", "rows": rows}], locale="ar")
        for said in ("تأخير يوم واحد", "تأخير يومين", "تأخير 11 يومًا"):
            self.assertIn(said, page)
