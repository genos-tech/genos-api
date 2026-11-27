"""Mostly visual dashboards with a layout per kind (MIP-2420).

What is pinned, each by a test that fails when its guard is removed:

  * a milestone, a project and "my progress" are drawn from different blocks;
  * a task one reader may not open is in no block and no count of the open work;
  * the verdict is only given when the data supports it;
  * the page escapes every word, holds no script and warns about nothing, in every language;
  * a skill picks blocks from the fixed set, each cut to its limits; unknown ones are ignored;
  * data in the old shape still makes a page;
  * the fingerprint covers the blocks and does not move with the date alone;
  * the chat notice is word for word what it was.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from unittest import mock

from django.test import SimpleTestCase

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services import dashboard_links, dashboard_page, dashboard_strings, html_notes
from origin.services.schedule_jobs import ai_dashboard, dashboard_blocks, skill_dashboard
from origin.tests.test_schedule_jobs import JobBase

TODAY = date(2026, 11, 27)


def types(dash):
    return [b["type"] for b in dash["blocks"]]


def draw(blocks, **extra):
    dash = {
        "title": "T",
        "kind": "skill",
        "subtitle": "s",
        "stats": [],
        "today": TODAY.isoformat(),
        "blocks": blocks,
        **extra,
    }
    return dashboard_page.render(dash)[0].split("</style>", 1)[1]


class LayoutTests(JobBase):
    def milestone(self, title="M1"):
        backing = self.make_task(f"{title} backing", is_milestone=True)
        return MilestoneMaster.objects.create(
            team=self.team, project=self.project, task=backing, title=title, reporter=self.user
        )

    def work(self):
        milestone = self.milestone()
        today = self.local_today()
        late = self.make_task("Late one", milestone=milestone, due_date=today - timedelta(days=2))
        blocker = self.make_task("The blocker", milestone=milestone, status="WIP")
        blocked = self.make_task("Waits", milestone=milestone, due_date=today + timedelta(days=3))
        TaskDependency.objects.create(blocker_task=blocker, blocked_task=blocked)
        done = self.make_task("Finished", milestone=milestone, status="Closed")
        self.activity(done, self.user, "status_changed", "WIP", "Closed", at=self.ago(hours=3))
        return milestone, late, blocker, blocked, done

    def dash(self, kind, **params):
        return self.run_job("ai_dashboard", kind=kind, **params)["dashboard"]

    def test_the_three_kinds_have_three_layouts(self):
        milestone = self.work()[0]
        by_kind = {
            "milestone": types(self.dash("milestone", milestone_id=milestone.milestone_id)),
            "project": types(self.dash("project", project_id=self.project.project_id)),
            "mine": types(self.dash("mine")),
        }
        self.assertEqual(
            by_kind["milestone"],
            ["hero", "status", "attention", "timeline", "workload", "done"],
        )
        self.assertEqual(
            by_kind["project"],
            ["numbers", "meters", "status", "trend", "workload", "attention", "done"],
        )
        self.assertEqual(by_kind["mine"], ["headline", "attention", "timeline", "done"])

    def test_a_milestone_page_shows_its_progress_what_is_late_and_what_waits_on_what(self):
        milestone, late, blocker, blocked, _done = self.work()
        dash = self.dash("milestone", milestone_id=milestone.milestone_id)
        blocks = {b["type"]: b for b in dash["blocks"]}
        self.assertEqual((blocks["hero"]["done"], blocks["hero"]["total"]), (1, 4))
        groups = {g["reason"]: g for g in blocks["attention"]["groups"]}
        self.assertEqual(groups["overdue"]["rows"][0]["ref"]["id"], late.task_id)
        waiting = groups["blocked"]["rows"][0]
        self.assertEqual(waiting["ref"]["id"], blocked.task_id)
        self.assertEqual(waiting["blocker"]["ref"]["id"], blocker.task_id)
        self.assertIn("/task/", waiting["blocker"]["href"])
        html = dashboard_page.render(dash)[0]
        self.assertIn("25%", html)
        self.assertIn("1 of 4 done", html)
        self.assertEqual(html_notes.scan_html(html).warnings, [])

    def test_an_empty_milestone_says_so_and_draws_nothing_else(self):
        dash = self.dash("milestone", milestone_id=self.milestone().milestone_id)
        self.assertEqual(types(dash), ["hero"])
        self.assertIn("No tasks yet", dashboard_page.render(dash)[0])

    def test_one_person_and_sixty_tasks(self):
        before = self.open_count(self.dash("project", project_id=self.project.project_id))
        for n in range(60):
            self.make_task(f"Task {n} " + "とても長い名前" * 20)
        dash = self.dash("project", project_id=self.project.project_id)
        blocks = {b["type"]: b for b in dash["blocks"]}
        self.assertEqual(len(blocks["workload"]["rows"]), 1)
        self.assertEqual(self.open_count(dash), before + 60)
        html = dashboard_page.render(dash)[0]
        self.assertEqual(html_notes.scan_html(html).warnings, [])
        self.assertLess(len(html.encode()), 60_000)

    @staticmethod
    def open_count(dash):
        return sum(
            n
            for block in dash["blocks"]
            if block["type"] == "workload"
            for row in block["rows"]
            for key, n in row.items()
            if key != "name"
        )

    def test_a_task_a_reader_may_not_open_is_in_no_block_and_no_open_count(self):
        milestone = self.work()[0]
        hidden = TaskMaster.objects.get(title="Late one")
        real = ai_dashboard.visible_tasks

        def without_it(ctx, ids, readers=None):
            return {i: t for i, t in real(ctx, ids, readers).items() if i != hidden.task_id}

        kinds = (
            ("milestone", {"milestone_id": milestone.milestone_id}),
            ("project", {"project_id": self.project.project_id}),
            ("mine", {}),
        )
        for kind, params in kinds:
            full = self.dash(kind, **params)
            with mock.patch.object(ai_dashboard, "visible_tasks", without_it):
                dash = self.dash(kind, **params)
            self.assertIn("Late one", json.dumps(full["blocks"]), kind)
            self.assertNotIn("Late one", json.dumps(dash["blocks"]), kind)
            if kind != "mine":
                self.assertEqual(self.open_count(dash), self.open_count(full) - 1, kind)

    def test_someone_who_left_the_team_is_not_named(self):
        gone = str(uuid.uuid4())
        self.assertEqual(dashboard_blocks._names({gone}, self.team.team_id), {})

    def test_the_page_is_written_in_the_owners_language(self):
        milestone = self.work()[0]
        for locale, word in (("ja", "マイルストーン"), ("ar", "مَعلَم"), ("zh", "里程碑")):
            self.user.language = locale
            self.user.save(update_fields=["language"])
            dash = self.dash("milestone", milestone_id=milestone.milestone_id)
            html = dashboard_page.render(dash)[0]
            self.assertIn(f'<html lang="{locale}"', html)
            self.assertIn(word, html)
        self.assertIn('dir="rtl"', dashboard_page.render({**dash, "locale": "ar"})[0])

    def test_the_fingerprint_covers_the_blocks_and_not_the_date_alone(self):
        milestone = self.work()[0]
        dash = self.dash("milestone", milestone_id=milestone.milestone_id)
        again = {**dash, "today": "2099-01-01", "locale": "ja"}
        self.assertEqual(ai_dashboard.data_fingerprint(dash), ai_dashboard.data_fingerprint(again))
        TaskMaster.objects.filter(title="Waits").update(title="Waits for more")
        changed = {**dash, "blocks": [{**dash["blocks"][0], "done": 2}, *dash["blocks"][1:]]}
        self.assertNotEqual(
            ai_dashboard.data_fingerprint(dash), ai_dashboard.data_fingerprint(changed)
        )

    def test_a_day_on_which_nothing_happens_moves_no_fingerprint(self):
        milestone = self.work()[0]
        for kind, params in (
            ("milestone", {"milestone_id": milestone.milestone_id}),
            ("project", {"project_id": self.project.project_id}),
            ("mine", {}),
        ):
            first = self.dash(kind, **params)
            self.now += timedelta(days=1)
            second = self.dash(kind, **params)
            self.now -= timedelta(days=1)
            self.assertEqual(
                ai_dashboard.data_fingerprint(first),
                ai_dashboard.data_fingerprint(second),
                kind,
            )

    def test_finished_milestones_do_not_use_up_the_room_of_the_running_ones(self):
        for n in range(3):
            done = self.milestone(f"Old {n}")
            done.status, done.due_date = "Closed", self.local_today() - timedelta(days=400)
            done.save(update_fields=["status", "due_date"])
        running = self.milestone("Running")
        self.make_task("Left", milestone=running)
        with mock.patch.object(dashboard_blocks, "MAX_MILESTONES", 2):
            blocks = self.dash("project", project_id=self.project.project_id)["blocks"]
        rows = next(b for b in blocks if b["type"] == "meters")["rows"]
        self.assertIn("Running", [r["text"] for r in rows])

    def test_a_close_just_past_the_last_day_is_not_lost_from_the_pace(self):
        trend = dashboard_blocks.pace(
            [{"label": "2026-11-28", "closed": 2, "created": 0}], "week", TODAY, 7
        )
        self.assertEqual(trend["points"][-1]["closed"], 2)

    def test_the_chat_notice_and_the_inbox_rows_are_made_from_the_same_data_as_before(self):
        milestone = self.work()[0]
        report = self.run_job("ai_dashboard", kind="milestone", milestone_id=milestone.milestone_id)
        today = self.local_today().isoformat()
        self.assertEqual(
            report["summary"],
            f"Last 30 days, as of {today}: 4 tasks, 25% closed, 1 overdue, 1 closed this month.",
        )
        self.assertEqual(
            [s["heading"] for s in report["sections"]],
            ["Numbers", "Waiting on blockers", "Ready to start", "Recently closed"],
        )


class VerdictTests(SimpleTestCase):
    def of(self, done=10, total=20, due=12, created=30, closed=6):
        return dashboard_page.verdict(
            done=done,
            total=total,
            due=None if due is None else TODAY + timedelta(days=due),
            today=TODAY,
            created=TODAY - timedelta(days=created),
            closed_recently=closed,
        )[0]

    def test_it_is_given_only_when_the_data_supports_it(self):
        self.assertEqual(self.of(), "risk")
        self.assertEqual(self.of(due=30), "ontrack")
        self.assertEqual(self.of(due=21), "tight")
        self.assertEqual(self.of(due=-1), "past")
        self.assertEqual(self.of(done=20), "done")
        self.assertEqual(self.of(closed=0), "stalled")
        # Not enough to guess from: silent.
        self.assertIsNone(self.of(closed=2))
        self.assertIsNone(self.of(created=3))
        self.assertIsNone(self.of(closed=0, due=90))
        self.assertIsNone(self.of(due=None))
        self.assertIsNone(self.of(total=0, done=0))


class RenderTests(SimpleTestCase):
    EVIL = '<img src=x onerror="a">'

    def test_every_word_of_every_block_is_escaped(self):
        row = {"text": self.EVIL, "href": "https://evil.example", "note": self.EVIL}
        body = draw(
            [
                {"type": "hero", "label": self.EVIL, "done": 1, "total": 2},
                {"type": "numbers", "items": [{"label": self.EVIL, "value": self.EVIL}]},
                {"type": "status", "heading": self.EVIL, "counts": {"Open": 1, self.EVIL: 3}},
                {"type": "bars", "rows": [{**row, "value": 2}]},
                {"type": "meters", "rows": [{**row, "done": 1, "total": 2, "due": self.EVIL}]},
                {"type": "workload", "rows": [{"name": self.EVIL, "Open": 1}]},
                {"type": "timeline", "rows": [{**row, "due": "2026-12-01", "status": self.EVIL}]},
                {
                    "type": "attention",
                    "groups": [{"title": self.EVIL, "reason": self.EVIL, "rows": [row]}],
                },
                {
                    "type": "trend",
                    "legend": [self.EVIL, self.EVIL],
                    "points": [{"label": self.EVIL, "closed": 1, "created": 1}],
                },
                {"type": "table", "columns": [self.EVIL], "rows": [[row]]},
                {"type": "list", "rows": [row]},
                {"type": "text", "text": self.EVIL},
                {"type": "done", "rows": [row]},
                {"type": "script", "text": "x"},
            ],
            narrative=self.EVIL,
            focus=self.EVIL,
            note=self.EVIL,
        )
        self.assertNotIn("<img", body)
        self.assertNotIn("<script", body)
        self.assertNotIn("evil.example", body)

    def test_every_language_has_every_word(self):
        english = set(dashboard_strings.STRINGS["en"])
        for locale, table in dashboard_strings.STRINGS.items():
            missing = {k for k in english - set(table) if not k.endswith("_one")}
            self.assertEqual(missing, set(), locale)
            self.assertEqual(set(table) - english, set(), locale)
            for key in table:
                dashboard_strings.text(
                    locale, key, n=2, days=3, date="d", done=1, total=2, text="t"
                )

    def test_a_written_summary_is_cut_to_two_sentences(self):
        body = draw([], narrative="One. Two! Three.")
        self.assertIn("One. Two!", body)
        self.assertNotIn("Three", body)
        self.assertIn("一。二。", draw([], narrative="一。二。三。"))

    def test_data_in_the_old_shape_still_makes_a_page(self):
        html = dashboard_page.render(
            {
                "title": "Old",
                "kind": "project",
                "subtitle": "Last 30 days, as of 2026-11-25",
                "stats": [{"label": "tasks", "value": "3"}],
                "breakdown": [{"label": "Open", "count": 3}],
                "series": [{"label": "2026-11-20", "closed": 1, "created": 2}],
                "lists": [{"heading": "Recently closed", "rows": ["T-1 · A"]}],
            }
        )[0]
        for expected in ("tasks", "s-open", 'class="chart"', "Recently closed", "T-1"):
            self.assertIn(expected, html)
        self.assertIn("Last 30 days, as of 2026-11-25", html)


class SkillBlockTests(SimpleTestCase):
    def parse(self, blocks, **extra):
        answer = json.dumps({"title": "S", "blocks": blocks, **extra})
        return skill_dashboard.parse_dashboard(answer, default_title="d")

    def test_two_skills_can_make_two_different_layouts(self):
        one = self.parse(
            [
                {"type": "bars", "rows": [{"text": "a", "value": 1}]},
                {"type": "table", "columns": ["c"], "rows": [["x"]]},
            ]
        )
        two = self.parse(
            [
                {"type": "text", "text": "t"},
                {"type": "timeline", "rows": [{"text": "a", "due": "2026-12-01"}]},
                {"type": "hero", "done": 3, "total": 5, "heading": "Top five"},
            ]
        )
        self.assertEqual(types(one), ["bars", "table"])
        # The skill's order, with the progress ring leading.
        self.assertEqual(types(two), ["hero", "text", "timeline"])

    def test_unknown_blocks_and_empty_ones_are_ignored_and_limits_hold(self):
        many = [{"type": "list", "rows": [str(n) for n in range(40)]} for _ in range(9)]
        dash = self.parse(
            [
                {"type": "iframe", "src": "https://evil.example"},
                {"type": ["list"]},
                "list",
                {"type": "list", "rows": []},
                {"type": "hero", "done": 1, "total": 0},
                *many,
                {"type": "bars", "rows": [{"text": str(n), "value": n} for n in range(40)]},
                {
                    "type": "table",
                    "columns": list("abcdefgh"),
                    "rows": [list("abcdefgh")] * 40,
                },
                {
                    "type": "attention",
                    "groups": [{"title": "t", "rows": ["r"] * 9, "tone": "css;x"}] * 9,
                },
                {"type": "trend", "points": [{"label": "w", "closed": 1}] * 90},
                {"type": "workload", "rows": [{"name": "n", "Open": 1, "Closed": 9}] * 20},
            ]
        )
        blocks = dash["blocks"]
        self.assertEqual(len(blocks), skill_dashboard.MAX_BLOCKS)
        self.assertEqual(types(dash).count("list"), 3)
        by = {b["type"]: b for b in blocks}
        self.assertEqual(len(by["list"]["rows"]), skill_dashboard.MAX_BLOCK_ROWS)
        self.assertEqual(len(by["bars"]["rows"]), skill_dashboard.MAX_BARS)
        self.assertEqual(len(by["table"]["columns"]), 4)
        self.assertEqual(len(by["table"]["rows"]), 12)
        self.assertEqual(len(by["attention"]["groups"]), 3)
        self.assertEqual(len(by["attention"]["groups"][0]["rows"]), 3)
        self.assertEqual(by["attention"]["groups"][0]["reason"], "")
        self.assertNotIn("evil.example", json.dumps(dash))

    def test_nothing_else_of_the_answer_reaches_a_block(self):
        dash = self.parse(
            [
                {
                    "type": "meters",
                    "rows": [
                        {
                            "text": "m",
                            "done": 9,
                            "total": 2,
                            "due": "tomorrow",
                            "href": "https://evil.example",
                            "closed_recently": 9,
                        }
                    ],
                    "wide": True,
                    "title_key": "x",
                }
            ]
        )
        self.assertEqual(
            dash["blocks"],
            [
                {
                    "type": "meters",
                    "heading": "",
                    "rows": [{"text": "m", "done": 2, "total": 2, "due": None}],
                }
            ],
        )

    def test_the_headline_numbers_are_drawn_under_the_leading_ring(self):
        dash = self.parse(
            [{"type": "hero", "done": 1, "total": 2}],
            stats=[{"label": "open", "value": "7"}],
        )
        body = dashboard_page.render(dash)[0].split("</style>", 1)[1]
        self.assertLess(body.index('class="ring"'), body.index('class="numbers"'))

    def test_the_old_answer_shape_still_parses_with_no_blocks(self):
        dash = skill_dashboard.parse_dashboard(
            json.dumps({"stats": [{"label": "a", "value": "1"}]}), default_title="d"
        )
        self.assertNotIn("blocks", dash)

    def test_the_model_is_told_the_blocks_it_may_use(self):
        for kind in skill_dashboard.BLOCK_LIMITS:
            self.assertIn(f'"{kind}"', skill_dashboard.DIRECTIVE)

    def test_an_attention_card_draws_three_rows_and_counts_the_rest(self):
        rows = [{"text": f"task {n}"} for n in range(10)]
        dash = self.parse([{"type": "attention", "groups": [{"title": "Overdue", "rows": rows}]}])
        body = dashboard_page.render(dash)[0].split("</style>", 1)[1]
        in_sight = body.split("<template data-gd-extra>", 1)[0]
        self.assertIn("task 2", in_sight)
        self.assertNotIn("task 3", in_sight)
        self.assertIn("+7 more", body)

    def test_the_model_is_told_to_give_every_attention_row_not_three(self):
        told = skill_dashboard.DIRECTIVE
        self.assertIn(
            f"give every row you read, up to {skill_dashboard.MAX_ROWS} per group: the page shows "
            f"{dashboard_page.MAX_ATTENTION_ROWS} and counts the rest",
            told,
        )
        self.assertNotIn("rows each", told)

    def test_the_model_is_told_which_id_each_kind_of_ref_takes(self):
        # A `list_milestones` row has a `task_id` too (its backing task): not the milestone's id.
        for kind in dashboard_links.KINDS:
            self.assertRegex(skill_dashboard.DIRECTIVE, rf'for "{kind}" its {kind}_id\b')
