"""MIP-1413: the closed-ancestor rule in the in-app agent's rollups and lists, behind AGENT_CLOSED_ANCESTOR_RULE.

A task under a Closed or Deleted ancestor reads as Closed or Deleted (`services/task_ancestry.py`). The
MCP task reads, the briefing and the progress bar apply it; the in-app tools did not. With the flag on:

  * counts and breakdowns (`get_my_task_summary`, `get_milestone_summary`, `get_sprint_summary`,
    `list_milestones`, `list_my_milestones`) read each task by its EFFECTIVE status (Closed, not open,
    not overdue; Deleted: not counted), the progress bar's reading;
  * `get_my_focus_tasks` leaves such tasks out (the briefing's rule);
  * `list_tasks` rows and `fetch_task` carry `effective_status` / `closed_by_ancestor` (the MCP fields)
    and keep their own `status`; `list_tasks(overdue_only)` drops a task that reads as closed.

Off (the default) every result is what `origin/main` returns (`test_task_ancestry` still pins that the
registry tool's result is untouched).

PINS. The off-state goldens were recorded on PRISTINE `origin/main` (09f52388), where the flag does not
exist, over one fixed tree (a Closed parent with open children, a Deleted parent, a Closed milestone, a
sprint) with the clock frozen around the calls:

    git -C <genos-api> worktree add --detach <wt> origin/main
    cp origin/tests/test_agent_closed_ancestor_rule.py <wt>/origin/tests/
    RECORD_GOLDEN=1 python manage.py test origin.tests.test_agent_closed_ancestor_rule.FlagOffTests --noinput
    # prints `GOLDEN <key> = "<sha256>"`; paste below

`tool/*` hash each tool's result (ids by order of appearance, the rest verbatim); `declarations` hashes every
tool declaration the model is sent. FlagOffTests imports nothing this change adds, so it runs on main.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict
from datetime import date, datetime, timezone
from unittest import mock

from django.conf import settings
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import REGISTRY
from origin.services.milestone_rollup import milestone_work_counts
from origin.services.task_ancestry import effective_statuses
from origin.tests.test_task_ancestry import AncestryBase

FLAG = "AGENT_CLOSED_ANCESTOR_RULE"
NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 2)
PAST = date(2020, 1, 1)

GOLDEN: dict[str, str] = {
    "tool/get_my_focus_tasks": "ec54fa70ed07ba54ba2ecc966db9709f7fa7ce067e4c2d9daffbc286286fcbf3",
    "tool/get_my_task_summary": "5bdfd24aeac57b5d80064f7b1648dccc21804aa48f5df4e3c4ced910021523d1",
    "tool/get_milestone_summary": "2df239fbc82dcbb0b1fa2b014ac5f1f8f5f504aac2fb89a331e9408a31f0550f",
    "tool/get_milestone_summary_closed": "a1fcff416e4c3bb81a32a8ecdfa6b521939f32fd9dfd30c5ef556180c83039fa",
    "tool/list_milestones": "8818a28365be09d7777bb53722affc682f81369485cd4feb9f24aacb0c340c92",
    "tool/list_my_milestones": "34fd1ff2378de073a423d84e8c751d8d68028cb914c3e072188ecd6c92f5a33a",
    "tool/get_sprint_summary": "0ee30c0bcba6e0518b4a963182a42a9db1adb7471b0e6a0a260d265d0fdb04c2",
    "tool/list_tasks": "8c20725757cd5cee1b4c3c5d1ae9a7dabb0bf6cf8cf597d05607d36c28e7d9ae",
    "tool/list_tasks_overdue": "5514d527ce3f8604ce6f4b412586e7a286fa052a3987ca070bada3e776a6052c",
    "tool/fetch_task": "ed42299a027f7691caddc6436558e294a1facd301fd221d195ba1abce30297bf",
    "declarations": "ef496d745cfc175a1a0e02e92e254e7f4304bab500d1ffd93d9016da2731832a",
}

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def normalised(result) -> str:
    seen: dict[str, str] = {}

    def walk(value, key=""):
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        if isinstance(value, int) and not isinstance(value, bool) and key.endswith("id"):
            return "<int>"
        if isinstance(value, str):
            return _UUID.sub(lambda m: seen.setdefault(m.group(0), f"<uuid{len(seen)}>"), value)
        return value

    return json.dumps(walk(result), ensure_ascii=False, indent=1)


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def on():
    return override_settings(SEARCH_ENGINE={**settings.SEARCH_ENGINE, FLAG: True})


def frozen():
    return mock.patch("django.utils.timezone.now", return_value=NOW)


class Tree(AncestryBase):
    """One project with every shape the rule reads."""

    def setUp(self):
        super().setUp()
        t = self.t = {}
        t["P"] = self.make("Closed parent", status="Closed")
        t["C1"] = self.make("Child under closed", parent=t["P"], status="WIP")
        t["G1"] = self.make("Grandchild", parent=t["C1"], due_date=PAST)
        t["D"] = self.make("Deleted parent", status="Deleted")
        t["DC"] = self.make("Child under deleted", parent=t["D"], status="WIP")
        t["A1"] = self.make("Plain overdue", due_date=PAST)
        t["A2"] = self.make("Plain WIP", status="WIP")
        self.m, self.backing = self.milestone("Launch")
        self.mc, self.backing_c = self.milestone("Shipped", status="Closed")
        self._n = 1000  # the milestones' backing rows took the next numbers
        t["M1"] = self.make("M open", milestone=self.m)
        t["M2"] = self.make("M under closed parent", milestone=self.m, parent=t["P"])
        t["M3"] = self.make("M under deleted parent", milestone=self.m, parent=t["D"])
        t["M4"] = self.make("M closed", milestone=self.m, status="Closed")
        t["M5"] = self.make("M overdue", milestone=self.m, status="WIP", due_date=PAST)
        t["MC1"] = self.make("In the closed milestone", milestone=self.mc)
        for m in (self.m, self.mc):
            MilestoneAssignees.objects.create(milestone=m, user=self.user, team=self.team)
        self.sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="Sprint 1",
            sequence_number=1,
            start_date=date(2026, 10, 27),
            end_date=date(2026, 11, 9),
        )
        TaskMaster.objects.filter(pk__in=[t[k].pk for k in ("A1", "C1", "DC", "M2", "M4")]).update(
            sprint=self.sprint
        )
        type(self.m).objects.filter(pk=self.m.pk).update(sprint=self.sprint)

    def ids(self, *names):
        return {self.t[n].task_id for n in names}

    def run_tool(self, name, **args):
        with frozen():
            return REGISTRY[name].run(args, self.ctx)

    def results(self):
        return {
            "tool/get_my_focus_tasks": self.run_tool("get_my_focus_tasks", limit=20),
            "tool/get_my_task_summary": self.run_tool("get_my_task_summary"),
            "tool/get_milestone_summary": self.run_tool(
                "get_milestone_summary", milestone_id=self.m.milestone_id
            ),
            "tool/get_milestone_summary_closed": self.run_tool(
                "get_milestone_summary", milestone_id=self.mc.milestone_id
            ),
            "tool/list_milestones": self.run_tool("list_milestones"),
            "tool/list_my_milestones": self.run_tool("list_my_milestones"),
            "tool/get_sprint_summary": self.run_tool(
                "get_sprint_summary", sprint_id=self.sprint.sprint_id
            ),
            "tool/list_tasks": self.run_tool("list_tasks", limit=100),
            "tool/list_tasks_overdue": self.run_tool("list_tasks", overdue_only=True, limit=100),
            "tool/fetch_task": self.run_tool("fetch_task", task_id=self.t["G1"].task_id),
        }


class FlagOffTests(Tree):
    """What the model is sent with the flag off: main's, byte for byte (hashes from main)."""

    def pin(self, key, text):
        digest = digest_text(text)
        if os.environ.get("RECORD_GOLDEN"):
            print(f'GOLDEN {key} = "{digest}"')
            return
        self.assertEqual(digest, GOLDEN[key], f"what the model is sent changed: {key}")

    def test_every_tool_result(self):
        for key, result in self.results().items():
            self.pin(key, normalised(result))

    def test_the_declarations_the_model_reads(self):
        text = json.dumps([asdict(d) for d in _build_tool_declarations()], sort_keys=True)
        self.pin("declarations", text)

    def test_today_a_task_under_a_closed_parent_is_listed_and_counted_as_open(self):
        listed = {r["task_id"] for r in self.run_tool("get_my_focus_tasks", limit=20)["tasks"]}
        self.assertTrue(self.ids("C1", "G1", "DC") <= listed)
        self.assertNotIn("effective_status", json.dumps(self.run_tool("list_tasks")))


