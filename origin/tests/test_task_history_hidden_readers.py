"""The other readers of a task's history rows follow the same rule as the activity feed
(MIP-2536): the progress feed, the agent work report and the briefing's wording.

Setup as in `test_task_history_hidden_custom`: `self.user` is on both projects, `self.user2`
only on the destination.
"""

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import briefing
from origin.services.history_labels import HIDDEN_VALUE
from origin.views.public.mcp.progress.get_progress_history import GET_PROGRESS_HISTORY

from . import test_task_history_hidden_custom as custom

PROGRESS_URL = "/api/v2/task/progress/"
WORK_URL = "/api/v2/agent/work/"
SECRET = (*custom.SECRET, "fifty grand")
KEY = {"kind": "api_key", "key_id": "k-1"}


class HistoryOtherReaderTests(custom.MovedTaskSetup):
    def _progress(self, viewer, task=None):
        self.authenticate(viewer)
        res = self.client.get(PROGRESS_URL, {"task_id": (task or self.task).pk, "limit": 100})
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()["entries"]

    def _hide_old_project(self):
        ProjectMaster.objects.filter(pk=self.old.pk).update(is_private=True, is_hidden=True)

    def _custom_rows(self, entries):
        return [e for e in entries if e["actionType"] == TaskActivityActionType.CUSTOM_FIELD]

    def test_the_progress_feed_cuts_the_names_for_a_reader_off_the_old_project(self):
        entries = self._progress(self.user2)
        for secret in SECRET:
            self.assertNotIn(secret, repr(entries))
        rows = self._custom_rows(entries)
        self.assertTrue(rows)
        for row in rows:
            self.assertNotIn("fieldName", row["metadata"])
            self.assertIn(row["newValue"], (None, HIDDEN_VALUE))

    def test_the_progress_feed_does_not_name_a_hidden_project_the_reader_is_not_on(self):
        self.assertIn("Vault", repr(self._progress(self.user2)))
        self._hide_old_project()
        self.assertNotIn("Vault", repr(self._progress(self.user2)))
        self.assertIn("Vault", repr(self._progress(self.user)))

    def test_the_progress_feed_keeps_the_names_for_a_reader_on_the_old_project(self):
        text = repr(self._progress(self.user))
        for secret in ("Client", "Zeta Corp", "Budget note", "Legal hold", "Vault"):
            self.assertIn(secret, text)

    def test_a_sub_tasks_current_project_does_not_pass_by_itself(self):
        # Both readers are on the parent's project; only `user` is on the sub-task's.
        parent = self._task("parent", self.new)
        child = TaskMaster.objects.create(
            team=self.team,
            project=self.old,
            reporter=self.user,
            title="child",
            status="Open",
            parent_task_id=parent.task_id,
        )
        field = self._field(self.old, "Child secret", "text")
        child.custom_field_values = {str(field.field_id): "hush"}
        child.save()
        self.assertNotIn("Child secret", repr(self._progress(self.user2, parent)))
        self.assertNotIn("hush", repr(self._progress(self.user2, parent)))
        self.assertIn("Child secret", repr(self._progress(self.user, parent)))

    def test_nothing_stored_changes(self):
        before = list(TaskActivity.objects.order_by("pk").values())
        self._progress(self.user2)
        self._work(self.user2)
        self.assertEqual(list(TaskActivity.objects.order_by("pk").values()), before)

    def test_the_progress_tool_carries_no_name_or_value(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        result = GET_PROGRESS_HISTORY.run({"task_id": self.task.pk, "limit": 100}, ctx)
        self.assertTrue(result["entries"])
        for secret in SECRET:
            self.assertNotIn(secret, repr(result))

    # -- the agent work report ------------------------------------------------------------- #

    def _work(self, viewer):
        self.authenticate(viewer)
        res = self.client.get(WORK_URL, {"team_id": str(self.team.team_id), "limit": 200})
        self.assertEqual(res.status_code, 200, res.content)
        return [i for group in res.json()["groups"] for i in group["items"]]

    def _edit_text_field(self):
        """A custom field row with a typed value on both sides, written in the old project."""
        row = TaskActivity.objects.filter(
            task=self.task,
            action_type=TaskActivityActionType.CUSTOM_FIELD,
            metadata__fieldName="Budget note",
        ).get()
        row.pk = None
        row.old_value, row.new_value = "forty grand", "fifty grand"
        row.metadata = {**row.metadata, "via": KEY}
        row.actor = self.user
        row.save()
        return row

    def _text_of(self, viewer, row):
        items = {i["id"]: i for i in self._work(viewer)}
        return items[f"activity:{row.pk}"]["text"]

    def test_the_work_report_has_no_value_of_a_project_the_reader_is_not_on(self):
        row = self._edit_text_field()
        self.assertEqual(row.project_id, self.old.project_id)
        self.assertIsNone(self._text_of(self.user2, row))
        for secret in SECRET:
            self.assertNotIn(secret, repr(self._work(self.user2)))

    def test_the_work_report_keeps_the_value_for_a_reader_on_that_project(self):
        row = self._edit_text_field()
        self.assertEqual(self._text_of(self.user, row), "forty grand → fifty grand")

    def test_the_work_report_keeps_a_value_of_the_tasks_current_project(self):
        row = self._edit_text_field()
        TaskActivity.objects.filter(pk=row.pk).update(project=self.new)
        self.assertEqual(self._text_of(self.user2, row), "forty grand → fifty grand")

    def test_the_work_report_cuts_a_row_with_no_project(self):
        row = self._edit_text_field()
        TaskActivity.objects.filter(pk=row.pk).update(project=None)
        self.assertIsNone(self._text_of(self.user, row))

    # -- the briefing's words -------------------------------------------------------------- #

    def test_the_briefing_names_no_custom_field_to_anyone(self):
        rows = TaskActivity.objects.filter(
            task=self.task, action_type=TaskActivityActionType.CUSTOM_FIELD
        )
        self.assertTrue(rows)
        for row in rows:
            for viewer in (self.user, self.user2):
                event = briefing.activity_event(row, str(viewer.id))
                self.assertEqual(event["summary"], "changed a custom field")
                self.assertEqual(event["_data"], {"code": "custom_field_changed", "name": None})
