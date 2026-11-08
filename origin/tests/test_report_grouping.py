"""Report items grouped by project and sorted inside each group (MIP-1207).

The rule, the same in every section of every job (`report_kit.arrange`):

  * task items are grouped by project (name, case-insensitive, then id), and every item of a
    project is contiguous, so a card can draw a header where `project` changes;
  * inside a project: overdue first (the longest overdue first), then by due date, tasks with no
    due date last, then by display id (the task number);
  * anything that is not a task of a known project (to-dos, calendar events, requests, notes,
    chats) keeps its own order and follows the project groups;
  * EXCEPT "Start with" (MIP-1236): it is the briefing's top items in the order of what matters
    to the member, so it keeps that order and is not grouped; its items still carry `project`;
  * the stored report carries `project` ({id, name, code?}) per task item, whitelisted by type
    and length, so nothing else a job puts there is stored.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest import mock

from django.test import SimpleTestCase

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services import briefing, schedule_delivery
from origin.services.schedule_jobs import report_kit
from origin.services.schedule_jobs.report_kit import TaskFacts, arrange
from origin.tests.test_schedule_jobs import JobBase

TODAY = date(2026, 11, 8)


def facts(task_id, project_id, name, code, number, due=None, status="Open"):
    return TaskFacts(task_id, number, project_id, name, code, due, status, "")


def task_item(text, f):
    out = {"text": text, "project": {"id": f.project_id, "name": f.project_name}}
    out["_facts"] = f
    return out


def plain_item(text):
    return {"text": text}


class TestTheRule(SimpleTestCase):
    def texts(self, items):
        return [i["text"] for i in items]

    def test_projects_by_name_whatever_the_case_then_id(self):
        items = [
            task_item("z1", facts(1, 9, "zeta", "ZET", 1)),
            task_item("a1", facts(2, 3, "Alpha", "ALP", 1)),
            task_item("b1", facts(3, 5, "beta", "BET", 1)),
            task_item("a2", facts(4, 3, "Alpha", "ALP", 2)),
            task_item("t1", facts(5, 2, "Twin", "TW1", 1)),
            task_item("t2", facts(6, 1, "Twin", "TW2", 1)),
        ]
        self.assertEqual(self.texts(arrange(items, TODAY)), ["a1", "a2", "b1", "t2", "t1", "z1"])

    def test_inside_a_project_overdue_first_then_due_date_then_undated_then_id(self):
        def t(text, task_id, number, due):
            return task_item(text, facts(task_id, 1, "Alpha", "ALP", number, due))

        items = [
            t("undated 2", 1, 2, None),
            t("tomorrow", 2, 7, TODAY + timedelta(days=1)),
            t("overdue 1 day", 3, 8, TODAY - timedelta(days=1)),
            t("undated 1", 4, 1, None),
            t("today", 5, 9, TODAY),
            t("overdue 5 days", 6, 10, TODAY - timedelta(days=5)),
            t("same day, higher number", 7, 12, TODAY + timedelta(days=1)),
        ]
        self.assertEqual(
            self.texts(arrange(items, TODAY)),
            [
                "overdue 5 days",
                "overdue 1 day",
                "today",
                "tomorrow",
                "same day, higher number",
                "undated 1",
                "undated 2",
            ],
        )

    def test_the_display_id_is_the_number_not_the_text(self):
        items = [
            task_item("ten", facts(1, 1, "Alpha", "ALP", 10)),
            task_item("nine", facts(2, 1, "Alpha", "ALP", 9)),
            task_item("two", facts(3, 1, "Alpha", "ALP", 2)),
        ]
        self.assertEqual(self.texts(arrange(items, TODAY)), ["two", "nine", "ten"])

    def test_what_is_not_a_task_keeps_its_order_and_follows_the_groups(self):
        items = [
            plain_item("first todo"),
            task_item("beta", facts(1, 2, "Beta", "BET", 1)),
            plain_item("second todo"),
            task_item("alpha", facts(2, 1, "Alpha", "ALP", 1)),
            plain_item("third todo"),
        ]
        self.assertEqual(
            self.texts(arrange(items, TODAY)),
            ["alpha", "beta", "first todo", "second todo", "third todo"],
        )

    def test_a_section_of_only_non_tasks_is_not_touched(self):
        items = [plain_item("c"), plain_item("a"), plain_item("b")]
        self.assertEqual(self.texts(arrange(items, TODAY)), ["c", "a", "b"])

    def test_the_private_sort_key_never_leaves(self):
        out = arrange([task_item("a", facts(1, 1, "Alpha", "ALP", 1)), plain_item("b")], TODAY)
        self.assertTrue(all("_facts" not in i for i in out))

    def test_it_is_deterministic_for_equal_items(self):
        def make(order):
            return [task_item(f"t{n}", facts(n, 1, "Alpha", "ALP", 1)) for n in order]

        first = self.texts(arrange(make((3, 1, 2)), TODAY))
        second = self.texts(arrange(make((2, 3, 1)), TODAY))
        self.assertEqual(first, second)
        self.assertEqual(first, ["t1", "t2", "t3"])


class Base(JobBase):
    def project_named(self, name, code):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        return project

    def setUp(self):
        super().setUp()
        self.alpha = self.project_named("Alpha", "ALP")
        self.beta = self.project_named("beta", "BET")

    def names(self, items):
        return [i.get("project", {}).get("name") for i in items]

    def assert_contiguous(self, items):
        seen = []
        for name in self.names(items):
            if not seen or seen[-1] != name:
                seen.append(name)
        self.assertEqual(len(seen), len(set(seen)), f"a project came back after another: {seen}")


class TestWhatToDoToday(Base):
    def late(self, title, project, days):
        return self.make_task(
            title, project=project, due_date=self.local_today() - timedelta(days=days)
        )

    def test_overdue_is_grouped_by_project_and_sorted_by_how_late(self):
        b1 = self.late("Beta one day", self.beta, 1)
        a2 = self.late("Alpha two days", self.alpha, 2)
        b9 = self.late("Beta nine days", self.beta, 9)
        a7 = self.late("Alpha seven days", self.alpha, 7)
        items = self.section(self.run_job("what_to_do_today"), "Overdue")
        self.assertEqual(
            [i["link"] for i in items],
            [self.task_link(t) for t in (a7, a2, b9, b1)],
        )
        self.assertEqual(self.names(items), ["Alpha", "Alpha", "beta", "beta"])
        self.assertEqual(
            items[0]["project"], {"id": self.alpha.project_id, "name": "Alpha", "code": "ALP"}
        )
        self.assert_contiguous(items)

    def test_every_task_section_is_grouped(self):
        for n, project in enumerate((self.beta, self.alpha, self.beta, self.alpha)):
            task = self.make_task(
                f"T{n}", project=project, due_date=self.local_today(), priority="High"
            )
            TaskMaster.objects.filter(pk=task.pk).update(
                ts_updated_at=self.now - timedelta(days=10 + n)
            )
        report = self.run_job("what_to_do_today")
        checked = 0
        # ("Start with" is not here: it keeps the briefing's rank order, MIP-1236 below.)
        for heading in ("Due today", "Gone quiet"):
            items = self.section(report, heading)
            self.assertTrue(items, heading)
            self.assert_contiguous(items)
            self.assertEqual(
                self.names(items), sorted(self.names(items), key=str.casefold), heading
            )
            checked += 1
        self.assertEqual(checked, 2)

    def test_changed_tasks_are_grouped_and_notes_and_threads_follow(self):
        b = self.make_task("Beta work", project=self.beta)
        a = self.make_task("Alpha work", project=self.alpha)
        self.comment(b, self.user2, "hello", at=self.ago(hours=2))
        self.comment(a, self.user2, "hello", at=self.ago(hours=1))
        items = self.section(self.run_job("what_to_do_today"), "Changed in the last 24 hours")
        self.assertEqual([i["link"] for i in items], [self.task_link(a), self.task_link(b)])

    def test_to_dos_keep_their_order_and_have_no_project(self):
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=self.local_today()
        )
        for title in ("Zebra", "apple", "Mango"):
            ToDoItem.objects.create(group=group, title=title)
        items = self.section(self.run_job("what_to_do_today"), "To-dos for today")
        self.assertEqual([i["text"] for i in items], ["Zebra", "apple", "Mango"])
        self.assertTrue(all("project" not in i for i in items))

    def test_a_task_in_the_text_still_reads_as_before(self):
        task = self.late("Beta one day", self.beta, 1)
        item = self.section(self.run_job("what_to_do_today"), "Overdue")[0]
        self.assertIn("Beta one day", item["text"])
        self.assertIn("1 day late", item["text"])
        self.assertEqual(item["link"], self.task_link(task))


class TestTheOtherJobs(Base):
    def test_what_is_left_groups_open_and_closed(self):
        tasks = {}
        for title, project, status in (
            ("B open", self.beta, "WIP"),
            ("A open", self.alpha, "WIP"),
            ("B closed", self.beta, "Closed"),
            ("A closed", self.alpha, "Closed"),
        ):
            tasks[title] = self.make_task(title, project=project, status=status)
            self.activity(
                tasks[title], self.user, "status_changed", "Open", status, at=self.ago(days=2)
            )
        report = self.run_job("what_is_left")
        for heading, expected in (
            ("Still open from last time", ["A open", "B open"]),
            ("Closed after your work", ["A closed", "B closed"]),
        ):
            items = self.section(report, heading)
            self.assertEqual(
                [i["link"] for i in items], [self.task_link(tasks[t]) for t in expected]
            )
            self.assertEqual(self.names(items), ["Alpha", "beta"])

    def test_what_you_did_groups_the_tasks_section(self):
        tasks = []
        for title, project in (("B1", self.beta), ("A1", self.alpha), ("B2", self.beta)):
            task = self.make_task(title, project=project)
            self.activity(task, self.user, "status_changed", "Open", "WIP", at=self.ago(hours=1))
            tasks.append(task)
        items = self.section(self.run_job("what_you_did", day="today"), "Tasks")
        self.assertEqual(self.names(items), ["Alpha", "beta", "beta"])
        self.assert_contiguous(items)

    def test_what_the_ai_app_looked_at_is_grouped_by_project(self):
        from origin.models.common.member_event_models import MemberEvent  # noqa: PLC0415

        for n, project in enumerate((self.beta, self.alpha, self.beta)):
            task = self.make_task(f"Looked {n}", project=project)
            when = self.now - timedelta(hours=1 + n)
            MemberEvent.objects.create(
                user=self.user,
                team=self.team,
                kind="read",
                object_type="task",
                object_id=str(task.task_id),
                client="Claude",
                ts=when,
                bucket=int(when.timestamp() // 600),
            )
        items = self.section(
            self.run_job("what_you_did", day="today"), "Looked at with your AI app"
        )
        self.assertEqual(len(items), 3)
        self.assertEqual(self.names(items), ["Alpha", "beta", "beta"])

    def test_milestone_updates_sorts_by_urgency_inside_a_project(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.alpha, reporter=self.user, title="v1", status="Open"
        )
        quiet = self.make_task("Newest, no date", project=self.alpha, milestone=milestone)
        late = self.make_task(
            "Oldest, overdue",
            project=self.alpha,
            milestone=milestone,
            due_date=self.local_today() - timedelta(days=3),
        )
        self.comment(late, self.user2, "x", at=self.ago(hours=5))
        self.comment(quiet, self.user2, "y", at=self.ago(hours=1))
        report = self.run_job("milestone_updates", milestone_id=milestone.milestone_id)
        changed = self.section(report, "Changed in the last 168 hours")
        self.assertEqual(
            [i["link"] for i in changed], [self.task_link(late), self.task_link(quiet)]
        )
        self.assertEqual(self.names(changed), ["Alpha", "Alpha"])
        stands = self.section(report, "Where it stands")
        self.assertTrue(all("project" not in i for i in stands))


class TestTheStoredReport(SimpleTestCase):
    def clean(self, project):
        report = {
            "title": "t",
            "summary": "s",
            "sections": [{"heading": "h", "items": [{"text": "x", "project": project}]}],
        }
        return schedule_delivery.clean_report(report, "t")["sections"][0]["items"][0]

    def test_a_project_is_kept_with_its_id_name_and_code(self):
        self.assertEqual(
            self.clean({"id": 4, "name": "Alpha", "code": "ALP"})["project"],
            {"id": 4, "name": "Alpha", "code": "ALP"},
        )
        self.assertEqual(
            self.clean({"id": 4, "name": "Alpha"})["project"], {"id": 4, "name": "Alpha"}
        )

    def test_anything_else_is_dropped_never_repaired(self):
        for bad in (
            None,
            "Alpha",
            [],
            {},
            {"name": "x"},
            {"id": "4", "name": "x"},
            {"id": 4.0, "name": "x"},
            {"id": True, "name": "x"},
            {"id": 0, "name": "x"},
            {"id": -3, "name": "x"},
            {"id": 4, "name": ""},
            {"id": 4, "name": 7},
            {"id": 4},
        ):
            self.assertNotIn("project", self.clean(bad), bad)

    def test_lengths_and_extra_keys_are_cut_and_dropped(self):
        project = self.clean(
            {"id": 4, "name": "N" * 500, "code": "C" * 50, "html": "<b>x</b>", "x": 1}
        )["project"]
        self.assertEqual(set(project), {"id", "name", "code"})
        self.assertLessEqual(len(project["name"]), schedule_delivery.MAX_PROJECT_NAME_CHARS)
        self.assertLessEqual(len(project["code"]), schedule_delivery.MAX_PROJECT_CODE_CHARS)

    def test_a_name_is_one_line_of_text(self):
        project = self.clean({"id": 4, "name": "Alpha\n‮evil  team"})["project"]
        self.assertEqual(project["name"], "Alpha evil team")

    def test_an_old_item_without_a_project_is_unchanged(self):
        report = {
            "title": "t",
            "summary": "s",
            "sections": [{"heading": "h", "items": [{"text": "x", "link": "/workspace/a"}]}],
        }
        out = schedule_delivery.clean_report(report, "t")
        self.assertEqual(out["sections"][0]["items"], [{"text": "x", "link": "/workspace/a"}])
        self.assertEqual(set(out), {"version", "title", "text", "sections"})

    def test_the_private_sort_key_is_never_stored(self):
        report = {
            "title": "t",
            "summary": "s",
            "sections": [{"heading": "h", "items": [{"text": "x", "_facts": object()}]}],
        }
        self.assertNotIn("_facts", json.dumps(schedule_delivery.clean_report(report, "t")))


class TestTheKit(SimpleTestCase):
    def test_the_closing_line_is_added_after_the_sort_by_the_callers(self):
        # `more` has no project, so arrange alone would put it after the groups too.
        items = [
            {"text": "and 3 more tasks"},
            task_item("a", facts(1, 1, "Alpha", "ALP", 1)),
        ]
        self.assertEqual(
            [i["text"] for i in report_kit.arrange(items, TODAY)], ["a", "and 3 more tasks"]
        )


class TestStartWithKeepsTheRank(Base):
    """The "Start with" section lists the briefing's top items in the briefing's order (MIP-1236)."""

    def ranked(self, tasks):
        """Run the job with the briefing's `suggested_next` in exactly this order."""
        real = briefing.build_briefing

        def build(*args, **kwargs):
            data = real(*args, **kwargs)
            by_id = {t["task_id"]: t for t in data["suggested_next"]}
            data["suggested_next"] = [by_id[t.task_id] for t in tasks if t.task_id in by_id]
            return data

        with mock.patch.object(briefing, "build_briefing", build):
            return self.run_job("what_to_do_today")

    def three(self):
        """Tasks of two projects, all due today so the briefing offers them: ranked
        beta, alpha, beta, which grouping by project would turn into alpha, beta, beta."""
        return [
            self.make_task(f"T{n}", project=project, due_date=self.local_today(), priority="High")
            for n, project in enumerate((self.beta, self.alpha, self.beta))
        ]

    def test_the_rank_order_is_kept_and_not_grouped_by_project(self):
        tasks = self.three()
        report = self.ranked(tasks)
        items = self.section(report, "Start with")
        self.assertEqual([i["link"] for i in items], [self.task_link(t) for t in tasks])
        self.assertEqual(self.names(items), ["beta", "Alpha", "beta"])
        # Not contiguous: that is the point, and a card must not draw headers for it.
        with self.assertRaises(AssertionError):
            self.assert_contiguous(items)

    def test_each_row_still_has_its_project_and_typed_fields(self):
        tasks = self.three()
        items = self.section(self.ranked(tasks), "Start with")
        for task, entry in zip(tasks, items):
            self.assertEqual(entry["project"]["id"], task.project_id)
            self.assertEqual(entry["display_id"], f"{task.project.code}-{task.project_task_number}")
            self.assertEqual(entry["kind"], "task")
            self.assertEqual(entry["priority"], "High")
            self.assertNotIn("_facts", entry)

    def test_the_section_is_marked_so_the_card_knows(self):
        report = self.ranked(self.three())
        (section,) = [s for s in report["sections"] if s["heading"] == "Start with"]
        self.assertEqual(section["key"], "start_with")

    def test_every_other_section_is_still_grouped_by_project(self):
        self.three()
        report = self.ranked(self.three()[::-1])
        items = self.section(report, "Due today")
        self.assertTrue(items)
        self.assert_contiguous(items)
        self.assertEqual(self.names(items), sorted(self.names(items), key=str.casefold))

    def test_the_stored_report_keeps_the_order_and_the_projects(self):
        tasks = self.three()
        stored = schedule_delivery.clean_report(self.ranked(tasks), "x")
        section = next(s for s in stored["sections"] if s["key"] == "start_with")
        self.assertEqual(
            [i["project"]["name"] for i in section["items"]], ["beta", "Alpha", "beta"]
        )

    def test_a_member_with_a_recorded_week_gets_the_importance_order(self):
        """No stand-in for the briefing: a week of activity is recorded, the busiest task is
        in beta, then one in alpha, then a quiet one in beta."""
        busy = self.make_task("Busy", project=self.beta)
        middle = self.make_task("Middle", project=self.alpha)
        quiet = self.make_task("Quiet", project=self.beta)
        # Others' events count 3 each up to six, plus 2 per busy day: 32, then 15, then 10.
        for days in range(1, 8):
            for k in range(4):
                self.comment(busy, self.user2, f"b{k}", at=self.ago(days=days, hours=k))
        for days in (1, 4, 7):
            self.comment(middle, self.user2, "m", at=self.ago(days=days))
        for days in (2, 5):
            self.comment(quiet, self.user2, "q", at=self.ago(days=days))
        report = self.run_job("what_to_do_today")
        links = [i["link"] for i in self.section(report, "Start with")]
        expected = [self.task_link(t) for t in (busy, middle, quiet)]
        self.assertEqual(links[:3], expected)
        names = self.names(self.section(report, "Start with")[:3])
        self.assertEqual(names, ["beta", "Alpha", "beta"])
