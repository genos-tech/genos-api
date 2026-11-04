"""`PUT /task/` does not take the server's own columns from the body (MIP-1018).

The whole body reached a partial `fields = "__all__"` serializer, so a save
could also set `is_deleted` (a soft delete that skipped the delete endpoint's
checks and cleanup) or `project_task_number`, and turn a real task into a
create-form scaffold (`is_init_task`), which every list hides.

The app's full save, the create form's finalize and the diagram's partial
saves send none of these except `is_init_task: false` on finalize; those
bodies are the contract. A task's own value echoed back is accepted and
unused; any other is a 400 and nothing is written.
"""

from django.contrib.auth import get_user_model

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

TASK = "/api/v2/task/"


class TaskPutForgedFieldTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            owner=self.user,
            project_system_user=self.user,
            code="PP",
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Original",
            status="Open",
            reporter=self.user,
        )
        self.task.refresh_from_db()  # the signal numbers it
        self.authenticate(self.user)

    def _put(self, **extra):
        return self.client.put(TASK, {"task_id": self.task.task_id, **extra}, format="json")

    def _row(self):
        row = TaskMaster.objects.get(task_id=self.task.task_id)
        return (
            row.title,
            row.is_deleted,
            row.is_milestone,
            row.is_init_task,
            row.project_task_number,
            row.mentioned_user_ids,
            row.linked_calendar_event_id,
            row.linked_calendar_id,
        )

    def test_a_forged_server_field_is_refused_and_nothing_is_written(self):
        forged = {
            "is_deleted": True,
            "is_milestone": True,
            "project_task_number": 999,
            "mentioned_user_ids": [str(self.user2.id)],
            "linked_calendar_event_id": "evt-forged",
            "linked_calendar_id": "cal-forged",
            "is_init_task": True,
        }
        before = self._row()
        for field, value in forged.items():
            with self.subTest(field=field):
                res = self._put(title="changed", **{field: value})
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data, {"error": f"{field} cannot be set."})
                self.assertEqual(self._row(), before)

    def test_the_apps_full_save_body_still_works(self):
        """`sendUpdatedSpecificTask`'s keys."""
        res = self._put(
            team=str(self.team.team_id),
            project=self.project.project_id,
            thread_id=None,
            parent_task_id=None,
            assignee=str(self.user2.id),
            reporter=str(self.user.id),
            title="Renamed",
            priority="High",
            effort_level="M",
            status="In Progress",
            content=[],
            due_date="2026-12-01",
            start_date=None,
            links=[],
            tags=[],
            milestone=None,
        )
        self.assertEqual(res.status_code, 200, res.data)
        task = TaskMaster.objects.get(task_id=self.task.task_id)
        self.assertEqual(
            (task.title, task.status, task.priority), ("Renamed", "In Progress", "High")
        )
        self.assertEqual(task.assignee_id, self.user2.id)
        self.assertFalse(task.is_deleted)
        self.assertEqual(task.project_task_number, self.task.project_task_number)

    def test_the_diagrams_partial_body_still_works(self):
        res = self._put(title="Diagram rename", start_date="2026-12-02")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(TaskMaster.objects.get(task_id=self.task.task_id).title, "Diagram rename")

    def test_the_create_forms_finalize_still_clears_the_scaffold_flag(self):
        TaskMaster.objects.filter(task_id=self.task.task_id).update(is_init_task=True)
        res = self._put(title="Finalized", is_init_task=False)
        self.assertEqual(res.status_code, 200, res.data)
        task = TaskMaster.objects.get(task_id=self.task.task_id)
        self.assertEqual((task.title, task.is_init_task), ("Finalized", False))

    def test_the_tasks_own_values_echoed_back_are_accepted(self):
        res = self._put(
            title="Echo",
            is_deleted=False,
            is_milestone=False,
            is_init_task=False,
            project_task_number=self.task.project_task_number,
            linked_calendar_event_id=None,
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(TaskMaster.objects.get(task_id=self.task.task_id).title, "Echo")

    def test_the_mention_list_is_still_derived_from_the_content(self):
        res = self._put(
            title="Mentions",
            content=[
                {
                    "type": "paragraph",
                    "content": [{"type": "mention", "props": {"userId": str(self.user2.id)}}],
                }
            ],
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("newly_mentioned_user_ids", res.data)
