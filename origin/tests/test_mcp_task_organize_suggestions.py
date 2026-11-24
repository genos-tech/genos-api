"""MIP-2015: a task made over MCP with no parent task gets `suggestions` in the reply.

The decision (Ken, 2026-11-22, "Genos guides agents with suggestions in tool replies, not hard
rejections"): the write ALWAYS succeeds; the reply adds a short stable list of
`{kind, message, candidates}` when something was probably forgotten. Pinned here: the write is
never refused, a task with a parent says nothing, the in-app agent's reply is untouched, the
candidates stay in the one project and open, the list stays small, and finding them costs two
queries and no model call.
"""

from __future__ import annotations

from unittest import mock

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import create_task as create_task_tool
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import task_organize_suggestions as organize
from origin.services.milestone_service import create_milestone
from origin.tests.test_mcp_endpoint import McpBase


class _Board(McpBase):
    """The base project (WRD) with a small board: an epic with an open child, loose tasks, one
    closed task and one task in another project."""

    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)
        self.n = 7

        self.epic = self._task("Checkout redesign", status="WIP")
        self._task("Checkout payment form validation", parent=self.epic)
        self.loose = self._task("Write release notes for checkout", parent=self.epic)
        self.closed = self._task("Checkout redesign spike", status="Closed")
        self.other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Elsewhere",
            code="ELS",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.other_project, attendee=self.user
        )
        self.foreign = TaskMaster.objects.create(
            team=self.team,
            project=self.other_project,
            reporter=self.user,
            title="Checkout redesign in another project",
            status="Open",
            project_task_number=1,
        )

    def _task(self, title, status="Open", parent=None):
        self.n += 1
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title=title,
            status=status,
            parent_task_id=parent.task_id if parent is not None else None,
            project_task_number=self.n,
        )

    def _create(self, **arguments):
        arguments.setdefault("project_id", self.project.project_id)
        return self._result(self._call("create_task", arguments))["structuredContent"]


