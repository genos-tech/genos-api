"""Untouched template bodies are not content (MIP-799).

MIP-825 made `get_task` read a body without its untouched prompts. This
covers the rest: search no longer indexes them (task + milestone chunkers),
agents can see which milestones have no goal (`has_goal`), tool-created
tasks store an empty body, and the one-off command empties the stored rows.
"""

from io import StringIO
from unittest import mock

from django.core.management import call_command

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.get_milestone_summary import GET_MILESTONE_SUMMARY
from origin.search_engine.agent.tools.list_milestones import LIST_MILESTONES
from origin.search_engine.agent.tools.template_prompts import (
    has_written_body,
    is_untouched_template,
)
from origin.search_engine.chunkers.milestone_chunker import iter_milestone_chunks
from origin.search_engine.chunkers.task_chunker import iter_task_chunks
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase
from .test_template_prompts import _p, _template

COMMAND = "origin.management.commands.clean_untouched_template_bodies.push_task_body"


def _touched_template():
    body = _template()
    body[4] = _p("Users lose their draft when the tab closes.")
    return body


class _ProjectCase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Kraken",
            code="KDS",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _task(self, content, **kw):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title=kw.pop("title", "A task"),
            status="Open",
            content=content,
            **kw,
        )

    def _milestone(self, description, title="A milestone"):
        m = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title=title, description=description
        )
        backing = ensure_backing_task(m)
        TaskMaster.objects.filter(pk=backing.pk).update(content=description)
        m.refresh_from_db()
        return m


class HelperTests(BaseAPITestCase):
    def test_has_written_body(self):
        self.assertFalse(has_written_body(None))
        self.assertFalse(has_written_body([]))
        self.assertFalse(has_written_body(_template()))
        self.assertTrue(has_written_body(_touched_template()))
        self.assertTrue(has_written_body([_p("Ship it.")]))

    def test_is_untouched_template(self):
        self.assertTrue(is_untouched_template(_template()))
        self.assertFalse(is_untouched_template(_touched_template()))
        # Empty is not "a template": nothing to clean.
        self.assertFalse(is_untouched_template([]))
        self.assertFalse(is_untouched_template(None))
        self.assertFalse(is_untouched_template([_p("Ship it.")]))


class ChunkerTests(_ProjectCase):
    def _task_text(self, task):
        for ec in iter_task_chunks():
            if ec.entity_id == f"task:{task.task_id}":
                return "\n".join(c.search_text for c in ec.chunks)
        return None

    def test_untouched_prompts_are_not_indexed_for_a_task(self):
        t = self._task(_template(), title="Fix login")
        text = self._task_text(t)
        self.assertEqual(text, "Fix login")
        self.assertNotIn("Why does this matter", text)

    def test_real_text_next_to_prompts_is_still_indexed(self):
        t = self._task(_touched_template(), title="Fix login")
        text = self._task_text(t)
        self.assertIn("Users lose their draft when the tab closes.", text)
        self.assertNotIn("First condition that must be true", text)
        self.assertNotIn("Second condition", text)

    def test_the_stored_task_body_is_not_rewritten_by_indexing(self):
        t = self._task(_template())
        self._task_text(t)
        t.refresh_from_db()
        self.assertEqual(t.content, _template())

    def test_untouched_prompts_are_not_indexed_for_a_milestone(self):
        m = self._milestone(_template(), title="Launch")
        chunks = [
            ec for ec in iter_milestone_chunks() if ec.entity_id == f"milestone:{m.milestone_id}"
        ]
        self.assertEqual(len(chunks), 1)
        text = chunks[0].chunks[0].search_text
        self.assertEqual(text, "Launch")
        self.assertNotIn("What outcome does this milestone deliver", text)

    def test_a_written_milestone_goal_is_indexed(self):
        body = _template()
        body[1] = _p("Ship the mobile app to all teams.")
        m = self._milestone(body, title="Launch")
        chunks = [
            ec for ec in iter_milestone_chunks() if ec.entity_id == f"milestone:{m.milestone_id}"
        ]
        text = chunks[0].chunks[0].search_text
        self.assertIn("Ship the mobile app to all teams.", text)


