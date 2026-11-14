"""MIP-1510: a `create_task_plan` task proposed without a body gets one, server-side.

The model often proposes a plan of titles only. The approval card and the created tasks now carry
the project's default task template (api #129) with the title as the Summary line, or a one-line
`### Summary` + title when the project has none. Stubs only: no model is called.

Pinned here:
  * what is filled: a missing, null or blank `content_markdown`; what is not: a body the model
    wrote (byte-identical plan, same object back), the milestone description;
  * which template: the project's own default `custom:{pk}`; a built-in id, a pk of ANOTHER
    project, a missing template, an empty or oversize one all read as "no template";
  * card == created text; the card is built before `_run`'s ACL gate, so it must not show a
    template body to someone who cannot create in the project;
  * the persisted arguments (replayed to the model) are never changed.
"""

import copy

from django.test import override_settings

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTaskTemplate
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN
from origin.search_engine.agent.tools.plan_task_bodies import (
    fill_plan_task_bodies,
    fill_plan_task_bodies_for_caller,
)

from .test_base import BaseAPITestCase


def _text(text, **styles):
    return {"type": "text", "text": text, "styles": styles}


def _heading(text):
    return {"type": "heading", "props": {"level": 3}, "content": [_text(text)], "children": []}


def _para(*runs):
    return {"type": "paragraph", "content": list(runs), "children": []}


def _bullet(text):
    return {"type": "bulletListItem", "content": [_text(text)], "children": []}


# A member-authored template: a heading, the grey italic hint of the built-ins, more sections.
TEMPLATE = [
    _heading("概要"),
    _para(_text("このタスクで届けるものを書く。", italic=True, textColor="gray")),
    _heading("受け入れ条件"),
    _bullet("条件その1"),
    _bullet("条件その2"),
]
NO_HEADING_TEMPLATE = [_para(_text("Definition of done:")), _bullet("tests pass")]

TITLES = ["フィルターUIの設計", "フィルターAPIの実装", "Write the tests"]


class PlanBodiesBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def template(self, body, project=None, name="T"):
        return ProjectTaskTemplate.objects.create(
            team=self.team, project=project or self.project, template_name=name, body=body
        )

    def set_default(self, value, project=None):
        project = project or self.project
        project.default_task_template = value
        project.save(update_fields=["default_task_template"])

    def plan(self, **task_extra):
        return {
            "project_id": self.project.project_id,
            "milestone": {"title": "Search filters beta", "description_markdown": "## Goal\nship"},
            "tasks": [{"title": t, **task_extra} for t in TITLES],
        }

    def run_tool(self, args):
        return CREATE_TASK_PLAN.run(args, self.ctx)

    def created(self):
        return {t.title: t for t in TaskMaster.objects.filter(is_milestone=False)}

    def card(self, args, ctx=None):
        return controller._friendly_arguments(args, "create_task_plan", ctx=ctx or self.ctx)

    def other_project(self, name="Other"):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=name[:3].upper(),
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)
        return other


class NoTemplateTests(PlanBodiesBase):
    def test_created_tasks_get_the_one_line_summary(self):
        self.run_tool(self.plan())
        tasks = self.created()
        self.assertEqual(len(tasks), 3)
        for title in TITLES:
            self.assertEqual(
                blocks_to_markdown(tasks[title].content), f"### Summary\n\n{title}", title
            )
            # The structure the editor needs, not a wall of text.
            self.assertEqual(tasks[title].content[0]["type"], "heading")

    def test_the_card_shows_the_same_bodies(self):
        args = self.plan()
        shown = self.card(args)
        self.assertEqual(
            [t["content_markdown"] for t in shown["tasks"]],
            [f"### Summary\n\n{t}" for t in TITLES],
        )
        self.run_tool(args)
        tasks = self.created()
        for spec in shown["tasks"]:
            self.assertEqual(
                blocks_to_markdown(tasks[spec["title"]].content), spec["content_markdown"]
            )

    def test_missing_null_and_blank_all_count_as_no_body(self):
        args = {
            "project_id": self.project.project_id,
            "tasks": [
                {"title": "a"},
                {"title": "b", "content_markdown": None},
                {"title": "c", "content_markdown": ""},
                {"title": "d", "content_markdown": " \n\t"},
            ],
        }
        out = fill_plan_task_bodies(args, self.project)
        self.assertEqual(
            [t["content_markdown"] for t in out["tasks"]],
            [f"### Summary\n\n{t}" for t in "abcd"],
        )

    def test_title_whitespace_is_collapsed_and_a_titleless_row_is_left_for_validation(self):
        args = {
            "project_id": self.project.project_id,
            "tasks": [{"title": "  two   words \n"}, {"title": "  "}, {"content_markdown": ""}],
        }
        out = fill_plan_task_bodies(args, self.project)
        self.assertEqual(out["tasks"][0]["content_markdown"], "### Summary\n\ntwo words")
        self.assertEqual(out["tasks"][1], {"title": "  "})
        self.assertEqual(out["tasks"][2], {"content_markdown": ""})

    def test_malformed_plans_pass_through(self):
        for args in (
            {"project_id": 1},
            {"project_id": 1, "tasks": "nope"},
            {"project_id": 1, "tasks": [None, "x", 3]},
        ):
            self.assertIs(fill_plan_task_bodies(args, self.project), args)

    def test_the_milestone_description_is_not_touched(self):
        out = fill_plan_task_bodies(self.plan(), self.project)
        self.assertEqual(out["milestone"], self.plan()["milestone"])
        self.run_tool(self.plan())
        self.assertEqual(MilestoneMaster.objects.get().description[0]["type"], "heading")