class CreateTaskSuggestionsTests(_Board):
    def test_a_task_with_no_parent_is_created_and_gets_parent_and_dependency_candidates(self):
        out = self._create(title="Checkout redesign: mobile release notes")

        task = TaskMaster.objects.get(task_id=out["task_id"])
        self.assertEqual(task.title, "Checkout redesign: mobile release notes")  # never refused
        by_kind = {s["kind"]: s for s in out["suggestions"]}
        self.assertEqual(list(by_kind["parent_task"]), ["kind", "message", "candidates"])
        parents = by_kind["parent_task"]["candidates"]
        self.assertEqual(parents[0]["task_id"], self.epic.task_id)
        self.assertEqual(parents[0]["display_id"], self.epic.display_id)
        self.assertEqual(set(parents[0]), {"task_id", "display_id", "title"})
        self.assertIn("parent_task_id", by_kind["parent_task"]["message"])
        self.assertIn("update_task", by_kind["parent_task"]["message"])
        related = {c["task_id"] for c in by_kind["dependencies"]["candidates"]}
        self.assertIn(self.loose.task_id, related)
        self.assertIn("update_task_dependencies", by_kind["dependencies"]["message"])

    def test_candidates_are_open_tasks_of_the_same_project_and_never_the_new_task(self):
        out = self._create(title="Checkout redesign: mobile layout")

        offered = {c["task_id"] for s in out["suggestions"] for c in s["candidates"]}
        self.assertNotIn(self.closed.task_id, offered)
        self.assertNotIn(self.foreign.task_id, offered)
        self.assertNotIn(out["task_id"], offered)

    def test_at_most_three_candidates_per_entry(self):
        for i in range(6):
            self._task(f"Checkout redesign step {i}")

        out = self._create(title="Checkout redesign: final step")

        for entry in out["suggestions"]:
            self.assertLessEqual(len(entry["candidates"]), 3)

    def test_no_match_still_says_to_set_a_parent_with_no_candidates(self):
        out = self._create(title="Zebra quokka telemetry")

        self.assertEqual(
            [(s["kind"], s["candidates"]) for s in out["suggestions"]],
            [("parent_task", [])],
        )

    def test_a_task_with_neither_parent_nor_milestone_is_told_it_has_no_place(self):
        # MIP-2367: a suggestion, never a refusal; the message names both ways to give it a place.
        out = self._create(title="Zebra quokka telemetry")

        self.assertTrue(TaskMaster.objects.filter(task_id=out["task_id"]).exists())
        entry = next(s for s in out["suggestions"] if s["kind"] == "parent_task")
        self.assertIn("no milestone", entry["message"])
        self.assertIn("milestone_id", entry["message"])

    def test_a_task_with_a_parent_task_gets_no_suggestions(self):
        out = self._create(
            title="Checkout redesign: mobile layout", parent_task_id=self.epic.task_id
        )

        self.assertNotIn("suggestions", out)
        self.assertEqual(out["parent_task_id"], self.epic.task_id)

    def test_a_task_in_a_milestone_but_with_no_parent_task_still_gets_the_hint(self):
        # Built through the real service so the backing task exists.
        milestone = create_milestone(self.project, reporter_id=str(self.user.id), title="Q4 launch")
        out = self._create(
            title="Checkout redesign: mobile layout", milestone_id=milestone.milestone_id
        )

        self.assertEqual(out["milestone_id"], milestone.milestone_id)
        entry = next(s for s in out["suggestions"] if s["kind"] == "parent_task")
        # It has a place already, so the hint says a parent would MOVE it, not that it has none.
        self.assertIn("directly under a milestone", entry["message"])
        self.assertIn("moves to that task's milestone", entry["message"])

    def test_a_failure_while_finding_candidates_never_turns_the_create_into_an_error(self):
        with mock.patch.object(
            create_task_tool, "suggestions_for_new_task", side_effect=RuntimeError("boom")
        ):
            out = self._create(title="Checkout redesign: mobile layout")

        self.assertTrue(TaskMaster.objects.filter(task_id=out["task_id"]).exists())
        self.assertNotIn("suggestions", out)

    def test_the_in_app_agents_reply_is_unchanged(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

        result = create_task_tool._run(
            {"title": "Checkout redesign: mobile layout", "project_id": self.project.project_id},
            ctx,
        )

        self.assertNotIn("suggestions", result)

    def test_finding_candidates_costs_two_queries_and_no_model_call(self):
        # Two reads: the project's open tasks (titles only) and its milestone rows.
        with self.assertNumQueries(2):
            organize.find_candidates(
                project_id=self.project.project_id,
                team_id=str(self.team.team_id),
                title_texts=["Checkout redesign: mobile layout"],
            )


class CreateTaskPlanSuggestionsTests(_Board):
    def _plan(self, **arguments):
        arguments.setdefault("project_id", self.project.project_id)
        return self._result(self._call("create_task_plan", arguments))["structuredContent"]

    def test_a_standalone_batch_gets_one_hint_for_the_whole_batch(self):
        out = self._plan(
            tasks=[
                {"title": "Checkout redesign: mobile layout", "content_markdown": "Layout."},
                {"title": "Checkout redesign: tablet layout", "content_markdown": "Layout."},
            ]
        )

        self.assertEqual(len(out["tasks"]), 2)
        kinds = [s["kind"] for s in out["suggestions"]]
        self.assertEqual(kinds.count("parent_task"), 1)
        parents = next(s for s in out["suggestions"] if s["kind"] == "parent_task")["candidates"]
        self.assertEqual(parents[0]["task_id"], self.epic.task_id)
        offered = {c["task_id"] for s in out["suggestions"] for c in s["candidates"]}
        self.assertFalse(offered & {t["task_id"] for t in out["tasks"]})

    def test_sub_tasks_of_one_task_get_no_suggestions(self):
        out = self._plan(
            parent_task_id=self.epic.task_id,
            tasks=[{"title": "Checkout redesign: mobile layout", "content_markdown": "Layout."}],
        )

        self.assertNotIn("suggestions", out)

    def test_a_new_milestones_tree_gets_no_suggestions(self):
        out = self._plan(
            milestone={"title": "Checkout redesign launch"},
            tasks=[{"title": "Checkout redesign: mobile layout", "content_markdown": "Layout."}],
        )

        self.assertNotIn("suggestions", out)

    def test_a_batch_whose_every_task_is_a_sub_task_of_an_earlier_one_but_the_first_is_not(self):
        out = self._plan(
            tasks=[
                {"title": "Checkout redesign: mobile layout", "content_markdown": "Layout."},
                {
                    "title": "Checkout redesign: mobile icons",
                    "content_markdown": "Icons.",
                    "parent_index": 0,
                },
            ]
        )

        # The first task has no parent task, so the batch still gets its one hint.
        self.assertIn("parent_task", {s["kind"] for s in out["suggestions"]})


class NoDependenciesSuggestionTests(_Board):
    """MIP-2367: a plan of 3+ tasks with no dependency edge gets one more hint (never a refusal)."""

    def _plan(self, **arguments):
        arguments.setdefault("project_id", self.project.project_id)
        return self._result(self._call("create_task_plan", arguments))["structuredContent"]

    @staticmethod
    def _tasks(n, blocked_by=None):
        tasks = [{"title": f"Zebra quokka step {i}", "content_markdown": "Step."} for i in range(n)]
        for index, blockers in (blocked_by or {}).items():
            tasks[index]["blocked_by_indexes"] = blockers
        return tasks

    @staticmethod
    def _kinds(out):
        return [s["kind"] for s in out.get("suggestions", [])]

    def test_three_tasks_and_no_dependency_get_the_hint_and_are_still_created(self):
        out = self._plan(parent_task_id=self.epic.task_id, tasks=self._tasks(3))

        self.assertEqual(len(out["tasks"]), 3)
        self.assertEqual(out["dependencies_created"], 0)
        self.assertEqual(self._kinds(out), ["no_dependencies"])
        entry = out["suggestions"][0]
        self.assertEqual(entry["candidates"], [])
        self.assertIn("update_task_dependencies", entry["message"])

    def test_a_new_milestones_tree_with_no_dependency_gets_the_hint(self):
        out = self._plan(milestone={"title": "Zebra launch"}, tasks=self._tasks(4))

        self.assertEqual(self._kinds(out), ["no_dependencies"])

    def test_one_dependency_is_enough_to_stay_quiet(self):
        out = self._plan(
            parent_task_id=self.epic.task_id, tasks=self._tasks(3, blocked_by={1: [0]})
        )

        self.assertEqual(out["dependencies_created"], 1)
        self.assertNotIn("suggestions", out)

    def test_two_tasks_are_allowed_to_be_independent(self):
        out = self._plan(parent_task_id=self.epic.task_id, tasks=self._tasks(2))

        self.assertNotIn("suggestions", out)

    def test_a_standalone_batch_gets_the_parent_hint_and_the_dependency_hint(self):
        out = self._plan(tasks=self._tasks(3))

        self.assertEqual(self._kinds(out).count("parent_task"), 1)
        self.assertEqual(self._kinds(out).count("no_dependencies"), 1)

    def test_the_in_app_agents_reply_is_unchanged(self):
        from origin.search_engine.agent.tools import create_task_plan as plan_tool

        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        result = plan_tool._run(
            {
                "project_id": self.project.project_id,
                "parent_task_id": self.epic.task_id,
                "tasks": self._tasks(3),
            },
            ctx,
        )

        self.assertNotIn("suggestions", result)

    def test_the_helper_needs_three_tasks_and_zero_edges(self):
        self.assertIsNone(organize.no_dependencies_suggestion(2, 0))
        self.assertIsNone(organize.no_dependencies_suggestion(3, 1))
        self.assertEqual(organize.no_dependencies_suggestion(3, 0)["kind"], "no_dependencies")


class FindCandidatesTests(_Board):
    def _find(self, title, body=""):
        return organize.find_candidates(
            project_id=self.project.project_id,
            team_id=str(self.team.team_id),
            title_texts=[title],
            body_texts=[body],
        )

    def test_a_container_is_offered_as_a_parent_and_a_leaf_as_a_possible_blocker(self):
        parents, related = self._find("Checkout redesign: mobile layout")

        self.assertEqual(parents[0]["task_id"], self.epic.task_id)
        self.assertNotIn(self.epic.task_id, {c["task_id"] for c in related})

    def test_the_body_adds_words_to_the_match(self):
        parents, related = self._find(
            "Mobile layout", body="Part of the checkout redesign: the payment form"
        )

        offered = {c["task_id"] for c in parents + related}
        self.assertIn(self.epic.task_id, offered)

    def test_japanese_titles_match_on_character_pairs(self):
        jp = self._task("決済画面のリニューアル", status="WIP")
        self._task("決済画面の入力チェック", parent=jp)

        parents, _related = self._find("決済画面のスマホ対応")

        self.assertEqual(parents[0]["task_id"], jp.task_id)

    def test_one_shared_japanese_word_or_particle_is_not_a_match(self):
        # "画面の修正" and "メール送信の修正" share only "修正" (and the particle pair "の修"); one
        # shared word must not make every "…の修正" title a candidate.
        self._task("メール送信の修正", status="WIP")
        self._task("検索結果の表示", status="WIP")

        parents, related = self._find("ログイン画面の修正")

        self.assertEqual(parents + related, [])

    def test_japanese_particle_pairs_are_not_tokens(self):
        self.assertEqual(organize._tokens("画面の修正"), {"画面", "修正"})

    def test_a_shared_japanese_word_pair_with_a_second_shared_word_is_a_match(self):
        jp = self._task("ログイン画面の履歴表示", status="WIP")
        self._task("ログイン画面の履歴削除", parent=jp)

        parents, _related = self._find("ログイン画面の履歴を検索")

        self.assertEqual(parents[0]["task_id"], jp.task_id)

    def test_a_plan_under_an_existing_milestone_says_a_parent_would_move_it(self):
        milestone = create_milestone(self.project, reporter_id=str(self.user.id), title="Q4 launch")
        out = self._result(
            self._call(
                "create_task_plan",
                {
                    "project_id": self.project.project_id,
                    "existing_milestone_id": milestone.milestone_id,
                    "tasks": [
                        {"title": "Checkout redesign: mobile layout", "content_markdown": "Layout."}
                    ],
                },
            )
        )["structuredContent"]

        entry = next(s for s in out["suggestions"] if s["kind"] == "parent_task")
        self.assertIn("directly under a milestone", entry["message"])

    def test_an_empty_title_finds_nothing(self):
        self.assertEqual(self._find("   "), ([], []))

    def test_a_long_title_is_cut_for_the_reply(self):
        self._task("Checkout " + "redesign " * 20, status="WIP")

        parents, related = self._find("Checkout redesign")

        longest = max(len(c["title"]) for c in parents + related)
        self.assertLessEqual(longest, 90)