class HasGoalTests(_ProjectCase):
    def test_list_milestones_flags_the_ones_without_a_goal(self):
        empty = self._milestone(None, title="Empty")
        bare = self._milestone(_template(), title="Bare template")
        written = self._milestone(_touched_template(), title="Written")
        out = LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        by_id = {m["milestone_id"]: m["has_goal"] for m in out["milestones"]}
        self.assertEqual(
            by_id,
            {empty.milestone_id: False, bare.milestone_id: False, written.milestone_id: True},
        )

    def test_get_milestone_summary_carries_has_goal(self):
        bare = self._milestone(_template(), title="Bare template")
        written = self._milestone(_touched_template(), title="Written")
        self.assertFalse(
            GET_MILESTONE_SUMMARY.run({"milestone_id": bare.milestone_id}, self.ctx)["has_goal"]
        )
        self.assertTrue(
            GET_MILESTONE_SUMMARY.run({"milestone_id": written.milestone_id}, self.ctx)["has_goal"]
        )


class ToolCreateStoresEmptyBodyTests(_ProjectCase):
    def test_a_task_created_without_a_body_stores_an_empty_one(self):
        out = CREATE_TASK.run({"title": "T", "project_id": self.project.project_id}, self.ctx)
        task = TaskMaster.objects.get(task_id=out["task_id"])
        self.assertEqual(task.content, [])


class CleanupCommandTests(_ProjectCase):
    def _run(self, *args):
        out = StringIO()
        with mock.patch(COMMAND) as push:
            call_command("clean_untouched_template_bodies", *args, stdout=out)
        return out.getvalue(), push

    def test_dry_run_lists_rows_and_writes_nothing(self):
        t = self._task(_template())
        m = self._milestone(_template())
        text, push = self._run()
        self.assertIn(f"task {t.task_id}", text)
        self.assertIn(f"milestone {m.milestone_id}", text)
        self.assertIn("DRY RUN", text)
        push.assert_not_called()
        t.refresh_from_db()
        m.refresh_from_db()
        self.assertEqual(t.content, _template())
        self.assertEqual(m.description, _template())
        self.assertEqual(m.task.content, _template())

    def test_dry_run_and_apply_report_the_same_counts(self):
        self._task(_template())
        self._milestone(_template())
        dry, _ = self._run()
        applied, _ = self._run("--apply")
        # 1 milestone (with its backing task folded in) + 1 plain task.
        self.assertIn("1 milestone description(s) and 1 task body(ies)", dry)
        self.assertIn("1 milestone description(s) and 1 task body(ies)", applied)

    def test_apply_empties_both_copies_and_pushes_the_editor_doc(self):
        t = self._task(_template())
        m = self._milestone(_template())
        backing_id = m.task_id
        _, push = self._run("--apply")
        t.refresh_from_db()
        m.refresh_from_db()
        self.assertEqual(t.content, [])
        self.assertEqual(m.description, [])
        self.assertEqual(m.task.content, [])
        pushed = {c.args[0] for c in push.call_args_list}
        self.assertEqual(pushed, {t.task_id, backing_id})
        for c in push.call_args_list:
            self.assertEqual(c.args[1], [])

    def test_apply_leaves_ts_updated_at_alone(self):
        t = self._task(_template())
        m = self._milestone(_template())
        before_task = TaskMaster.objects.get(pk=t.pk).ts_updated_at
        before_ms = MilestoneMaster.objects.get(pk=m.pk).ts_updated_at
        before_backing = TaskMaster.objects.get(pk=m.task_id).ts_updated_at
        self._run("--apply")
        self.assertEqual(TaskMaster.objects.get(pk=t.pk).ts_updated_at, before_task)
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).ts_updated_at, before_ms)
        self.assertEqual(TaskMaster.objects.get(pk=m.task_id).ts_updated_at, before_backing)

    def test_a_touched_or_empty_or_deleted_row_is_not_changed(self):
        touched = self._task(_touched_template())
        empty = self._task([])
        deleted = self._task(_template(), is_deleted=True)
        written_ms = self._milestone(_touched_template())
        _, push = self._run("--apply")
        touched.refresh_from_db()
        empty.refresh_from_db()
        deleted.refresh_from_db()
        written_ms.refresh_from_db()
        self.assertEqual(touched.content, _touched_template())
        self.assertEqual(empty.content, [])
        self.assertEqual(deleted.content, _template())
        self.assertEqual(written_ms.description, _touched_template())
        push.assert_not_called()

    def test_team_scope(self):
        t = self._task(_template())
        text, _ = self._run("--apply", "--team", "00000000-0000-0000-0000-000000000000")
        t.refresh_from_db()
        self.assertEqual(t.content, _template())
        self.assertIn("0 milestone description(s) and 0 task body(ies)", text)
