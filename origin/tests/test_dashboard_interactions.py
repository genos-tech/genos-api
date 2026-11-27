"""The interactive parts the server writes on a dashboard page (MIP-2452).

What is pinned, each by a test that fails when its guard is removed:

  * the script is one fixed string, the same on every page, and no data value reaches its text;
  * only a page the server drew gets it;
  * the saved page has no script and no control, and without script it reads as it did: what is
    held back or only works with script sits in `<template>`s, which search does not read;
  * a held-back row obeys the audience rule like a drawn one;
  * what the page holds for these parts moves no fingerprint and no chat notice;
  * held rows are capped, linked after the drawn ones, and filters appear only when they can help.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from html.parser import HTMLParser
from unittest import mock

from django.test import SimpleTestCase

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services import (
    dashboard_links,
    dashboard_page,
    dashboard_strings,
    html_note_pages,
    html_notes,
)
from origin.services.schedule_jobs import ai_dashboard, skill_dashboard
from origin.tests.test_schedule_jobs import JobBase

TODAY = date(2026, 11, 27)
SCRIPT = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL)
TEMPLATE = re.compile(r"<template[^>]*>.*?</template>", re.DOTALL)
HOSTILE = "\"><script>alert(1)</script> __proto__ ' onmouseover=x"


def render(blocks, **extra):
    dash = {
        "title": "T",
        "kind": "skill",
        "subtitle": "s",
        "stats": [],
        "today": TODAY.isoformat(),
        "blocks": blocks,
        **extra,
    }
    return dashboard_page.render(dash)[0]


def body_of(html):
    return html.split("</style>", 1)[1]


def row(n, owner="Aya", status="Open", days=1, **extra):
    return {
        "text": f"GEN-{n} · task number {n}",
        "ref": {"kind": "task", "id": n},
        "owner": owner,
        "status": status,
        "due": (TODAY + timedelta(days=days)).isoformat(),
        **extra,
    }


def timeline(rows, more_rows=(), **extra):
    return {"type": "timeline", "rows": rows, "more_rows": list(more_rows), **extra}


class Tags(HTMLParser):
    """Every start tag with its attributes."""

    def __init__(self, html):
        super().__init__()
        self.found = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.found.append((tag, dict(attrs)))

    def attrs(self, name):
        return [a[name] for _, a in self.found if name in a]


class ScriptTests(SimpleTestCase):
    def scripts(self, html):
        return SCRIPT.findall(html_note_pages.document(html, "ab" * 16, "cd" * 16))

    def test_the_script_is_one_fixed_text_on_every_page_and_holds_no_data(self):
        hostile = render(
            [timeline([row(n, owner=HOSTILE) for n in range(5)])],
            title=HOSTILE,
            locale="ja",
        )
        plain = render([{"type": "text", "heading": "h", "text": "words"}])
        first, second = self.scripts(hostile), self.scripts(plain)
        self.assertEqual(len(first), 2)  # the sentinel and the interactive parts
        self.assertEqual(first[1], html_note_pages.DASHBOARD_UI)
        self.assertEqual(second[1], html_note_pages.DASHBOARD_UI)
        # The sentinel's secret is not given to the second script.
        self.assertNotIn("ab" * 16, first[1])
        self.assertNotIn("task number", "".join(first))

    def test_the_script_writes_no_markup_and_reaches_nothing_outside_the_page(self):
        code = re.sub(r"/\*.*?\*/", "", html_note_pages.DASHBOARD_UI, flags=re.DOTALL)
        for banned in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "Function(",
            "setTimeout",
            "setInterval",
            "fetch(",
            "XMLHttpRequest",
            "WebSocket",
            "sendBeacon",
            "postMessage",
            "location",
            "localStorage",
            "sessionStorage",
            "cookie",
            "parent.",
            "top.",
            "window.open",
            ".href",
            ".src",
        ):
            self.assertNotIn(banned, code)
        self.assertNotIn("</", html_note_pages.DASHBOARD_UI)

    def test_a_page_that_is_not_the_servers_gets_no_script_of_ours_but_the_sentinel(self):
        doc = html_note_pages.document("<!doctype html><p data-gd-fold>x</p>", "ab" * 16)
        self.assertEqual(len(SCRIPT.findall(doc)), 1)
        self.assertNotIn(html_note_pages.DASHBOARD_UI, doc)

    def test_hostile_words_stay_attribute_values(self):
        html = render(
            [
                timeline([row(n, owner=HOSTILE if n % 2 else "__proto__") for n in range(6)]),
                {
                    "type": "workload",
                    "rows": [{"name": HOSTILE, "Open": 2}],
                },
                {"type": "bars", "rows": [{"text": HOSTILE, "value": 3}]},
            ]
        )
        self.assertNotIn("<script", html)
        tags = Tags(body_of(html))
        self.assertNotIn("script", [t for t, _ in tags.found])
        self.assertNotIn("onmouseover", [k for _, a in tags.found for k in a])
        self.assertEqual(sorted(set(tags.attrs("data-gd-who"))), ["p0", "p1"])
        self.assertIn(HOSTILE[:59], [v[:59] for v in tags.attrs("data-gd-name")])


class SavedPageTests(SimpleTestCase):
    def blocks(self):
        return [
            {
                "type": "attention",
                "groups": [
                    {
                        "reason": "overdue",
                        "count": 6,
                        "rows": [row(n) for n in range(3)],
                        "more_rows": [row(n, owner="Ben") for n in range(3, 6)],
                    }
                ],
            },
            timeline(
                [row(n) for n in range(10, 18)],
                [row(n, owner="Ben", status="WIP") for n in range(18, 21)],
                more=3,
            ),
            {"type": "done", "count": 9, "days": 30, "rows": [row(n) for n in range(30, 39)]},
        ]

    def test_the_saved_page_has_no_script_and_no_control_outside_a_template(self):
        html = render(self.blocks(), kind="milestone")
        self.assertNotIn("<script", html)
        seen = TEMPLATE.sub("", body_of(html))
        self.assertNotIn("<button", seen)
        self.assertNotIn("tabindex", seen)
        self.assertIsNone(re.search(r"\shidden[\s=>]", seen))

    def test_without_script_the_page_says_what_it_said_and_search_reads_no_held_row(self):
        html = render(self.blocks(), kind="milestone")
        text = html_notes.scan_html(html).text
        self.assertIn("task number 2", text)
        self.assertIn("+3 more", text)
        for held in (
            "task number 3\n",
            "task number 20",
            "task number 35",
            "Sort by",
            "Show fewer",
        ):
            self.assertNotIn(held, text + "\n")
        self.assertEqual(text, html_notes.scan_html(TEMPLATE.sub("", html)).text)
        self.assertEqual(
            [w["code"] for w in html_notes.scan_html(html).warnings],
            [],
        )

    def test_more_brings_out_the_rows_the_page_holds(self):
        body = body_of(render(self.blocks(), kind="milestone"))
        tags = Tags(body)
        lists = set(tags.attrs("id"))
        pointed = tags.attrs("data-gd-for")
        self.assertEqual(len(pointed), 3)
        self.assertTrue(set(pointed) <= lists)
        held = "".join(TEMPLATE.findall(body))
        for n in (3, 5, 18, 20, 35, 38):
            self.assertIn(f"task number {n}", held)
        # 9 closed, 5 drawn and 4 held: nothing is left unlisted.
        self.assertNotIn("data-gd-rest", held)
        self.assertEqual(render(self.blocks()), render(self.blocks()))

    def test_what_is_on_no_part_of_the_page_is_said(self):
        body = body_of(render([{"type": "done", "count": 40, "rows": [row(n) for n in range(10)]}]))
        self.assertIn('<li class="meta" data-gd-rest>30 more not listed here</li>', body)

    def test_a_page_with_nothing_held_has_no_more_button(self):
        body = body_of(render([{"type": "done", "count": 12, "rows": [row(n) for n in range(5)]}]))
        self.assertIn("+7 more", body)
        self.assertNotIn("data-gd-more", body)

    def test_every_headed_section_folds(self):
        body = body_of(render(self.blocks(), kind="milestone"))
        self.assertEqual(body.count("<h2 data-gd-fold>"), 3)
        self.assertEqual(body.count('<div class="gd-body" id="gd-'), 3)
        self.assertNotIn("<h2>", body)

    def test_chart_parts_say_their_number(self):
        body = body_of(
            render(
                [
                    {"type": "status", "counts": {"Open": 4, "Closed": 2}},
                    {"type": "workload", "rows": [{"name": "Aya", "WIP": 3}]},
                    {"type": "bars", "rows": [{"text": "Lead", "value": 31}]},
                    {"type": "hero", "done": 1, "total": 4},
                    {
                        "type": "trend",
                        "unit": "day",
                        "points": [{"label": "2026-11-01", "closed": 2, "created": 1}],
                    },
                ]
            )
        )
        titles = Tags(body).attrs("title")
        for words in (
            "4 Open",
            "2 Closed",
            "Aya: 3 WIP",
            "Lead: 31",
            "1 of 4 done",
            "Nov 1: 2 closed, 1 added",
        ):
            self.assertIn(words, titles)
        # The labels a screen reader reads stay.
        self.assertIn('aria-label="2 Closed, 4 Open"', body)
        self.assertIn('aria-label="closed: 2"', body)

    def test_a_charts_tooltip_uses_the_words_of_its_own_legend(self):
        trend = {
            "type": "trend",
            "unit": "day",
            "legend": ["won", "lost"],
            "points": [{"label": "2026-11-01", "closed": 2, "created": 1}],
        }
        body = body_of(render([trend]))
        self.assertIn("Nov 1: 2 won, 1 lost", Tags(body).attrs("title"))
        self.assertIn('aria-label="won: 2"', body)

    def test_filters_are_offered_only_when_they_can_help(self):
        def tools(block, **extra):
            return Tags(body_of(render([block], **extra)))

        same = [row(n) for n in range(6)]
        self.assertEqual(tools(timeline(same)).attrs("data-gd-act"), [])
        two = [row(n, owner="Aya" if n % 2 else None, status="WIP") for n in range(6)]
        found = tools(timeline(two), kind="milestone")
        self.assertEqual(found.attrs("data-gd-key"), ["due", "owner"])
        self.assertEqual(found.attrs("data-gd-dim"), ["who", "who"])
        # Three rows need none; a personal page has no people to choose from.
        self.assertEqual(tools(timeline(two[:3])).attrs("data-gd-act"), [])
        self.assertEqual(tools(timeline(two), kind="mine").attrs("data-gd-dim"), [])
        # A skill's rows say nobody's name, so only their status can be chosen.
        skill = [
            {"text": f"t{n}", "due": TODAY.isoformat(), "status": "WIP" if n % 2 else "Open"}
            for n in range(6)
        ]
        found = tools(timeline(skill))
        self.assertEqual(found.attrs("data-gd-key"), ["due", "status"])
        self.assertEqual(found.attrs("data-gd-dim"), ["st", "st"])

    def test_what_takes_the_keyboard_has_a_name(self):
        body = body_of(
            render(
                [{"type": "hero", "done": 1, "total": 4}, timeline([row(1)], heading="Due soon")]
            )
        )
        for tag, attrs in Tags(body).found:
            if "data-gd-tips" in attrs:
                self.assertTrue(attrs.get("aria-label"), tag)

    def test_the_more_words_and_their_separator_hide_together(self):
        block = timeline([row(n) for n in range(8)], [row(9)], more=1, undated=2)
        body = body_of(render([block]))
        self.assertIn("</span> · </span>2 more open with no due date", body)
        self.assertEqual(body.count("<span data-gd-lead>"), 1)

    def test_a_task_in_two_cards_counts_once_for_its_person(self):
        both = [row(1), row(2, owner="Ben")]
        group = {"count": 2, "rows": both}
        body = body_of(
            render(
                [
                    {
                        "type": "attention",
                        "groups": [{"reason": "overdue", **group}, {"reason": "blocked", **group}],
                    }
                ],
                kind="project",
            )
        )
        self.assertIn('<span class="gd-nm" dir="auto">Aya</span> <b>1</b>', body)
        self.assertNotIn("<b>2</b>", body)

    def test_a_table_with_rows_to_order_has_sortable_headers(self):
        table = {"type": "table", "columns": ["a", "b"], "rows": [["1", "2"]] * 3}
        self.assertEqual(Tags(body_of(render([table]))).attrs("data-gd-col"), ["0", "1"])
        short = {**table, "rows": [["1", "2"]] * 2}
        self.assertEqual(Tags(body_of(render([short]))).attrs("data-gd-col"), [])

    def test_the_new_words_are_in_every_language(self):
        keys = (
            "show_fewer",
            "more_elsewhere",
            "sort_by",
            "sort_due",
            "sort_status",
            "sort_owner",
            "filter_status",
            "filter_person",
            "clear_filters",
            "nothing_matches",
        )
        for locale, table in dashboard_strings.STRINGS.items():
            for key in keys:
                self.assertTrue(table.get(key), f"{locale} {key}")

    def test_a_skill_card_keeps_the_rows_past_three(self):
        answer = {
            "blocks": [
                {
                    "type": "attention",
                    "groups": [{"title": "t", "rows": [{"text": f"r{n}"} for n in range(15)]}],
                }
            ]
        }
        dash = skill_dashboard.parse_dashboard(json.dumps(answer), default_title="d")
        group = dash["blocks"][0]["groups"][0]
        self.assertEqual((len(group["rows"]), len(group["more_rows"])), (3, 12))
        html = dashboard_page.render(dash)[0]
        self.assertIn("r14", html)
        self.assertIn("+12 more", html)

    def test_held_rows_are_linked_after_every_row_in_sight(self):
        page = {
            "blocks": [
                {"rows": [{"ref": 1}], "more_rows": [{"ref": 3, "blocker": {"ref": 4}}]},
                {"rows": [{"ref": 2}]},
            ],
            "subject": {"ref": 0},
        }
        order = [h["ref"] for h in dashboard_links._holders(page)]
        self.assertEqual(order, [1, 2, 0, 3, 4])


class BuiltInPageTests(JobBase):
    def milestone(self):
        backing = self.make_task("M backing", is_milestone=True)
        return MilestoneMaster.objects.create(
            team=self.team, project=self.project, task=backing, title="M", reporter=self.user
        )

    def crowded(self, overdue=20, dated=40):
        milestone = self.milestone()
        today = self.local_today()
        for n in range(overdue):
            self.make_task(
                f"Late {n:02d}", milestone=milestone, due_date=today - timedelta(days=40 - n)
            )
        for n in range(dated - overdue):
            self.make_task(
                f"Later {n:02d}",
                milestone=milestone,
                due_date=today + timedelta(days=n + 1),
                assignee=self.user2 if n % 2 else self.user,
            )
        return milestone

    def dash(self, milestone):
        report = self.run_job("ai_dashboard", kind="milestone", milestone_id=milestone.milestone_id)
        return report, report["dashboard"]

    def test_held_rows_are_capped_and_the_page_stays_small(self):
        dash = self.dash(self.crowded(overdue=40, dated=60))[1]
        blocks = {b["type"]: b for b in dash["blocks"]}
        overdue = blocks["attention"]["groups"][0]
        self.assertEqual(overdue["count"], 40)
        self.assertEqual(len(overdue["rows"]), 3)
        self.assertEqual(len(overdue["more_rows"]), dashboard_page.MAX_EXTRA_ATTENTION)
        self.assertEqual(len(blocks["timeline"]["more_rows"]), dashboard_page.MAX_EXTRA_TIMELINE)
        self.assertIn("/task/", blocks["timeline"]["more_rows"][-1]["href"])
        html = dashboard_page.render(dash)[0]
        self.assertIn("25 more not listed here", html)  # 40 overdue, 15 on the card
        self.assertIn("30 more not listed here", html)  # 60 dated, 30 on the timeline
        self.assertLess(len(html.encode()), 100_000)

    def test_a_held_row_a_reader_may_not_open_is_nowhere_on_the_page(self):
        milestone = self.crowded()
        # Past the three a card draws and the eight the timeline draws.
        secret = TaskMaster.objects.get(title="Late 10")
        full = dashboard_page.render(self.dash(milestone)[1])[0]
        self.assertIn("Late 10", "".join(TEMPLATE.findall(full)))
        real = ai_dashboard.visible_tasks

        def without_it(ctx, ids, readers=None):
            return {i: t for i, t in real(ctx, ids, readers).items() if i != secret.task_id}

        with mock.patch.object(ai_dashboard, "visible_tasks", without_it):
            dash = self.dash(milestone)[1]
        self.assertNotIn("Late 10", json.dumps(dash["blocks"]))
        html = dashboard_page.render(dash)[0]
        self.assertNotIn("Late 10", html)
        self.assertNotIn(f'/task/{secret.task_id}"', html)

    def test_rows_say_who_has_them_except_on_my_progress(self):
        milestone = self.crowded()
        dash = self.dash(milestone)[1]
        rows = next(b for b in dash["blocks"] if b["type"] == "timeline")["more_rows"]
        self.assertIn(self.user.username, [r["owner"] for r in rows])
        mine = self.run_job("ai_dashboard", kind="mine")["dashboard"]
        self.assertNotIn('"owner"', json.dumps(mine["blocks"]))

    def test_what_the_filters_hold_moves_no_fingerprint_and_no_notice(self):
        report, dash = self.dash(self.crowded())

        def without(value):
            if isinstance(value, list):
                return [without(v) for v in value]
            if isinstance(value, dict):
                return {k: without(v) for k, v in value.items() if k not in ("owner", "more_rows")}
            return value

        before = without(dash)
        self.assertNotEqual(before, dash)
        self.assertEqual(ai_dashboard.data_fingerprint(before), ai_dashboard.data_fingerprint(dash))
        again = ai_dashboard.report_of(before)
        for key in ("title", "summary", "sections"):
            self.assertEqual(again[key], report[key])
