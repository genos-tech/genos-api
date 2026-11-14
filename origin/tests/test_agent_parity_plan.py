"""`create_task_plan` tasks take `status` and `template` (MIP-1561, flag `AGENT_PARITY_TOOLS`).

A plan's tasks were all created Open, and could not name a body template; the project's default
template (MIP-1510) was the only fill. With the flag on each task may start in another status and
name its template, a project default that is a built-in applies, and the approval card shows the
same bodies the tasks are created with. Stubs only: no model is called.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings

from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTaskTemplate
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN
from origin.search_engine.agent.tools.plan_task_bodies import (
    BUILT_IN_HEADINGS,
    fill_plan_task_bodies,
    fill_plan_task_bodies_for_caller,
)

from .test_agent_parity_create_task import CUSTOM
from .test_base import BaseAPITestCase

PARITY = {"AGENT_PARITY_TOOLS": True}


class ParityPlanBase(BaseAPITestCase):
    flag = True

    def setUp(self):
        super().setUp()
        if self.flag:
            patcher = mock.patch.dict(settings.SEARCH_ENGINE, PARITY)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def plan(self, *tasks):
        return {
            "project_id": self.project.project_id,
            "tasks": [dict(t) for t in tasks] or [{"title": "A"}],
        }

    def run_tool(self, args):
        return CREATE_TASK_PLAN.run(args, self.ctx)

    def created(self):
        return {t.title: t for t in TaskMaster.objects.filter(is_milestone=False)}

    def body(self, task):
        return blocks_to_markdown(task.content)

    def template(self, body=None, name="Design doc"):
        return ProjectTaskTemplate.objects.create(
            team=self.team,
            project=self.project,
            template_name=name,
            body=CUSTOM if body is None else body,
        )

    def card(self, args, ctx=None):
        return controller._friendly_arguments(args, "create_task_plan", ctx=ctx or self.ctx)


class StatusTests(ParityPlanBase):
    def test_each_task_starts_in_its_own_status(self):
        out = self.run_tool(
            self.plan(
                {"title": "A"},
                {"title": "B", "status": "WIP"},
                {"title": "C", "status": "Closed"},
                {"title": "D", "status": "Pending"},
            )
        )

        tasks = self.created()
        self.assertEqual(
            {t: tasks[t].status for t in tasks},
            {"A": "Open", "B": "WIP", "C": "Closed", "D": "Pending"},
        )
        self.assertEqual(
            [row["status"] for row in out["tasks"]], ["Open", "WIP", "Closed", "Pending"]
        )

    def test_deleted_and_unknown_statuses_are_refused_and_nothing_is_created(self):
        for bad in ("Deleted", "Done"):
            with self.subTest(status=bad):
                with self.assertRaises(ToolError) as caught:
                    self.run_tool(self.plan({"title": "A"}, {"title": "B", "status": bad}))
                self.assertIn("tasks[1]", str(caught.exception))
                self.assertEqual(TaskMaster.objects.count(), 0)

    def test_a_blocker_still_wins_over_an_active_status_as_it_does_in_the_app(self):
        # The plan's edges exist after the rows do: an Open, WIP or Pending task with an open
        # blocker is Blocked; a Closed one is never reopened (`task_blocking`).
        out = self.run_tool(
            self.plan(
                {"title": "Blocker"},
                {"title": "Active", "status": "WIP", "blocked_by_indexes": [0]},
                {"title": "Done", "status": "Closed", "blocked_by_indexes": [0]},
            )
        )

        tasks = self.created()
        self.assertEqual(tasks["Active"].status, "Blocked")
        self.assertEqual(tasks["Done"].status, "Closed")
        self.assertEqual(TaskDependency.objects.count(), 2)
        self.assertEqual([row["status"] for row in out["tasks"]], ["Open", "Blocked", "Closed"])


class TemplateTests(ParityPlanBase):
    def test_a_task_names_its_template(self):
        self.template()

        self.run_tool(
            self.plan(
                {"title": "Crash", "template": "bug"},
                {"title": "Doc it", "template": "design doc"},
                {"title": "Plain"},
            )
        )

        tasks = self.created()
        self.assertTrue(
            self.body(tasks["Crash"]).startswith(f"### {BUILT_IN_HEADINGS['bug'][0]}\n\nCrash")
        )
        self.assertEqual(
            self.body(tasks["Doc it"]), "### Context\n\nDoc it\n\n### Done when\n\n- tests pass"
        )
        self.assertEqual(self.body(tasks["Plain"]), "### Summary\n\nPlain")

    def test_an_unknown_template_is_refused_with_the_names_and_nothing_is_created(self):
        with self.assertRaises(ToolError) as caught:
            self.run_tool(self.plan({"title": "A", "template": "Nope"}))

        self.assertIn("default, bug, spike, milestone", str(caught.exception))
        self.assertEqual(TaskMaster.objects.count(), 0)

    def test_a_template_and_a_body_on_one_task_are_refused(self):
        with self.assertRaises(ToolError) as caught:
            self.run_tool(
                self.plan({"title": "A", "template": "bug", "content_markdown": "my own body"})
            )

        self.assertIn("tasks[0]", str(caught.exception))
        self.assertIn("not both", str(caught.exception))
        self.assertEqual(TaskMaster.objects.count(), 0)

    def test_a_body_the_model_wrote_is_never_touched(self):
        self.run_tool(self.plan({"title": "A", "content_markdown": "mine"}))

        self.assertEqual(self.body(self.created()["A"]), "mine")

    def test_a_built_in_the_project_made_its_default_applies(self):
        self.project.default_task_template = "spike"
        self.project.save(update_fields=["default_task_template"])

        self.run_tool(self.plan({"title": "How do we cache?"}))

        self.assertTrue(
            self.body(self.created()["How do we cache?"]).startswith(
                f"### {BUILT_IN_HEADINGS['spike'][0]}\n\nHow do we cache?"
            )
        )

    def test_the_custom_default_still_applies_to_a_task_that_names_nothing(self):
        template = self.template()
        self.project.default_task_template = f"custom:{template.pk}"
        self.project.save(update_fields=["default_task_template"])

        self.run_tool(self.plan({"title": "A"}, {"title": "B", "template": "bug"}))

        tasks = self.created()
        self.assertTrue(self.body(tasks["A"]).startswith("### Context\n\nA"))
        self.assertTrue(self.body(tasks["B"]).startswith("### 🐞 Summary\n\nB"))

    def test_the_card_shows_the_bodies_the_tasks_are_created_with(self):
        self.template()
        args = self.plan(
            {"title": "Crash", "template": "bug"}, {"title": "Doc", "template": "Design doc"}
        )

        shown = self.card(args)["tasks"]
        self.run_tool(args)

        tasks = self.created()
        self.assertEqual(shown[0]["content_markdown"], self.body(tasks["Crash"]))
        self.assertEqual(shown[1]["content_markdown"], self.body(tasks["Doc"]))

    def test_the_card_leaves_a_task_with_an_unknown_template_unfilled(self):
        args = self.plan({"title": "A", "template": "Nope"}, {"title": "B"})

        shown = self.card(args)["tasks"]

        self.assertNotIn("content_markdown", shown[0])
        self.assertEqual(shown[1]["content_markdown"], "### Summary\n\nB")

    def test_the_card_gives_a_caller_who_cannot_create_in_the_project_no_template_body(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        args = self.plan({"title": "A", "template": "bug"})

        self.assertIs(fill_plan_task_bodies_for_caller(args, ctx.team_id, ctx.user_id), args)

    def test_the_persisted_arguments_are_not_changed(self):
        args = self.plan({"title": "A", "template": "bug"})
        before = {**args, "tasks": [dict(t) for t in args["tasks"]]}

        self.card(args)
        self.run_tool(args)

        self.assertEqual(args, before)


class FlagOffTests(ParityPlanBase):
    flag = False

    def test_status_and_template_are_ignored_and_the_tasks_are_what_they_were(self):
        self.run_tool(
            self.plan({"title": "A", "status": "Closed", "template": "bug"}, {"title": "B"})
        )

        tasks = self.created()
        self.assertEqual({t.status for t in tasks.values()}, {"Open"})
        self.assertEqual(self.body(tasks["A"]), "### Summary\n\nA")

    def test_a_built_in_default_is_still_the_one_line_summary(self):
        self.project.default_task_template = "bug"
        self.project.save(update_fields=["default_task_template"])

        self.run_tool(self.plan({"title": "A"}))

        self.assertEqual(self.body(self.created()["A"]), "### Summary\n\nA")

    def test_the_fill_ignores_a_template_the_model_sent(self):
        args = self.plan({"title": "A", "template": "bug"})

        filled = fill_plan_task_bodies(args, self.project)

        self.assertEqual(filled["tasks"][0]["content_markdown"], "### Summary\n\nA")
        self.assertNotIn("status", self.run_tool(args)["tasks"][0])
