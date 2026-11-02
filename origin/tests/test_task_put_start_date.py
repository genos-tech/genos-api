"""`PUT /api/v2/task/` can clear a start date (MIP-912).

The handler strips every `None` from the payload before the serializer
sees it (the PUT is partial, so a subset like the diagram's
`{task_id, start_date}` works). Only `due_date` had a way around that:
the explicit-clear branch. So `start_date: null` was silently dropped and
the app's "Clear" button never persisted.

Contract pinned here, for `start_date` exactly as for `due_date`:
  * key absent                -> the stored date is left alone;
  * a date                    -> it is set;
  * `null` or `""`            -> it is cleared, and the clear writes the
                                 same `start_date_changed` history row a
                                 normal change does;
  * clearing a date that is already null changes nothing and writes
    nothing.

The frontend must not send `start_date: null` for "this object has no
start date loaded" (it did, on every full save, until MIP-912's frontend
PR); that is why the two PRs merge in a fixed order.
"""

from contextlib import contextmanager
from datetime import date
from unittest import mock

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster

from .test_base import BaseAPITestCase

TASK = "/api/v2/task/"


class TaskPutStartDateTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Schedule",
            code="SCH",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Plan the launch",
            status="Open",
            # Real dates, not strings: a string on the in-memory instance
            # differs from the date the next save reads back, and the
            # history diff would record a phantom change.
            start_date=date(2026, 11, 2),
            due_date=date(2026, 12, 31),
        )
        self.authenticate()

    def _put(self, **fields):
        res = self.client.put(TASK, {"task_id": self.task.task_id, **fields}, format="json")
        self.task.refresh_from_db()
        return res

    def _full_save(self, **overrides):
        """The shape the app's full save sends, minus `start_date`."""
        payload = {
            "task_id": self.task.task_id,
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": self.task.title,
            "priority": "Medium",
            "effort_level": "Medium",
            "status": "WIP",
            "content": None,
            "due_date": "2026-12-31",
            "links": [],
            "tags": [],
            "milestone": None,
        }
        payload.update(overrides)
        res = self.client.put(TASK, payload, format="json")
        self.task.refresh_from_db()
        return res

    def _forget_start_date(self):
        """Null the column without the history row a save would write."""
        TaskMaster.objects.filter(pk=self.task.pk).update(start_date=None)

    @contextmanager
    def _recorded_saves(self):
        """The `update_fields` of every `TaskMaster.save` made inside."""
        real_save = TaskMaster.save
        saves = []

        def recording_save(instance, *args, **kwargs):
            saves.append(kwargs.get("update_fields"))
            return real_save(instance, *args, **kwargs)

        with mock.patch.object(TaskMaster, "save", recording_save):
            yield saves

    def _rows(self, action=TaskActivityActionType.START_DATE):
        return list(
            TaskActivity.objects.filter(task=self.task, action_type=action).order_by("activity_id")
        )

    # ── the clear ────────────────────────────────────────────────

    def test_null_clears_the_start_date(self):
        res = self._put(start_date=None)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self.task.start_date)

    def test_an_empty_string_clears_the_start_date(self):
        """Some legacy callers ship `""` for "no date" (the due-date branch
        says so too). It must clear, not fail the date parser."""
        res = self._put(start_date="")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self.task.start_date)

    def test_the_clear_is_reflected_in_the_response(self):
        res = self._put(start_date=None)
        self.assertIsNone(res.data["task"]["start_date"])

    def test_a_full_save_with_a_null_start_date_clears_it(self):
        res = self._full_save(start_date=None)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self.task.start_date)
        # ...and the rest of the save still landed.
        self.assertEqual(self.task.status, "WIP")

    # ── what must NOT clear it ───────────────────────────────────

    def test_an_absent_key_leaves_the_start_date_alone(self):
        res = self._put(title="Renamed")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(str(self.task.start_date), "2026-11-02")
        self.assertEqual(self.task.title, "Renamed")
        self.assertEqual(self._rows(), [])

    def test_a_full_save_without_the_key_leaves_it_alone(self):
        """What the frontend sends once it omits `start_date` for an object
        that does not carry one."""
        res = self._full_save()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(str(self.task.start_date), "2026-11-02")
        self.assertEqual(self._rows(), [])

    def test_a_real_date_still_sets(self):
        res = self._put(start_date="2026-11-16")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(str(self.task.start_date), "2026-11-16")

    def test_a_malformed_date_is_still_refused(self):
        res = self._put(start_date="not-a-date")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(str(self.task.start_date), "2026-11-02")

    # ── history ──────────────────────────────────────────────────

    def test_clearing_writes_the_same_history_row_a_change_does(self):
        self._put(start_date="2026-11-16")
        changed = self._rows()[-1]

        self._put(start_date=None)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        cleared = rows[-1]

        self.assertEqual(cleared.action_type, TaskActivityActionType.START_DATE)
        self.assertEqual(cleared.field_name, "start_date")
        self.assertEqual(cleared.old_value, "2026-11-16")
        self.assertIsNone(cleared.new_value)
        self.assertEqual(str(cleared.actor_id), str(self.user.id))
        self.assertEqual(cleared.metadata.get("via"), {"kind": "app"})
        # Same shape as the row an ordinary change wrote.
        self.assertEqual(cleared.field_name, changed.field_name)
        self.assertEqual(cleared.metadata.get("via"), changed.metadata.get("via"))
        self.assertEqual(cleared.task_id, changed.task_id)

    def test_an_empty_string_clear_writes_the_row_too(self):
        self._put(start_date="")
        (row,) = self._rows()
        self.assertEqual(row.old_value, "2026-11-02")
        self.assertIsNone(row.new_value)

    def test_clearing_a_date_that_is_already_null_writes_nothing(self):
        self._forget_start_date()
        before = TaskActivity.objects.filter(task=self.task).count()

        with self._recorded_saves() as saves:
            res = self._put(start_date=None)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self.task.start_date)
        self.assertEqual(self._rows(), [])
        self.assertEqual(TaskActivity.objects.filter(task=self.task).count(), before)
        # Nothing to clear, so no second save to write it.
        self.assertEqual([f for f in saves if f and "start_date" in f], [])

    def test_a_full_save_of_a_task_with_no_start_date_writes_nothing(self):
        """The create form's finalize PUT, and any save of a task that never
        had one, sends `start_date: null`."""
        self._forget_start_date()

        res = self._full_save(start_date=None)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._rows(), [])

    # ── next to the due-date clear ───────────────────────────────

    def test_clearing_the_due_date_with_a_start_date_in_the_payload(self):
        res = self._put(due_date=None, start_date="2026-11-09")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self.task.due_date)
        self.assertEqual(str(self.task.start_date), "2026-11-09")

    def test_clearing_the_due_date_leaves_an_absent_start_date_alone(self):
        res = self._put(due_date=None)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self.task.due_date)
        self.assertEqual(str(self.task.start_date), "2026-11-02")
        self.assertEqual(self._rows(), [])

    def test_clearing_the_start_date_leaves_the_due_date_alone(self):
        res = self._put(start_date=None)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(str(self.task.due_date), "2026-12-31")
        self.assertEqual(self._rows(TaskActivityActionType.DUE_DATE), [])

    def test_clearing_both_in_one_request_clears_both_and_records_both(self):
        with self._recorded_saves() as saves:
            res = self._put(due_date=None, start_date=None)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIsNone(self.task.due_date)
        self.assertIsNone(self.task.start_date)
        # One write for both columns, not one per column.
        clearing = [f for f in saves if f and ("start_date" in f or "due_date" in f)]
        self.assertEqual(len(clearing), 1)
        self.assertEqual(set(clearing[0]), {"due_date", "start_date", "ts_updated_at"})
        (start_row,) = self._rows()
        (due_row,) = self._rows(TaskActivityActionType.DUE_DATE)
        self.assertEqual(start_row.old_value, "2026-11-02")
        self.assertEqual(due_row.old_value, "2026-12-31")
