"""Tests for the task-activity signal's handling of the CreateTaskForm
"finalize" save (init/draft row -> real task).

CreateTaskForm first creates an empty `is_init_task=True` row, then the
submit PUT writes every field at once and flips `is_init_task` to False.
Without special handling the post_save signal would emit one
"changed X from None" row per field — creation noise the user never
performed *after* the task existed. The signal collapses that whole
transition into a single CREATED row and suppresses the follow-up saves
the same PUT makes (due-date clear, milestone/parent bridge).

See `origin/signals/task_signals.py::task_record_changes`.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster

User = get_user_model()


class TaskFinalizeActivityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="activityuser", email="activity@test.com", password="pass12345"
        )
        self.team = TeamMaster.objects.create(
            team_name="Activity Team", team_email="actteam@test.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Activity Project",
            owner=self.user,
            project_system_user=self.user,
        )

    def _init_task(self) -> TaskMaster:
        """Mirror `createEmptyTask`: a draft row that the form edits in
        place before the user submits."""
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="",
            status="Open",
            is_init_task=True,
        )

    def _actions(self, task) -> list[str]:
        return list(
            TaskActivity.objects.filter(task=task)
            .order_by("ts_created_at")
            .values_list("action_type", flat=True)
        )

    def test_init_insert_records_nothing(self):
        """The draft insert is skipped — no audit trail until submit."""
        task = self._init_task()
        self.assertEqual(self._actions(task), [])

    def test_finalize_records_single_created_no_field_diffs(self):
        """Finalizing (fields written + is_init_task->False in one save)
        yields exactly one CREATED row, not a diff per field."""
        task = self._init_task()

        task.title = "Real task"
        task.status = "WIP"
        task.priority = "High"
        task.effort_level = "Low"
        task.assignee = self.user
        task.due_date = "2026-12-31"
        task.is_init_task = False
        task.save()

        self.assertEqual(self._actions(task), [TaskActivityActionType.CREATED])

    def test_finalize_followup_bridge_save_is_suppressed(self):
        """The submit PUT runs extra saves after the finalize (milestone
        / parent bridge, due-date clear) with is_init_task already False.
        Those must not leak parent_changed / milestone_changed rows."""
        task = self._init_task()

        # First save: the finalize transition.
        task.title = "Sub-task"
        task.status = "Open"
        task.is_init_task = False
        task.save()

        # Second save on the SAME instance: the bridge writing
        # parent_task_id / root_task_id (as `_bridge_milestone_to_parent`
        # does for a task created inside a milestone / under a parent).
        task.parent_task_id = 999
        task.root_task_id = 999
        task.save(update_fields=["parent_task_id", "root_task_id", "ts_updated_at"])

        self.assertEqual(self._actions(task), [TaskActivityActionType.CREATED])

    def test_edit_after_creation_still_records(self):
        """Guard against over-suppression: a genuine edit on a freshly
        loaded instance (new request) still logs a field change."""
        task = self._init_task()
        task.title = "Real task"
        task.status = "Open"
        task.is_init_task = False
        task.save()

        # A later edit comes in on a fresh instance (no in-memory
        # finalize flag), exactly like a separate PUT request.
        reloaded = TaskMaster.objects.get(pk=task.pk)
        reloaded.status = "Closed"
        reloaded.save()

        self.assertEqual(
            self._actions(task),
            [TaskActivityActionType.CREATED, TaskActivityActionType.STATUS],
        )