class TemplateTests(PlanBodiesBase):
    def test_the_default_template_is_filled_with_the_title_as_the_summary_line(self):
        tpl = self.template(TEMPLATE)
        self.set_default(f"custom:{tpl.pk}")
        self.run_tool(self.plan())
        for title, task in self.created().items():
            md = blocks_to_markdown(task.content)
            self.assertEqual(
                md,
                f"### 概要\n\n{title}\n\n### 受け入れ条件\n\n- 条件その1\n- 条件その2",
                title,
            )
            # The template's grey hint is replaced by the title, not kept beside it.
            self.assertNotIn("届けるもの", md)

    def test_the_card_shows_the_templated_body_and_it_matches_what_is_created(self):
        tpl = self.template(TEMPLATE)
        self.set_default(f"custom:{tpl.pk}")
        args = self.plan()
        shown = self.card(args)
        self.assertIn(
            "### 概要\n\nフィルターUIの設計\n\n### 受け入れ条件",
            shown["tasks"][0]["content_markdown"],
        )
        self.run_tool(args)
        tasks = self.created()
        for spec in shown["tasks"]:
            self.assertEqual(
                blocks_to_markdown(tasks[spec["title"]].content), spec["content_markdown"]
            )

    def test_a_template_without_a_heading_gets_the_summary_above_it(self):
        tpl = self.template(NO_HEADING_TEMPLATE)
        self.set_default(f"custom:{tpl.pk}")
        out = fill_plan_task_bodies(self.plan(), self.project)
        self.assertEqual(
            out["tasks"][2]["content_markdown"],
            "### Summary\n\nWrite the tests\n\nDefinition of done:\n\n- tests pass",
        )

    def test_a_builtin_id_unknown_to_the_server_reads_as_no_template(self):
        for value in ("default", "bug", "milestone", "custom:", "custom:abc", "", None):
            self.set_default(value)
            out = fill_plan_task_bodies(self.plan(), self.project)
            self.assertEqual(
                out["tasks"][0]["content_markdown"], f"### Summary\n\n{TITLES[0]}", repr(value)
            )

    def test_a_deleted_or_empty_template_reads_as_no_template(self):
        tpl = self.template([])
        self.set_default(f"custom:{tpl.pk}")
        self.assertEqual(
            fill_plan_task_bodies(self.plan(), self.project)["tasks"][0]["content_markdown"],
            f"### Summary\n\n{TITLES[0]}",
        )
        tpl.delete()
        self.assertEqual(
            fill_plan_task_bodies(self.plan(), self.project)["tasks"][0]["content_markdown"],
            f"### Summary\n\n{TITLES[0]}",
        )

    def test_another_projects_template_is_never_used(self):
        other = self.other_project()
        secret = self.template([_heading("SECRET"), _para(_text("other project body"))], other)
        # `PUT /project/task-template/defaults/` stores any string: this default points at it.
        self.set_default(f"custom:{secret.pk}")
        out = fill_plan_task_bodies(self.plan(), self.project)
        for t in out["tasks"]:
            self.assertNotIn("SECRET", t["content_markdown"])
            self.assertTrue(t["content_markdown"].startswith("### Summary"))
        self.assertNotIn("SECRET", str(self.card(self.plan())))

    def test_an_oversize_template_never_fails_the_plan(self):
        big = self.template([_heading("H"), _para(_text("x" * 5000))])
        self.set_default(f"custom:{big.pk}")
        with override_settings(AGENT_MAX_BODY_BYTES=1000):
            self.run_tool(self.plan())
        for title, task in self.created().items():
            self.assertEqual(blocks_to_markdown(task.content), f"### Summary\n\n{title}")

    def test_the_default_task_template_is_used_not_the_milestone_one(self):
        task_tpl = self.template(TEMPLATE, name="task")
        ms_tpl = self.template([_heading("MILESTONE ONLY")], name="ms")
        self.project.default_milestone_template = f"custom:{ms_tpl.pk}"
        self.project.default_task_template = f"custom:{task_tpl.pk}"
        self.project.save()
        out = fill_plan_task_bodies(self.plan(), self.project)
        self.assertNotIn("MILESTONE ONLY", out["tasks"][0]["content_markdown"])
        self.assertIn("### 概要", out["tasks"][0]["content_markdown"])


