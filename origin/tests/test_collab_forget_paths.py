"""MIP-1937: every path that hard-deletes a note or a task body tells genos-collab to forget the
collaborative (Yjs) document, after the commit and never on a rollback.

Before, only force delete did: a note delete, a project delete, `purge_deleted_tasks` and the task
view's hard delete left `my-note:<id>` / `task-note:<id>` / `task-body:<id>` in collab's database
holding the text, until an orphan sweep that was never scheduled to delete anything.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import override_settings
from django.utils import timezone

from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.services import collab_push
from origin.services import deleted_task_purge as purge_svc
from origin.services.note_delete import delete_note
from origin.services.project_delete import delete_project
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


@override_settings(
    COLLAB_INTERNAL_URL="http://collab.internal", COLLAB_INTERNAL_SECRET="test-secret"
)
class CollabForgetPaths(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(mock.patch.stopall)
        self.forget = mock.patch("origin.services.collab_push.forget_document").start()
        for target in (
            "origin.search_engine.purge.purge_entities",
            "origin.search_engine.purge.purge_note",
            "origin.search_engine.purge.purge_task",
            "origin.services.project_delete._forget_search",
            "origin.services.note_delete.purge_note",
        ):
            mock.patch(target).start()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def forgotten(self):
        return sorted(c.args[0] for c in self.forget.call_args_list)

    def make_task(self, **extra):
        return TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open", **extra
        )

    def test_a_note_delete_forgets_its_document_after_the_commit_only(self):
        note = PersonalNoteMaster.objects.create(team=self.team, owner=self.user, title="n")
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            delete_note(team_id=self.team.team_id, family="personal", note_id=note.note_id)
        self.assertEqual(self.forgotten(), [], "nothing before the commit")
        for cb in callbacks:
            cb()
        self.assertEqual(self.forgotten(), [f"my-note:{note.note_id}"])

    def test_a_rolled_back_note_delete_forgets_nothing(self):
        note = PersonalNoteMaster.objects.create(team=self.team, owner=self.user, title="n")
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    delete_note(team_id=self.team.team_id, family="personal", note_id=note.note_id)
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        self.assertEqual(self.forgotten(), [])

    def test_a_project_delete_forgets_its_task_notes_and_task_bodies(self):
        task = self.make_task()
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=task, owner=self.user, title="n"
        )
        other = ProjectMaster.objects.create(
            team=self.team, project_name="O", owner=self.user, project_system_user=self.user
        )
        keep = TaskMaster.objects.create(team=self.team, project=other, title="k", status="Open")
        with self.captureOnCommitCallbacks(execute=True):
            delete_project(self.project)
        self.assertEqual(
            self.forgotten(), sorted([f"task-body:{task.task_id}", f"task-note:{note.note_id}"])
        )
        self.assertNotIn(f"task-body:{keep.task_id}", self.forgotten())

    def test_purge_deleted_tasks_forgets_the_body_and_the_notes_once(self):
        anchor = self.make_task()
        task = self.make_task()
        TaskMaster.objects.filter(pk=anchor.pk).update(project_task_number=50)
        task.status = "Deleted"
        task.save()
        when = timezone.now() - timedelta(days=60)
        TaskActivity.objects.filter(task=task, new_value="Deleted").update(ts_created_at=when)
        TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=when)
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=task, owner=self.user, title="n"
        )
        with self.captureOnCommitCallbacks(execute=True):
            purge_svc.purge_deleted_tasks(apply=True)
        self.assertFalse(TaskMaster.objects.filter(pk=task.pk).exists())
        self.assertEqual(
            self.forgotten(), sorted([f"task-body:{task.task_id}", f"task-note:{note.note_id}"])
        )

    def test_the_task_views_hard_delete_forgets_the_body_and_the_notes(self):
        task = self.make_task()
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=task, owner=self.user, title="n"
        )
        self.authenticate(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.delete(
                f"/api/v2/task/?team_id={self.team.team_id}&task_id={task.task_id}"
                "&is_init_task_boolean=0"
            )
        self.assertEqual(resp.status_code, 204, getattr(resp, "data", None))
        self.assertEqual(
            self.forgotten(), sorted([f"task-body:{task.task_id}", f"task-note:{note.note_id}"])
        )

    def test_a_huge_batch_is_capped_and_the_rest_left_to_the_sweep(self):
        names = [f"my-note:{i}" for i in range(collab_push.FORGET_AFTER_COMMIT_CAP + 25)]
        with self.captureOnCommitCallbacks(execute=True):
            collab_push.forget_documents_after_commit(names)
        self.assertEqual(self.forget.call_count, collab_push.FORGET_AFTER_COMMIT_CAP)
