"""`purge_orphaned_task_files`: the repair for task files already left in storage (MIP-1824).

Files are real (a temporary MEDIA_ROOT) and their age is set on disk, so the grace period is
exercised, not mocked. Dry run is the default and writes nothing; `--apply` needs `--yes`; a file
named by any live row, a file younger than the grace period, a file at an unexpected path and a
file whose row appeared after the scan are never deleted; output is counts only, never a name.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from io import StringIO
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import (
    TaskAttachments,
    TaskBodyAttachmentFact,
    TaskComments,
    TaskMaster,
)
from origin.services import orphaned_task_files as svc
from origin.tests.media_support import remove_media_root
from origin.tests.test_base import BaseAPITestCase

_MEDIA_ROOT = tempfile.mkdtemp(prefix="test-orphaned-task-files-")
OLD = 10 * 24 * 3600  # ten days: past the seven-day grace period


def _call(*args) -> str:
    out = StringIO()
    call_command("purge_orphaned_task_files", *args, stdout=out)
    return out.getvalue()


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class OrphanedTaskFiles(BaseAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        remove_media_root(_MEDIA_ROOT)

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        self.live = TaskMaster.objects.create(
            team=self.team, project=self.project, title="live", status="Open"
        )

    def put(self, name, *, age=OLD):
        """A file on disk under `name`, aged `age` seconds (0 = just written)."""
        stored = default_storage.save(name, ContentFile(b"secret bytes"))
        path = os.path.join(_MEDIA_ROOT, stored)
        when = time.time() - age
        os.utime(path, (when, when))
        self.addCleanup(lambda: default_storage.exists(stored) and default_storage.delete(stored))
        return stored

    def row_for(self, name):
        TaskAttachments.objects.create(
            task=self.live,
            attachment_id=TaskAttachments.objects.count() + 1,
            attached_file=name,
        )

    def exists(self, name):
        return default_storage.exists(name)

    def run_apply(self, *extra):
        return _call("--apply", "--yes", *extra)


class DryRunAndSafety(OrphanedTaskFiles):
    def test_a_dry_run_counts_and_writes_nothing(self):
        orphan = self.put("task_attachments/999999/gone.txt")
        unref = self.put(f"tasks/{self.live.task_id}/unref.png")
        rows_before = TaskAttachments.objects.count()
        out = _call()
        self.assertTrue(self.exists(orphan))
        self.assertTrue(self.exists(unref))
        self.assertEqual(TaskAttachments.objects.count(), rows_before)
        self.assertIn("DRY RUN", out)
        self.assertIn("2 orphaned file(s)", out)

    def test_apply_needs_yes(self):
        self.put("task_attachments/999999/gone.txt")
        with self.assertRaises(CommandError):
            _call("--apply")

    def test_output_never_names_a_file(self):
        name = self.put("task_attachments/999999/secret-name-xyz.txt")
        out = self.run_apply()
        self.assertNotIn("secret-name-xyz", out)
        self.assertNotIn("task_attachments", out)
        self.assertFalse(self.exists(name))


class WhatIsRemoved(OrphanedTaskFiles):
    def test_old_orphans_of_a_gone_task_go_on_apply(self):
        gone = self.put("task_attachments/999999/gone.txt")
        self.run_apply()
        self.assertFalse(self.exists(gone))

    def test_an_unnamed_file_of_a_live_task_is_kept_by_default(self):
        """MIP-1890: its body may still show it; only an explicit flag deletes it."""
        unref = self.put(f"tasks/{self.live.task_id}/unref.png")
        report = svc.scan(apply=True)
        self.assertTrue(self.exists(unref))
        self.assertEqual(report.removed, 0)
        self.assertEqual(report.orphan_task_live, 1)

    def test_a_live_task_file_goes_with_the_flag(self):
        unref = self.put(f"tasks/{self.live.task_id}/unref.png")
        self.run_apply("--include-live-tasks")
        self.assertFalse(self.exists(unref))

    def test_a_file_the_live_task_body_names_is_kept_even_with_the_flag(self):
        name = self.put(f"tasks/{self.live.task_id}/x.png")
        self.live.content = [{"type": "image", "props": {"url": f"/media/{name}"}}]
        self.live.save(update_fields=["content"])
        self.run_apply("--include-live-tasks")
        self.assertTrue(self.exists(name))

    def test_a_file_a_live_task_comment_names_is_kept_even_with_the_flag(self):
        name = self.put(f"tasks/{self.live.task_id}/c.png")
        TaskComments.objects.create(
            task=self.live, sender=self.user, comment_id=1, comment_body=[{"text": name}]
        )
        self.run_apply("--include-live-tasks")
        self.assertTrue(self.exists(name))

    def test_a_file_a_live_row_names_is_never_touched(self):
        name = self.put(f"task_attachments/{self.live.task_id}/kept.txt")
        self.row_for(name)
        self.run_apply()
        self.assertTrue(self.exists(name))
        self.assertTrue(TaskAttachments.objects.filter(attached_file=name).exists())

    def test_a_body_image_a_live_row_names_is_never_touched(self):
        name = self.put(f"tasks/{self.live.task_id}/body.png")
        TaskBodyAttachmentFact.objects.create(
            task=self.live, uploader=self.user, body_attachment_url=name
        )
        self.run_apply()
        self.assertTrue(self.exists(name))

    def test_a_file_younger_than_the_grace_period_is_never_matched(self):
        young = self.put("task_attachments/999999/upload-in-flight.txt", age=0)
        just_under = self.put(
            "task_attachments/999999/almost.txt", age=svc.GRACE.total_seconds() - 3600
        )
        report = svc.scan(apply=True)
        self.assertTrue(self.exists(young))
        self.assertTrue(self.exists(just_under))
        self.assertEqual(report.kept[svc.KEPT_YOUNG], 2)
        self.assertEqual(report.removed, 0)

    def test_a_file_at_an_unexpected_path_is_never_touched(self):
        odd = self.put("task_attachments/not-a-number/x.txt")
        other = self.put("elsewhere/tasks/999999/x.txt")
        self.run_apply()
        self.assertTrue(self.exists(odd))
        self.assertTrue(self.exists(other))

    def test_a_row_that_appears_after_the_scan_protects_its_file(self):
        name = self.put(f"task_attachments/{self.live.task_id}/late.txt")
        # The scan's snapshot says nobody names the file; a row has been written since.
        self.row_for(name)
        with mock.patch.object(svc, "_referenced_names", return_value=set()):
            self.run_apply()
        self.assertTrue(self.exists(name))

    def test_the_limit_caps_one_run(self):
        names = [self.put(f"task_attachments/999999/f{i}.txt") for i in range(3)]
        out = self.run_apply("--limit", "2")
        self.assertEqual(sum(self.exists(n) for n in names), 1)
        self.assertIn("1 more", out)

    def test_a_second_run_finds_nothing_more(self):
        self.put("task_attachments/999999/once.txt")
        self.run_apply()
        out = self.run_apply()
        self.assertIn("0 orphaned file(s)", out)

    def test_a_missing_storage_root_is_not_an_error(self):
        shutil.rmtree(os.path.join(_MEDIA_ROOT, "tasks"), ignore_errors=True)
        report = svc.scan(apply=False)
        self.assertEqual(report.orphan, 0)
