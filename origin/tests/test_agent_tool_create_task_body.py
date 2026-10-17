"""`create_task` parses markdown and files under a milestone (MIP-691, MIP-693).

`create_task` used to split its body on newlines, so `### Summary` and
`- item` were stored as literal paragraph text while `update_task` parsed
the same input as markdown. Every assertion here is on block TYPES:
`content_markdown` reconstructs a literal `###` paragraph as `###`, so a
text comparison passes on exactly the broken body.
"""

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase

BODY = "### Summary\n\nSome **prose**.\n\n- first\n- second\n\n- [ ] todo\n\n```py\nx = 1\n```"
EXPECTED_TYPES = ["heading", "paragraph", "bulletListItem", "bulletListItem", "checkListItem"]


def _types(blocks):
    return [b["type"] for b in blocks]


def _shape(blocks):
    """Blocks with their per-write random ids removed."""
    out = []
    for b in blocks:
        b = {k: v for k, v in b.items() if k != "id"}
        b["children"] = _shape(b.get("children") or [])
        out.append(b)
    return out


class CreateTaskBodyTests(BaseAPITestCase):
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

    def _create(self, **args):
        out = CREATE_TASK.run(
            {"title": "T", "project_id": self.project.project_id, **args}, self.ctx
        )
        return TaskMaster.objects.get(task_id=out["task_id"]), out

    def _milestone(self, project=None, **kwargs):
        milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=project or self.project,
            reporter=self.user,
            title=kwargs.pop("title", "v1.0"),
            status="Open",
            **kwargs,
        )
        ensure_backing_task(milestone)
        return milestone

    # ---- markdown ---------------------------------------------------

    def test_markdown_body_becomes_real_blocks(self):
        task, _ = self._create(content_text=BODY)
        self.assertEqual(_types(task.content)[:5], EXPECTED_TYPES)
        self.assertIn("codeBlock", _types(task.content))
        self.assertEqual(task.content[0]["props"]["level"], 3)
        self.assertNotIn("###", task.content[0]["content"][0]["text"])

    def test_body_keeps_editor_shape(self):
        """Every top-level block has an id, and the trailing blank
        paragraph the editor always carries is there."""
        task, _ = self._create(content_text=BODY)
        self.assertTrue(all(b.get("id") for b in task.content))
        self.assertEqual(len({b["id"] for b in task.content}), len(task.content))
        self.assertEqual(task.content[-1]["type"], "paragraph")
        self.assertEqual(task.content[-1]["content"], [])

    def test_empty_body_stays_empty(self):
        task, _ = self._create(content_text="   ")
        self.assertEqual(task.content, [])
        task, _ = self._create()
        self.assertEqual(task.content, [])

    def test_create_update_and_plan_produce_the_same_document(self):
        """One implementation: identical input, identical blocks, whichever
        tool wrote it."""
        created, _ = self._create(content_text=BODY)

        target, _ = self._create()
        UPDATE_TASK.run({"task_id": target.task_id, "content_text": BODY}, self.ctx)
        target.refresh_from_db()

        out = CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "tasks": [{"title": "P", "content_markdown": BODY}],
            },
            self.ctx,
        )
        planned = TaskMaster.objects.get(task_id=out["tasks"][0]["task_id"])

        self.assertEqual(_shape(created.content), _shape(target.content))
        self.assertEqual(_shape(created.content), _shape(planned.content))

    # ---- milestone --------------------------------------------------

    def test_milestone_files_task_like_the_plan_tool(self):
        sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S1",
            sequence_number=1,
            start_date="2026-10-01",
            end_date="2026-10-14",
        )
        milestone = self._milestone(sprint=sprint)
        task, out = self._create(milestone_id=milestone.milestone_id)

        self.assertEqual(task.milestone_id, milestone.milestone_id)
        self.assertEqual(task.parent_task_id, milestone.task_id)
        self.assertEqual(task.root_task_id, milestone.task_id)
        self.assertEqual(task.sprint_id, sprint.sprint_id)
        self.assertEqual(out["milestone_id"], milestone.milestone_id)
        self.assertIn("v1.0", out["__summary__"])

        plan_out = CREATE_TASK_PLAN.run(
            {
                "project_id": self.project.project_id,
                "existing_milestone_id": milestone.milestone_id,
                "tasks": [{"title": "P"}],
            },
            self.ctx,
        )
        planned = TaskMaster.objects.get(task_id=plan_out["tasks"][0]["task_id"])
        linkage = ("milestone_id", "parent_task_id", "root_task_id", "sprint_id")
        self.assertEqual(
            [getattr(task, f) for f in linkage], [getattr(planned, f) for f in linkage]
        )

    def test_no_milestone_leaves_task_standalone(self):
        task, out = self._create()
        self.assertIsNone(task.milestone_id)
        self.assertIsNone(task.parent_task_id)
        self.assertEqual(task.root_task_id, task.task_id)
        self.assertIsNone(out["milestone_id"])

    def test_milestone_in_another_project_is_refused(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        milestone = self._milestone(project=other)
        with self.assertRaisesRegex(ToolError, "belongs to project"):
            self._create(milestone_id=milestone.milestone_id)
        self.assertEqual(TaskMaster.objects.filter(is_milestone=False).count(), 0)

    def test_milestone_in_another_team_is_refused(self):
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        milestone = self._milestone()
        MilestoneMaster.objects.filter(pk=milestone.pk).update(team=other_team)
        with self.assertRaisesRegex(ToolError, "different team"):
            self._create(milestone_id=milestone.milestone_id)

    def test_unknown_deleted_or_garbage_milestone_is_refused(self):
        with self.assertRaisesRegex(ToolError, "not found"):
            self._create(milestone_id=999999)
        deleted = self._milestone()
        MilestoneMaster.objects.filter(pk=deleted.pk).update(is_deleted=True)
        with self.assertRaisesRegex(ToolError, "not found"):
            self._create(milestone_id=deleted.milestone_id)
        with self.assertRaisesRegex(ToolError, "must be an integer"):
            self._create(milestone_id="soon")
        self.assertEqual(TaskMaster.objects.filter(is_milestone=False).count(), 0)