class UntouchedTests(PlanBodiesBase):
    def bodies(self):
        return {
            "project_id": self.project.project_id,
            "milestone": {"title": "M", "description_markdown": "## Goal\nship"},
            "tasks": [
                {"title": "A", "content_markdown": "### 🧾 Summary\n\nAlready written."},
                {"title": "B", "content_markdown": "plain *words*", "priority": "High"},
            ],
        }

    def test_a_plan_where_every_task_has_a_body_is_returned_as_is(self):
        tpl = self.template(TEMPLATE)
        self.set_default(f"custom:{tpl.pk}")
        args = self.bodies()
        self.assertIs(fill_plan_task_bodies(args, self.project), args)
        self.assertIs(
            fill_plan_task_bodies_for_caller(args, self.ctx.team_id, self.ctx.user_id), args
        )

    def test_created_tasks_and_the_card_are_byte_identical_to_the_model_text(self):
        tpl = self.template(TEMPLATE)
        self.set_default(f"custom:{tpl.pk}")
        args = self.bodies()
        shown = self.card(args)
        self.assertEqual(shown["tasks"], args["tasks"])
        self.run_tool(copy.deepcopy(args))
        tasks = self.created()
        self.assertEqual(
            blocks_to_markdown(tasks["A"].content), "### 🧾 Summary\n\nAlready written."
        )
        self.assertEqual(blocks_to_markdown(tasks["B"].content), "plain *words*")

    def test_a_mixed_plan_fills_only_the_empty_ones(self):
        args = self.bodies()
        args["tasks"].append({"title": "C"})
        out = fill_plan_task_bodies(args, self.project)
        self.assertEqual(out["tasks"][0], args["tasks"][0])
        self.assertEqual(out["tasks"][1], args["tasks"][1])
        self.assertEqual(out["tasks"][2]["content_markdown"], "### Summary\n\nC")

    def test_the_proposed_arguments_are_never_changed(self):
        """`arguments_json` is replayed to the model as its own call."""
        args = self.plan()
        before = copy.deepcopy(args)
        fill_plan_task_bodies(args, self.project)
        self.card(args)
        self.run_tool(args)
        self.assertEqual(args, before)


class CardAccessTests(PlanBodiesBase):
    def setUp(self):
        super().setUp()
        tpl = self.template([_heading("TEAM SECRET"), _para(_text("body"))])
        self.set_default(f"custom:{tpl.pk}")

    def test_a_member_sees_the_template(self):
        self.assertIn("TEAM SECRET", self.card(self.plan())["tasks"][0]["content_markdown"])

    def test_a_non_member_gets_no_fill_so_the_card_leaks_nothing(self):
        # user2 is on the team but not on the project: `_run` would refuse this plan.
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        shown = self.card(self.plan(), ctx=ctx)
        self.assertNotIn("content_markdown", shown["tasks"][0])
        self.assertNotIn("TEAM SECRET", str(shown))

    def test_a_caller_from_another_team_gets_no_fill(self):
        stranger_team = TeamMaster.objects.create(team_name="Other", owner=self.user2)
        TeamMembers.objects.create(team=stranger_team, attendee=self.user2)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        ctx = ToolContext(team_id=str(stranger_team.team_id), user_id=str(self.user2.id))
        shown = self.card(self.plan(), ctx=ctx)
        self.assertNotIn("TEAM SECRET", str(shown))

    def test_an_unknown_or_deleted_project_gets_no_fill(self):
        for pid in (999999, "not a number", None):
            args = self.plan()
            args["project_id"] = pid
            self.assertNotIn("content_markdown", self.card(args)["tasks"][0], repr(pid))
        self.project.is_deleted = True
        self.project.save(update_fields=["is_deleted"])
        self.assertNotIn("content_markdown", self.card(self.plan())["tasks"][0])

    def test_the_card_fills_even_when_no_assignee_is_named(self):
        # The assignee pass returns early without ids; the fill must not sit behind it.
        args = self.plan()
        self.assertFalse(any(t.get("assignee_id") for t in args["tasks"]))
        self.assertTrue(self.card(args)["tasks"][0]["content_markdown"])

    def test_the_card_still_labels_assignees_when_it_fills(self):
        args = self.plan()
        args["tasks"][0]["assignee_id"] = str(self.user.id)
        shown = self.card(args)
        self.assertTrue(shown["tasks"][0]["content_markdown"])
        self.assertNotEqual(shown["tasks"][0]["assignee_id"], str(self.user.id))


class ToolSurfaceTests(PlanBodiesBase):
    def test_the_result_the_model_reads_carries_no_body(self):
        out = self.run_tool(self.plan())
        self.assertEqual(
            set(out["tasks"][0]),
            {"index", "task_id", "display_id", "title", "parent_task_id", "tags", "assignee_id"},
        )
