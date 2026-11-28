"""A task's history must not name a custom field, field value or custom status of a project the
reader is not on (MIP-2532), in the app's feed and in the agent / MCP `get_task_history`.

`self.user` is on both projects; `self.user2` only on the destination.
"""

from django.core.cache import cache

from origin.middleware.current_user import clear_current_user
from origin.models.project.prj_models import ProjectCustomField, ProjectStatus
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.services.history_labels import HIDDEN_VALUE

from . import test_task_history_hidden_labels as relation_labels
from .test_base import BaseAPITestCase

SECRET = ("Client", "Zeta Corp", "Budget note", "forty grand", "Legal hold", "Counsel check")
CUSTOM = (TaskActivityActionType.CUSTOM_FIELD, TaskActivityActionType.CUSTOM_STATUS)


class HistoryHiddenCustomNameTests(BaseAPITestCase):
    _project = relation_labels.HistoryHiddenLabelTests._project
    _task = relation_labels.HistoryHiddenLabelTests._task
    _feed = relation_labels.HistoryHiddenLabelTests._feed
    _tool = relation_labels.HistoryHiddenLabelTests._tool

    def setUp(self):
        super().setUp()
        cache.clear()
        # Not Hidden: membership alone decides.
        self.old = self._project("Vault", "VLT", members=[self.user])
        self.new = self._project("Open", "OPN", members=[self.user, self.user2])
        self.tag = self._field(
            self.old, "Client", "tag", options=[{"id": "opt-z", "label": "Zeta Corp"}]
        )
        self.text = self._field(self.old, "Budget note", "text")
        self.held = self._status(self.old, "Legal hold")
        self.next = self._status(self.old, "Counsel check")
        self.task = self._task("moved", self.old)
        self._save(custom_field_values={str(self.tag.field_id): ["opt-z"]})
        self._save(
            custom_field_values={
                str(self.tag.field_id): ["opt-z"],
                str(self.text.field_id): "forty grand",
            }
        )
        # A custom status alone, then one that changes together with the category.
        self._save(custom_status=self.held)
        self._save(status="WIP", custom_status=self._status(self.old, "Counsel check 2", "WIP"))
        self._save(status="Open", custom_status=self.next)
        # The move drops the old project's status; that row is stamped with the new project.
        self._save(project=self.new)

    def tearDown(self):
        clear_current_user()
        super().tearDown()

    def _save(self, **values):
        for name, value in values.items():
            setattr(self.task, name, value)
        self.task.save()

    def _field(self, project, name, field_type, **kwargs):
        return ProjectCustomField.objects.create(
            team=self.team, project=project, field_name=name, field_type=field_type, **kwargs
        )

    def _status(self, project, name, category="Open"):
        return ProjectStatus.objects.create(
            team=self.team, project=project, name=name, category=category
        )

    def _rows(self, feed, action):
        return [r for r in feed if r["actionType"] == action]

    def test_the_setup_records_every_kind_of_row(self):
        moved_off = TaskActivity.objects.get(
            task=self.task, action_type=TaskActivityActionType.CUSTOM_STATUS, new_value__isnull=True
        )
        self.assertEqual(moved_off.project_id, self.new.project_id)
        self.assertEqual(moved_off.metadata["oldLabel"], "Counsel check")
        self.assertTrue(
            TaskActivity.objects.filter(
                task=self.task, metadata__customStatus__newLabel="Counsel check 2"
            ).exists()
        )

    def test_a_reader_off_the_old_project_sees_none_of_its_names_or_values(self):
        for read in (self._feed, self._tool):
            with self.subTest(read=read.__name__):
                body = str(read(self.user2))
                for secret in (*SECRET, "opt-z"):
                    self.assertNotIn(secret, body)

    def test_a_cut_field_row_keeps_which_side_was_set(self):
        for row in self._rows(self._feed(self.user2), TaskActivityActionType.CUSTOM_FIELD):
            self.assertIsNone(row["oldValue"])
            self.assertEqual(row["newValue"], HIDDEN_VALUE)
            self.assertEqual(row["metadata"].keys() & {"fieldName", "newLabel"}, set())

    def test_a_member_of_both_sees_every_name(self):
        feed = str(self._feed(self.user))
        for secret in SECRET:
            self.assertIn(secret, feed)
        # The tool names fields and their values; it never carried status names.
        tool = str(self._tool(self.user))
        for secret in SECRET[:4]:
            self.assertIn(secret, tool)

    def test_the_new_projects_own_fields_and_statuses_are_named(self):
        field = self._field(self.new, "Area", "text")
        self._save(custom_field_values={str(field.field_id): "Kitchen"})
        self._save(custom_status=self._status(self.new, "Triage"))
        feed = str(self._feed(self.user2))
        for name in ("Area", "Kitchen", "Triage"):
            self.assertIn(name, feed)
        self.assertIn("Kitchen", str(self._tool(self.user2)))

    def test_a_deleted_status_of_the_old_project_stays_cut(self):
        ProjectStatus.objects.filter(project=self.old).delete()
        body = str(self._feed(self.user2))
        for name in ("Legal hold", "Counsel check"):
            self.assertNotIn(name, body)
        self.assertIn("Legal hold", str(self._feed(self.user)))

    def test_a_deleted_status_of_a_task_that_never_moved_keeps_its_name(self):
        status = self._status(self.new, "Triage")
        task = self._task("stayed", self.new)
        task.custom_status = status
        task.save()
        status.delete()
        self.assertIn("Triage", str(self._feed(self.user2, task)))

    def test_the_stored_rows_are_not_changed(self):
        before = list(self._stored())
        self._feed(self.user2)
        self._tool(self.user2)
        self.assertEqual(list(self._stored()), before)

    def _stored(self):
        return (
            TaskActivity.objects.filter(task=self.task, action_type__in=CUSTOM)
            .order_by("activity_id")
            .values_list("old_value", "new_value", "metadata")
        )