class FlagOnTests(Tree):
    def on_off(self, name, **args):
        before = self.run_tool(name, **args)
        with on():
            after = self.run_tool(name, **args)
        return before, after

    def oracle(self, qs, *, today=TODAY):
        """Counts by effective status, by the walker itself: the rule's own definition."""
        tasks = list(qs)
        found = effective_statuses([t.task_id for t in tasks], self.team.team_id)
        counts = {s: 0 for s in ("Open", "WIP", "Blocked", "Pending", "Closed")}
        overdue = total = 0
        for t in tasks:
            reads = found[t.task_id].status
            if reads == "Deleted":
                continue
            total += 1
            counts[reads] += 1
            if reads != "Closed" and t.due_date is not None and t.due_date < today:
                overdue += 1
        return total, counts, overdue

    # -- lists ------------------------------------------------------------ #

    def test_focus_tasks_leave_out_what_reads_as_closed_and_nothing_else(self):
        before, after = self.on_off("get_my_focus_tasks", limit=20)
        off_ids = {r["task_id"] for r in before["tasks"]}
        on_ids = {r["task_id"] for r in after["tasks"]}
        self.assertEqual(off_ids - on_ids, self.ids("C1", "G1", "DC", "M2", "M3", "MC1"))
        self.assertEqual(on_ids - off_ids, set())
        self.assertTrue(self.ids("A1", "A2", "M1", "M5") <= on_ids)
        # the rows that stay are byte-for-byte the rows they were
        keep = {r["task_id"]: r for r in before["tasks"] if r["task_id"] in on_ids}
        self.assertEqual({r["task_id"]: r for r in after["tasks"]}, keep)

    def test_reopening_the_parent_brings_the_branch_back(self):
        self.set(self.t["P"], "Open")
        with on():
            ids = {r["task_id"] for r in self.run_tool("get_my_focus_tasks", limit=20)["tasks"]}
        self.assertTrue(self.ids("C1", "G1") <= ids)

    def test_focus_tasks_and_the_briefing_agree(self):
        import inspect  # noqa: PLC0415

        from origin.services import briefing  # noqa: PLC0415

        if "effective_statuses" not in inspect.getsource(briefing._my_active_tasks):
            self.skipTest(
                "this stack predates genos-api #737: the briefing does not apply the rule yet"
            )
        with on():
            ids = {r["task_id"] for r in self.run_tool("get_my_focus_tasks", limit=20)["tasks"]}
        mine = briefing._my_active_tasks(self.ctx)
        self.assertEqual(ids, set(mine.values_list("task_id", flat=True)))

    def test_list_tasks_rows_carry_what_they_read_as_and_keep_their_own_status(self):
        before, after = self.on_off("list_tasks", limit=100)
        rows = {r["task_id"]: r for r in after["tasks"]}
        g1 = rows[self.t["G1"].task_id]
        self.assertEqual((g1["status"], g1["effective_status"]), ("Open", "Closed"))
        self.assertEqual(g1["closed_by_ancestor"]["task_id"], self.t["P"].task_id)
        self.assertEqual(rows[self.t["DC"].task_id]["effective_status"], "Deleted")
        self.assertEqual(rows[self.t["A2"].task_id]["effective_status"], "WIP")
        self.assertNotIn("closed_by_ancestor", rows[self.t["A2"].task_id])
        extra = ("effective_status", "closed_by_ancestor")
        stripped = [{k: v for k, v in r.items() if k not in extra} for r in after["tasks"]]
        self.assertEqual(stripped, before["tasks"])

    def test_list_tasks_status_filter_is_still_the_rows_own_status(self):
        with on():
            out = self.run_tool("list_tasks", status=["WIP"], limit=100)
        ids = {r["task_id"] for r in out["tasks"]}
        self.assertIn(self.t["C1"].task_id, ids)

    def test_overdue_only_drops_a_task_that_reads_as_closed(self):
        before, after = self.on_off("list_tasks", overdue_only=True, limit=100)
        self.assertIn(self.t["G1"].task_id, {r["task_id"] for r in before["tasks"]})
        on_ids = {r["task_id"] for r in after["tasks"]}
        self.assertNotIn(self.t["G1"].task_id, on_ids)
        self.assertTrue(self.ids("A1", "M5") <= on_ids)

    def test_fetch_task_carries_the_reading(self):
        before = self.run_tool("fetch_task", task_id=self.t["G1"].task_id)
        with on():
            after = self.run_tool("fetch_task", task_id=self.t["G1"].task_id)
            plain = self.run_tool("fetch_task", task_id=self.t["A1"].task_id)
        self.assertEqual(after["effective_status"], "Closed")
        self.assertEqual(after["closed_by_ancestor"]["task_id"], self.t["P"].task_id)
        self.assertEqual(after["status"], "Open")
        extra = {"effective_status", "closed_by_ancestor"}
        self.assertEqual({k: v for k, v in after.items() if k not in extra}, before)
        self.assertEqual(plain["effective_status"], "Open")
        self.assertNotIn("closed_by_ancestor", plain)

    # -- counts ----------------------------------------------------------- #

    def test_my_task_summary_counts_by_effective_status(self):
        with on():
            out = self.run_tool("get_my_task_summary")
        mine = TaskMaster.objects.filter(assignee=self.user, is_deleted=False, is_init_task=False)
        total, counts, overdue = self.oracle(mine.exclude(status="Deleted"))
        self.assertEqual(out["total"], total)
        self.assertEqual(out["status_breakdown"], counts)
        self.assertEqual(out["overdue_count"], overdue)
        before = self.run_tool("get_my_task_summary")
        self.assertGreater(out["status_breakdown"]["Closed"], before["status_breakdown"]["Closed"])
        self.assertLess(out["overdue_count"], before["overdue_count"])

    def test_milestone_summary_counts_by_effective_status_and_matches_the_progress_bar(self):
        with on():
            out = self.run_tool("get_milestone_summary", milestone_id=self.m.milestone_id)
        work = TaskMaster.objects.filter(task_id__in=self.ids("M1", "M2", "M3", "M4", "M5"))
        total, counts, overdue = self.oracle(work)
        self.assertEqual(out["total_tasks"], total)
        self.assertEqual({k: out["status_breakdown"][k] for k in counts}, counts)
        self.assertEqual(out["overdue_count"], overdue)
        self.assertEqual(out["status_breakdown"]["Deleted"], 1)  # M3, gone under D
        bar = milestone_work_counts([self.m], ancestor_rule=True)[self.m.milestone_id]
        self.assertEqual((out["total_tasks"], out["status_breakdown"]["Closed"]), bar)

    def test_a_closed_milestone_reads_all_closed(self):
        with on():
            out = self.run_tool("get_milestone_summary", milestone_id=self.mc.milestone_id)
        bar = milestone_work_counts([self.mc], ancestor_rule=True)[self.mc.milestone_id]
        self.assertEqual((out["total_tasks"], out["status_breakdown"]["Closed"]), bar)
        self.assertEqual(out["status_breakdown"]["Open"], 0)

    def test_nothing_that_reads_as_closed_is_ready_to_start(self):
        before, after = self.on_off("get_milestone_summary", milestone_id=self.m.milestone_id)

        def ready(out):
            return {r["task_id"] for r in out["ready_now"]}

        self.assertTrue(self.ids("M2", "M3") <= ready(before))
        self.assertEqual(ready(after), ready(before) - self.ids("M2", "M3"))
        self.assertEqual(after["ready_now_count"], before["ready_now_count"] - 2)

    def test_milestone_lists_match_the_progress_bar_and_the_oracle(self):
        bars = milestone_work_counts([self.m, self.mc], ancestor_rule=True)
        for name in ("list_milestones", "list_my_milestones"):
            with on():
                out = self.run_tool(name)
            rows = {r["milestone_id"]: r for r in out["milestones"]}
            for milestone in (self.m, self.mc):
                row = rows[milestone.milestone_id]
                self.assertEqual(
                    (row["tasks_total"], row["tasks_closed"]), bars[milestone.milestone_id], name
                )
            self.assertEqual(rows[self.m.milestone_id]["overdue_count"], 1, name)  # M5 only
            before = self.run_tool(name)
            self.assertEqual(
                {r["milestone_id"]: r["tasks_total"] for r in before["milestones"]}[
                    self.m.milestone_id
                ],
                5,
            )

    def test_sprint_summary_counts_by_effective_status(self):
        with on():
            out = self.run_tool("get_sprint_summary", sprint_id=self.sprint.sprint_id)
        in_sprint = TaskMaster.objects.filter(sprint=self.sprint, is_deleted=False)
        total, counts, overdue = self.oracle(in_sprint.exclude(status="Deleted"))
        self.assertEqual(out["total_tasks"], total)
        self.assertEqual({k: out["status_breakdown"][k] for k in counts}, counts)
        self.assertEqual(out["overdue_count"], overdue)
        self.assertEqual(out["status_breakdown"]["Deleted"], 1)  # DC
        (milestone,) = out["milestones"]
        bar = milestone_work_counts([self.m], ancestor_rule=True)[self.m.milestone_id]
        self.assertEqual((milestone["tasks_total"], milestone["tasks_closed"]), bar)

    def test_a_blocker_that_reads_as_closed_no_longer_blocks(self):
        from origin.models.task.task_models import TaskDependency  # noqa: PLC0415

        TaskDependency.objects.create(
            blocker_task=self.t["C1"],
            blocked_task=self.t["M1"],
            team=self.team,
            created_by=self.user,
        )

        def blockers(out):
            (row,) = [r for r in out["blocked"] if r["task_id"] == self.t["M1"].task_id]
            return [b["task_id"] for b in row["open_blockers"]]

        before, after = self.on_off("get_milestone_summary", milestone_id=self.m.milestone_id)
        self.assertEqual(blockers(before), [self.t["C1"].task_id])
        self.assertEqual(blockers(after), [])  # C1 reads as Closed under P

    def test_a_closed_milestone_with_no_backing_row_reads_all_closed_in_lists_and_sprints(self):
        from origin.models.task.milestone_models import MilestoneMaster  # noqa: PLC0415

        legacy = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Legacy",
            status="Closed",
            sprint=self.sprint,
        )
        MilestoneAssignees.objects.create(milestone=legacy, user=self.user, team=self.team)
        self.make("legacy a", milestone=legacy)
        self.make("legacy b", milestone=legacy, status="WIP")
        bar = milestone_work_counts([legacy], ancestor_rule=True)[legacy.milestone_id]
        self.assertEqual(bar, (2, 2))
        with on():
            rows = {r["milestone_id"]: r for r in self.run_tool("list_milestones")["milestones"]}
            mine = {r["milestone_id"]: r for r in self.run_tool("list_my_milestones")["milestones"]}
            sprint = self.run_tool("get_sprint_summary", sprint_id=self.sprint.sprint_id)
        for row in (rows[legacy.milestone_id], mine[legacy.milestone_id]):
            self.assertEqual((row["tasks_total"], row["tasks_closed"]), bar)
        got = {m["milestone_id"]: m for m in sprint["milestones"]}[legacy.milestone_id]
        self.assertEqual((got["tasks_total"], got["tasks_closed"]), bar)

    # -- cost, scope, text ------------------------------------------------ #

    def test_one_query_for_the_whole_tree_however_big(self):
        from origin.search_engine.agent.tools import ancestor_rule  # noqa: PLC0415

        def build(depth, width):
            top = self.make("top", status="Closed")
            for _ in range(width):
                parent = top
                for _ in range(depth):
                    parent = self.make("node", parent=parent)

        build(2, 2)
        with CaptureQueriesContext(connection) as few:
            ancestor_rule.rule_for_projects([self.project.project_id], self.team.team_id)
        build(8, 6)
        with CaptureQueriesContext(connection) as many:
            rule = ancestor_rule.rule_for_projects([self.project.project_id], self.team.team_id)
        self.assertEqual((len(few), len(many)), (1, 1))
        self.assertGreater(len(rule.closed), 50)

    def test_the_rule_is_per_team_and_never_changes_a_status(self):
        from origin.search_engine.agent.tools import ancestor_rule  # noqa: PLC0415

        rule = ancestor_rule.rule_for_projects(
            [self.project.project_id], "6f1c1f4e-0000-4000-8000-0000000000aa"
        )
        self.assertEqual((rule.closed, rule.deleted), (frozenset(), frozenset()))
        with on():
            self.results()
        self.t["G1"].refresh_from_db()
        self.assertEqual(self.t["G1"].status, "Open")

    def test_one_sentence_is_added_to_each_changed_tool_and_nothing_else_moves(self):
        from origin.search_engine.agent.tools import ancestor_rule  # noqa: PLC0415

        before = {d.name: d for d in _build_tool_declarations()}
        with on():
            after = {d.name: d for d in _build_tool_declarations()}
        added = {
            "get_my_focus_tasks": ancestor_rule.FOCUS_ADDITION,
            "get_my_task_summary": ancestor_rule.COUNTS_ADDITION,
            "get_milestone_summary": ancestor_rule.COUNTS_ADDITION,
            "get_sprint_summary": ancestor_rule.COUNTS_ADDITION,
            "list_milestones": ancestor_rule.COUNTS_ADDITION,
            "list_my_milestones": ancestor_rule.COUNTS_ADDITION,
            "list_tasks": ancestor_rule.ROWS_ADDITION,
            "fetch_task": ancestor_rule.ROWS_ADDITION,
        }
        for name, d in before.items():
            if name in added:
                self.assertEqual(after[name].description, d.description + added[name], name)
                self.assertEqual(after[name].parameters_schema, d.parameters_schema, name)
            else:
                self.assertEqual(after[name], d, name)

    def test_the_flag_defaults_off(self):
        self.assertIs(settings.SEARCH_ENGINE[FLAG], False)


