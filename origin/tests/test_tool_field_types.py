"""One field, one type, whichever tool returns it (MIP-781).

`get_task` returned `project_id` as the string "3" while `list_tasks` returned
the number 3 for the same task, so a client comparing ids had to convert. And
an unset priority came back as "" from some rows and null from others: the REST
writers store either, and the tools passed it through, so "" looked like a real
value. Now `project_id` is a number and an unset priority / effort is null.
"""

from unittest import mock

from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.controller import _note_source
from origin.search_engine.agent.tools import REGISTRY, ToolContext, search_kb
from origin.search_engine.agent.tools.get_milestone_summary import GET_MILESTONE_SUMMARY
from origin.search_engine.agent.tools.get_my_focus_tasks import GET_MY_FOCUS_TASKS
from origin.search_engine.agent.tools.list_milestones import LIST_MILESTONES
from origin.search_engine.agent.tools.list_tasks import LIST_TASKS
from origin.services.milestone_service import ensure_backing_task

from .test_base import BaseAPITestCase


class ToolFieldTypeTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Genos",
            code="GEN",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v1", status="Open"
        )
        ensure_backing_task(self.milestone)
        # The same meaning, stored the two ways the REST writers store it.
        self.blank = self._task("blank", priority="", effort_level="")
        self.null = self._task("null", priority=None, effort_level=None)

    def _task(self, title, **kw):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title=title,
            status="Open",
            milestone=self.milestone,
            **kw,
        )

    # -- project_id is a number --

    def test_project_id_is_a_number_in_get_task_and_list_tasks(self):
        fetched = REGISTRY["fetch_task"].run({"task_id": self.null.task_id}, self.ctx)
        listed = LIST_TASKS.run({"project_id": self.project.project_id}, self.ctx)
        row = next(t for t in listed["tasks"] if t["task_id"] == self.null.task_id)
        self.assertIsInstance(fetched["project_id"], int)
        self.assertEqual(fetched["project_id"], row["project_id"])
        self.assertEqual(fetched["project_id"], self.project.project_id)

    def test_project_id_is_a_number_in_the_milestone_summary(self):
        out = GET_MILESTONE_SUMMARY.run({"milestone_id": self.milestone.milestone_id}, self.ctx)
        self.assertIsInstance(out["project_id"], int)

    def test_search_hits_report_project_id_as_a_number(self):
        hit = {"entity_type": "task", "entity_id": "task:1", "project_id": str(self.project.pk)}
        with mock.patch.object(search_kb, "search", return_value={"results": [hit]}):
            out = search_kb.SEARCH_KNOWLEDGE_BASE.run({"query": "q"}, self.ctx)
        self.assertEqual(out["matches"][0]["project_id"], self.project.project_id)
        self.assertIsInstance(out["matches"][0]["project_id"], int)

    def test_a_hit_with_no_project_stays_null(self):
        hit = {"entity_type": "chat", "entity_id": "chat:1", "project_id": None}
        with mock.patch.object(search_kb, "search", return_value={"results": [hit]}):
            out = search_kb.SEARCH_KNOWLEDGE_BASE.run({"query": "q"}, self.ctx)
        self.assertIsNone(out["matches"][0]["project_id"])

    def test_a_task_notes_parent_context_carries_a_numeric_project_id(self):
        note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            owner=self.user,
            task=self.null,
            title="n",
            body=[],
        )
        out = REGISTRY["fetch_note"].run({"note_type": "task", "note_id": note.note_id}, self.ctx)
        self.assertEqual(out["parent_context"]["project_id"], self.project.project_id)

    def test_the_note_source_chip_still_carries_a_string(self):
        # The chip is what the front end deep-links with; only the tool result changed.
        chip = _note_source("task", 5, "n", {"project_id": 7, "task_id": "3"})
        self.assertEqual(chip["project_id"], "7")
        self.assertIsNone(_note_source("personal", 5, "n", {})["project_id"])

    # -- an unset priority / effort is null --

    def test_get_task_reports_both_storage_forms_of_unset_as_null(self):
        for task in (self.blank, self.null):
            with self.subTest(task=task.title):
                out = REGISTRY["fetch_task"].run({"task_id": task.task_id}, self.ctx)
                self.assertIsNone(out["priority"])
                self.assertIsNone(out["effort_level"])

    def test_list_tasks_reports_both_storage_forms_of_unset_as_null(self):
        out = LIST_TASKS.run({"milestone_id": self.milestone.milestone_id}, self.ctx)
        rows = {t["title"]: t for t in out["tasks"]}
        for title in ("blank", "null"):
            self.assertIsNone(rows[title]["priority"], title)
            self.assertIsNone(rows[title]["effort_level"], title)

    def test_focus_tasks_report_both_storage_forms_of_unset_as_null(self):
        out = GET_MY_FOCUS_TASKS.run({}, self.ctx)
        rows = {t["title"]: t for t in out["tasks"]}
        self.assertIsNone(rows["blank"]["priority"])
        self.assertIsNone(rows["null"]["priority"])

    def test_milestone_summary_ready_rows_report_unset_as_null(self):
        out = GET_MILESTONE_SUMMARY.run({"milestone_id": self.milestone.milestone_id}, self.ctx)
        rows = {t["title"]: t for t in out["ready_now"]}
        for title in ("blank", "null"):
            self.assertIsNone(rows[title]["priority"], title)
            self.assertIsNone(rows[title]["effort_level"], title)

    def test_list_milestones_reports_both_storage_forms_of_unset_as_null(self):
        other = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="v2", status="Open"
        )
        MilestoneMaster.objects.filter(pk=other.pk).update(priority="")
        MilestoneMaster.objects.filter(pk=self.milestone.pk).update(priority=None)
        out = LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        self.assertEqual({m["priority"] for m in out["milestones"]}, {None})

    def test_a_real_value_is_left_alone(self):
        TaskMaster.objects.filter(pk=self.null.pk).update(priority="High", effort_level="Low")
        out = REGISTRY["fetch_task"].run({"task_id": self.null.task_id}, self.ctx)
        self.assertEqual((out["priority"], out["effort_level"]), ("High", "Low"))
