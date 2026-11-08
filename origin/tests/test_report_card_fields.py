"""The typed fields of a stored report, so a card can draw it without parsing text (MIP-1208).

What is pinned here, each by a test that fails when the guard is removed:

  * every typed field is whitelisted by type: an enum value, an ISO date, a bounded integer, a
    bounded line of text; anything else is dropped, never repaired, and nothing outside the
    whitelist is stored (no markup, no script, no extra key);
  * the emoji of a section and of a headline count is the SERVER's, picked from the key: one a
    job (or the data it read) supplies is never stored;
  * `text` stays on every item and a report stored before these fields existed is still valid;
  * the built-in jobs fill the fields from the task's own row, with the project of MIP-1207.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from django.test import SimpleTestCase

from origin.services import schedule_delivery
from origin.services.schedule_jobs import (
    EMOJI,
    ITEM_KINDS,
    TASK_PRIORITIES,
    TASK_STATUSES,
)
from origin.services.schedule_jobs.report_kit import TaskFacts, _typed_fields, more
from origin.tests.test_report_grouping import Base


def stored(item=None, **section):
    report = {
        "title": "t",
        "summary": "s",
        "sections": [{"heading": "h", "items": [{"text": "x", **(item or {})}], **section}],
    }
    return schedule_delivery.clean_report(report, "t")


def one(item):
    return stored(item)["sections"][0]["items"][0]


class TestItemFields(SimpleTestCase):
    def test_valid_values_are_kept_as_they_are(self):
        item = {
            "kind": "task",
            "display_id": "MIP-12",
            "title": "Fix the login",
            "status": "WIP",
            "priority": "High",
            "due_date": "2026-11-08",
            "overdue_days": 3,
            "detail": "2 comments from others",
            "project": {"id": 4, "name": "Genos", "code": "MIP"},
        }
        self.assertEqual(one(item), {"text": "x", **item})

    def test_every_enum_keeps_its_values_and_nothing_else(self):
        for kind in ITEM_KINDS:
            self.assertEqual(one({"kind": kind})["kind"], kind)
        for status in TASK_STATUSES:
            self.assertEqual(one({"status": status})["status"], status)
        for priority in TASK_PRIORITIES:
            self.assertEqual(one({"priority": priority})["priority"], priority)
        for field, bad in (
            ("kind", ["tasks", "TASK", "", None, 3, ["task"], "<b>task</b>"]),
            ("status", ["wip", "Done", "Deleted", "", None, 1, "Open "]),
            ("priority", ["high", "Urgent", "", None, 0]),
        ):
            for value in bad:
                self.assertNotIn(field, one({field: value}), (field, value))

    def test_a_due_date_is_an_iso_day_that_exists(self):
        self.assertEqual(one({"due_date": "2026-02-28"})["due_date"], "2026-02-28")
        for bad in (
            "2026-02-30",
            "2026-13-01",
            "2026-1-1",
            "26-11-08",
            "2026-11-08T09:00:00",
            "20261108",  # a form `date.fromisoformat` reads but the card must not get
            "2026-W45-7",
            "2026-11-08 ",
            "08/11/2026",
            "",
            None,
            20261108,
            "<script>",
        ):
            self.assertNotIn("due_date", one({"due_date": bad}), bad)

    def test_overdue_days_is_a_whole_number_in_range(self):
        self.assertEqual(one({"overdue_days": 1})["overdue_days"], 1)
        self.assertEqual(one({"overdue_days": 36500})["overdue_days"], 36500)
        for bad in (0, -1, 36501, 2.5, "3", True, None, [3]):
            self.assertNotIn("overdue_days", one({"overdue_days": bad}), bad)

    def test_text_fields_are_one_bounded_line(self):
        out = one({"title": "T" * 900, "detail": "D\n‮" + "x" * 900, "display_id": "I" * 90})
        self.assertLessEqual(len(out["title"]), schedule_delivery.MAX_TEXT_CHARS)
        self.assertLessEqual(len(out["detail"]), schedule_delivery.MAX_TEXT_CHARS)
        self.assertLessEqual(len(out["display_id"]), schedule_delivery.MAX_DISPLAY_ID_CHARS)
        self.assertNotIn("\n", out["detail"])
        self.assertNotIn("‮", out["detail"])
        for field in ("title", "detail", "display_id"):
            for bad in (None, 5, ["a"], {"a": 1}, ""):
                self.assertNotIn(field, one({field: bad}), (field, bad))

    def test_no_other_key_is_stored(self):
        out = one(
            {
                "html": "<b>x</b>",
                "onclick": "alert(1)",
                "emoji": "💥",
                "style": "color:red",
                "_facts": object(),
                "kind": "task",
            }
        )
        self.assertEqual(set(out), {"text", "kind"})

    def test_text_stays_on_every_item_and_an_item_without_it_is_dropped(self):
        self.assertEqual(one({"kind": "task", "title": "T"})["text"], "x")
        report = {
            "title": "t",
            "summary": "s",
            "sections": [{"heading": "h", "items": [{"kind": "task", "title": "T"}]}],
        }
        self.assertEqual(schedule_delivery.clean_report(report, "t")["sections"], [])

    def test_an_old_item_is_exactly_as_before(self):
        out = schedule_delivery.clean_report(
            {
                "title": "t",
                "summary": "s",
                "sections": [
                    {
                        "heading": "h",
                        "items": [{"text": "x", "link": "/workspace/a"}, {"text": "y"}],
                    }
                ],
            },
            "t",
        )
        self.assertEqual(
            out["sections"],
            [{"heading": "h", "items": [{"text": "x", "link": "/workspace/a"}, {"text": "y"}]}],
        )
        self.assertNotIn("stats", out)


class TestTheKit(SimpleTestCase):
    def facts(self, due, status="Open"):
        return TaskFacts(1, 7, 3, "Genos", "MIP", due, status, "High", "Fix it")

    def test_overdue_days_is_for_a_task_still_open_and_past_its_day(self):
        today = date(2026, 11, 8)
        self.assertEqual(_typed_fields(self.facts(date(2026, 11, 4)), today)["overdue_days"], 4)
        self.assertEqual(_typed_fields(self.facts(date(2026, 11, 7)), today)["overdue_days"], 1)
        for due, status in ((today, "Open"), (today + timedelta(days=2), "WIP"), (None, "Open")):
            self.assertNotIn("overdue_days", _typed_fields(self.facts(due, status), today))
        closed = _typed_fields(self.facts(date(2026, 11, 1), "Closed"), today)
        self.assertNotIn("overdue_days", closed)
        self.assertEqual(closed["due_date"], "2026-11-01")

    def test_the_task_fields_come_from_the_row(self):
        out = _typed_fields(self.facts(date(2026, 11, 9)), date(2026, 11, 8))
        self.assertEqual(
            out,
            {
                "display_id": "MIP-7",
                "title": "Fix it",
                "status": "Open",
                "priority": "High",
                "due_date": "2026-11-09",
            },
        )
        bare = TaskFacts(42, None, None, "", "", None, "", "", "")
        self.assertEqual(_typed_fields(bare, date(2026, 11, 8)), {"display_id": "#42"})

    def test_the_closing_line_of_a_capped_list_says_what_it_is(self):
        self.assertEqual(more(3, "tasks"), {"text": "and 3 more tasks", "kind": "more"})


class TestSectionsAndStats(SimpleTestCase):
    def test_the_section_key_gets_the_servers_emoji(self):
        for key, emoji in EMOJI.items():
            section = stored(key=key)["sections"][0]
            self.assertEqual((section["key"], section["emoji"]), (key, emoji))

    def test_an_unknown_key_or_an_emoji_from_the_job_is_dropped(self):
        for bad in ("overdue ", "OVERDUE", "", None, 3, "<b>", "💥"):
            section = stored(key=bad)["sections"][0]
            self.assertNotIn("key", section, bad)
            self.assertNotIn("emoji", section, bad)
        section = stored(key="overdue", emoji="💥")["sections"][0]
        self.assertEqual(section["emoji"], EMOJI["overdue"])
        self.assertEqual(set(stored()["sections"][0]), {"heading", "items"})

    def body(self, stats):
        return schedule_delivery.clean_report(
            {"title": "t", "summary": "s", "sections": [], "stats": stats}, "t"
        )

    def test_stats_keep_a_known_key_a_whole_count_and_the_servers_emoji(self):
        out = self.body(
            [{"key": "overdue", "count": 2, "emoji": "💥"}, {"key": "due_today", "count": 0}]
        )
        self.assertEqual(
            out["stats"],
            [
                {"key": "overdue", "count": 2, "emoji": EMOJI["overdue"]},
                {"key": "due_today", "count": 0, "emoji": EMOJI["due_today"]},
            ],
        )

    def test_bad_stats_are_dropped(self):
        out = self.body(
            [
                {"key": "gossip", "count": 1},
                {"key": "overdue", "count": -1},
                {"key": "overdue", "count": True},
                {"key": "overdue", "count": 1.5},
                {"key": "overdue", "count": "2"},
                {"key": "overdue", "count": 100000},
                {"key": "due_today", "count": 3},
                {"key": "due_today", "count": 9},  # a key once
                "overdue",
                None,
            ]
        )
        self.assertEqual([(s["key"], s["count"]) for s in out["stats"]], [("due_today", 3)])
        for value in (None, "overdue", {"key": "overdue", "count": 1}, 5, []):
            self.assertNotIn("stats", self.body(value))

    def test_at_most_eight_stats(self):
        many = [{"key": key, "count": 1} for key in EMOJI]
        self.assertEqual(len(self.body(many)["stats"]), schedule_delivery.MAX_STATS)

    def test_the_body_says_which_version_it_is(self):
        self.assertEqual(stored()["version"], 2)
        self.assertEqual(set(stored()), {"version", "title", "text", "sections"})

    def test_the_whole_body_is_plain_json_with_no_markup_we_made(self):
        body = json.dumps(
            stored({"kind": "task", "title": "T", "project": {"id": 1, "name": "P"}}, key="overdue")
        )
        self.assertNotIn("<", body)


class TestWhatTheJobsFill(Base):
    def stored_report(self, key, **params):
        return schedule_delivery.clean_report(self.run_job(key, **params), key)

    def test_a_task_line_carries_its_own_fields_and_still_reads_as_text(self):
        task = self.make_task(
            "Fix the login",
            project=self.alpha,
            status="WIP",
            priority="High",
            due_date=self.local_today() - timedelta(days=3),
        )
        body = self.stored_report("what_to_do_today")
        section = [s for s in body["sections"] if s["heading"] == "Overdue"][0]
        self.assertEqual((section["key"], section["emoji"]), ("overdue", EMOJI["overdue"]))
        line = section["items"][0]
        self.assertEqual(
            {k: v for k, v in line.items() if k != "text"},
            {
                "link": self.task_link(task),
                "kind": "task",
                "task_id": task.task_id,
                "why": "overdue",
                "can_answer": True,
                "project": {"id": self.alpha.project_id, "name": "Alpha", "code": "ALP"},
                "display_id": task.display_id,
                "title": "Fix the login",
                "status": "WIP",
                "priority": "High",
                "due_date": (self.local_today() - timedelta(days=3)).isoformat(),
                "overdue_days": 3,
            },
        )
        # An older reader that knows only `text` still gets the whole line.
        self.assertIn("Fix the login", line["text"])
        self.assertIn("3 days late", line["text"])

    def test_a_task_due_later_has_no_overdue_days_and_a_closed_one_never_has(self):
        later = self.make_task("Later", project=self.alpha, due_date=self.local_today())
        body = self.stored_report("what_to_do_today")
        line = [s for s in body["sections"] if s["heading"] == "Due today"][0]["items"][0]
        self.assertEqual(line["due_date"], self.local_today().isoformat())
        self.assertNotIn("overdue_days", line)
        self.assertEqual(line["display_id"], later.display_id)

    def test_the_headline_counts_are_the_agendas_own(self):
        for n in range(2):
            self.make_task(
                f"Late {n}", project=self.alpha, due_date=self.local_today() - timedelta(days=1)
            )
        self.make_task("Due", project=self.alpha, due_date=self.local_today())
        stats = {s["key"]: s["count"] for s in self.stored_report("what_to_do_today")["stats"]}
        self.assertEqual(stats["overdue"], 2)
        self.assertEqual(stats["due_today"], 1)
        quiet = self.stored_report("what_you_did", day="today")
        self.assertNotIn("stats", quiet)

    def test_the_detail_is_the_sentence_about_the_task_that_is_not_in_its_fields(self):
        task = self.make_task("Hot", project=self.alpha)
        self.comment(task, self.user2, "hi", at=self.ago(hours=1))
        body = self.stored_report("what_to_do_today")
        changed = [s for s in body["sections"] if s.get("key") == "changed"][0]
        line = changed["items"][0]
        self.assertEqual(line["kind"], "task")
        self.assertIn("commented by otheruser", line["detail"])

    def test_non_task_lines_say_what_they_are_and_have_no_task_fields(self):
        from origin.models.chat.todo_models import ToDoGroup, ToDoItem  # noqa: PLC0415

        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=self.local_today()
        )
        ToDoItem.objects.create(group=group, title="Call the bank")
        body = self.stored_report("what_to_do_today")
        section = [s for s in body["sections"] if s["heading"] == "To-dos for today"][0]
        self.assertEqual(section["key"], "todos_today")
        line = section["items"][0]
        self.assertEqual(line["kind"], "todo")
        for field in ("project", "display_id", "status", "due_date", "overdue_days"):
            self.assertNotIn(field, line)

    def test_every_section_the_jobs_make_has_a_key_from_the_table(self):
        task = self.make_task("Work", project=self.alpha, status="WIP")
        self.activity(task, self.user, "status_changed", "Open", "WIP", at=self.ago(hours=1))
        self.comment(task, self.user2, "hi", at=self.ago(hours=1))
        for key, params in (
            ("what_to_do_today", {}),
            ("what_you_did", {"day": "today"}),
            ("what_is_left", {"days": 2}),
        ):
            report = self.run_job(key, **params)
            self.assertTrue(report["sections"], key)
            for section in report["sections"]:
                self.assertIn(section.get("key"), EMOJI, (key, section["heading"]))

    def test_what_you_did_and_what_is_left_fill_their_task_lines_too(self):
        task = self.make_task("Work", project=self.alpha, status="WIP", priority="Low")
        self.activity(task, self.user, "status_changed", "Open", "WIP", at=self.ago(hours=1))
        for key, params, heading in (
            ("what_you_did", {"day": "today"}, "Tasks"),
            ("what_is_left", {"days": 2}, "Still open from last time"),
        ):
            line = [
                s for s in self.stored_report(key, **params)["sections"] if s["heading"] == heading
            ][0]["items"][0]
            self.assertEqual(
                (line["kind"], line["status"], line["priority"], line["title"]),
                ("task", "WIP", "Low", "Work"),
                key,
            )
            self.assertEqual(line["project"]["name"], "Alpha")

    def test_milestone_updates_names_every_section(self):
        from origin.models.task.milestone_models import MilestoneMaster  # noqa: PLC0415

        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.alpha, reporter=self.user, title="v1", status="Open"
        )
        task = self.make_task("Work", project=self.alpha, milestone=milestone)
        self.comment(task, self.user2, "hi", at=self.ago(hours=1))
        self.make_task(
            "Late",
            project=self.alpha,
            milestone=milestone,
            due_date=self.local_today() - timedelta(days=2),
        )
        body = self.stored_report("milestone_updates", milestone_id=milestone.milestone_id)
        keys = {s["heading"]: s.get("key") for s in body["sections"]}
        self.assertEqual(keys["Where it stands"], "where_it_stands")
        self.assertEqual(keys["Changed in the last 168 hours"], "changed")
        self.assertEqual(keys["Ready to start"], "ready")
        stats = {s["key"]: s["count"] for s in body["stats"]}
        self.assertEqual(stats["changed"], 1)
        self.assertEqual(stats["overdue"], 1)
        changed = [s for s in body["sections"] if s.get("key") == "changed"][0]["items"][0]
        self.assertEqual((changed["kind"], changed["display_id"]), ("task", task.display_id))

    def test_a_capped_list_ends_with_a_more_line_that_says_so(self):
        for n in range(12):
            self.make_task(
                f"Late {n}", project=self.alpha, due_date=self.local_today() - timedelta(days=1)
            )
        items = [
            s
            for s in self.stored_report("what_to_do_today")["sections"]
            if s.get("key") == "overdue"
        ][0]["items"]
        self.assertEqual(items[-1]["kind"], "more")
        self.assertTrue(all(i["kind"] == "task" for i in items[:-1]))

    def test_it_reaches_the_inbox_item(self):
        from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems  # noqa: PLC0415
        from origin.models.common.schedule_models import MemberSchedule  # noqa: PLC0415

        self.make_task("Late", project=self.alpha, due_date=self.local_today() - timedelta(days=1))
        sched = MemberSchedule.objects.create(
            user=self.user,
            team=self.team,
            name="Plan",
            job="what_to_do_today",
            frequency="daily",
            time_of_day="09:00",
            tz_used="Asia/Tokyo",
        )
        schedule_delivery.deliver(sched, self.run_job("what_to_do_today"))
        body = InboxItems.objects.get(item_type=ITEM_TYPE_GENOS_REPORT).item_body
        self.assertEqual(body["version"], 2)
        self.assertTrue(body["title"] and body["text"])
        line = [s for s in body["sections"] if s.get("key") == "overdue"][0]["items"][0]
        self.assertEqual(line["kind"], "task")
        self.assertIn("Late", line["text"])