class SeedTests(AncestryBase):
    """The eval seed of the closed-ancestor cases (`group/seeds.py`): a closed parent, two open children, one task."""

    def info(self, team="v1"):
        return {"team_id": str(self.team.team_id), "user_id": str(self.user.id), "team": team}

    def test_the_seed_makes_the_tree_once_and_the_rule_reads_it(self):
        from origin.search_engine.agent.evals.group import seeds  # noqa: PLC0415
        from origin.services.task_ancestry import effective_statuses  # noqa: PLC0415

        for team in ("v1", "ja"):
            titles = seeds.CLOSED_TREE_TITLES["ja" if team == "ja" else "en"]
            seeds.apply(["closed_ancestor_tree"], self.info(team))
            seeds.apply(["closed_ancestor_tree"], self.info(team))
            tasks = {k: TaskMaster.objects.get(title=v) for k, v in titles.items()}
            self.assertEqual(
                [tasks[k].status for k in ("parent", "child_wip", "child_open", "open")],
                ["Closed", "WIP", "Open", "Open"],
            )
            self.assertTrue(all(t.assignee_id == self.user.id for t in tasks.values()))
            got = effective_statuses([t.task_id for t in tasks.values()], self.team.team_id)
            self.assertEqual(got[tasks["child_wip"].task_id].status, "Closed")
            self.assertEqual(got[tasks["child_open"].task_id].status, "Closed")
            self.assertEqual(got[tasks["open"].task_id].status, "Open")
            self.assertEqual(tasks["open"].due_date.isoformat(), "2026-10-01")
            # a project of its own, with the viewer alone: nobody else in the team sees these tasks
            self.assertEqual(tasks["open"].project.code, "ECT")
            self.assertNotEqual(tasks["open"].project_id, self.project.project_id)
            members = ProjectMembers.objects.filter(project=tasks["open"].project)
            self.assertEqual([m.attendee_id for m in members], [self.user.id])
