"""A hard delete of a task, or of a project or demo team that holds tasks, removes the task's files (MIP-1824).

Before this, the rows of a task's attachments and body images went with the task (CASCADE) and the
files stayed on the media volume, unreachable. The files are the ones the task's own rows name, under
the task's own folder and not named by another row; they go AFTER the commit, and a file that is
already gone never fails the delete.

Files are real: a temporary MEDIA_ROOT, so "the file left storage" is checked, not assumed.
"""

from __future__ import annotations

import shutil
import tempfile

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import override_settings

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskAttachments, TaskBodyAttachmentFact, TaskMaster
from origin.services import demo_seeder
from origin.services.project_delete import delete_project
from origin.tests.test_base import BaseAPITestCase

_MEDIA_ROOT = tempfile.mkdtemp(prefix="test-task-files-hard-delete-")


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class TaskFilesBase(BaseAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )

    def make_task(self, title="t", **extra):
        return TaskMaster.objects.create(
            team=self.team, project=self.project, title=title, status="Open", **extra
        )

    def attach(self, task, name="a.txt"):
        row = TaskAttachments(task=task, attachment_id=TaskAttachments.objects.count() + 1)
        row.attached_file.save(name, ContentFile(b"bytes"), save=True)
        return row.attached_file.name

    def body_image(self, task, name="b.png"):
        row = TaskBodyAttachmentFact(task=task, uploader=self.user)
        row.body_attachment_url.save(name, ContentFile(b"img"), save=True)
        return row.body_attachment_url.name

    def delete_task_over_http(self, task):
        self.authenticate(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.delete(
                f"/api/v2/task/?team_id={self.team.team_id}&task_id={task.task_id}"
                "&is_init_task_boolean=0"
            )
        self.assertEqual(res.status_code, 204, res.content)


class TaskDeleteRemovesFiles(TaskFilesBase):
    def test_the_hard_delete_takes_the_attachment_and_body_image_files(self):
        task = self.make_task()
        names = [self.attach(task), self.body_image(task)]
        for name in names:
            self.assertTrue(default_storage.exists(name))
        self.delete_task_over_http(task)
        self.assertFalse(TaskMaster.objects.filter(pk=task.pk).exists())
        for name in names:
            self.assertFalse(default_storage.exists(name), "file left in storage")

    def test_another_tasks_files_and_a_file_a_live_row_names_stay(self):
        task = self.make_task("gone")
        live = self.make_task("live")
        live_name = self.attach(live, "live.txt")
        shared = self.attach(task, "same.txt")
        # a live row of another task names the very same stored file
        TaskAttachments.objects.filter(attached_file=live_name).update(attached_file=shared)
        self.delete_task_over_http(task)
        self.assertTrue(default_storage.exists(live_name))
        self.assertTrue(default_storage.exists(shared), "a file a live row names was deleted")

    def test_a_file_already_gone_does_not_fail_the_delete(self):
        task = self.make_task()
        name = self.attach(task)
        default_storage.delete(name)
        other = self.attach(task, "other.txt")
        self.delete_task_over_http(task)  # 204 asserted inside
        self.assertFalse(default_storage.exists(other))
        self.assertFalse(TaskAttachments.objects.filter(task_id=task.task_id).exists())


class ProjectDeleteRemovesFiles(TaskFilesBase):
    def test_a_project_delete_takes_the_files_of_all_its_tasks_and_no_others(self):
        doomed = [self.make_task(f"t{i}") for i in range(3)]
        names = []
        for task in doomed:
            names.append(self.attach(task, f"{task.task_id}.txt"))
            names.append(self.body_image(task, f"{task.task_id}.png"))
        other_project = ProjectMaster.objects.create(
            team=self.team, project_name="Q", owner=self.user, project_system_user=self.user
        )
        keeper = TaskMaster.objects.create(
            team=self.team, project=other_project, title="keep", status="Open"
        )
        kept = self.attach(keeper, "keep.txt")
        with self.captureOnCommitCallbacks(execute=True):
            delete_project(self.project)
        for name in names:
            self.assertFalse(default_storage.exists(name), "file left in storage")
        self.assertTrue(default_storage.exists(kept))


class DemoTeardownRemovesFiles(TaskFilesBase):
    def test_the_demo_teardown_takes_the_files_of_the_team_tasks(self):
        task = self.make_task()
        names = [self.attach(task), self.body_image(task)]
        with self.captureOnCommitCallbacks(execute=True):
            demo_seeder.delete_demo_team_data(self.team.team_id)
        self.assertFalse(TaskMaster.objects.filter(pk=task.pk).exists())
        for name in names:
            self.assertFalse(default_storage.exists(name), "file left in storage")
