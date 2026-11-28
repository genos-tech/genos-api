"""MIP-1720: `purge_detached_note_attachments` removes the uploaded files of notes that are
already gone (`note IS NULL`), and nothing else.

The rows to remove are made the way the old delete made them: the note row is deleted by hand
(`.delete()`, which does not go through `forget_note_attachments`) and its `SET_NULL` FK leaves
the attachment detached with its file in storage.
"""

from __future__ import annotations

import tempfile
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteAttachmentFact, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.services import detached_note_attachments as svc
from origin.tests.media_support import remove_media_root
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_OWNER

User = get_user_model()
BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}]
_MEDIA_ROOT = tempfile.mkdtemp(prefix="test-detached-attachments-")


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class DetachedBase(BaseAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        remove_media_root(_MEDIA_ROOT)

    def setUp(self):
        super().setUp()
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        self.project = project
        self.task = TaskMaster.objects.create(
            team=self.team, project=project, title="t", status="Open"
        )

    # -- builders -----------------------------------------------------------------------------

    def make_note(self, family):
        if family == "personal":
            return PersonalNoteMaster.objects.create(
                team=self.team, owner=self.user, title="p", body=BODY
            )
        if family == "task":
            return TaskNoteMaster.objects.create(
                team=self.team,
                owner=self.user,
                project=self.project,
                task=self.task,
                title="t",
                body=BODY,
            )
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM)
        return ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=int(ChannelKind.GM),
            channel=channel,
            is_thread=False,
            title="c",
            body=BODY,
        )

    def attach(self, family, note, name="file.txt"):
        model = svc.FAMILIES[family][0]
        extra = {"chat_type": 3, "is_thread": False} if family == "chat" else {}
        row = model(note=note, uploader=self.user, **extra)
        row.note_attachment_url.save(name, ContentFile(b"bytes"), save=True)
        return row

    def detached(self, family, name="file.txt"):
        """What the old delete left: the note deleted by hand, its file and row still there."""
        note = self.make_note(family)
        row = self.attach(family, note, name)
        note.delete()
        row.refresh_from_db()
        self.assertIsNone(row.note_id)
        self.assertTrue(default_storage.exists(row.note_attachment_url.name))
        return row

    def rows(self, family):
        return svc.FAMILIES[family][0]._base_manager


class WhatIsRemoved(DetachedBase):
    def test_every_family_row_and_file(self):
        rows = {f: self.detached(f) for f in svc.FAMILIES}
        names = {f: r.note_attachment_url.name for f, r in rows.items()}
        report = svc.purge_detached_note_attachments(apply=True)
        self.assertEqual(report.removable, {"personal": 1, "task": 1, "chat": 1})
        self.assertEqual(report.files_removed, 3)
        for family, row in rows.items():
            self.assertFalse(self.rows(family).filter(pk=row.pk).exists(), family)
            self.assertFalse(default_storage.exists(names[family]), family)

    def test_dry_run_counts_and_writes_nothing(self):
        row = self.detached("personal")
        report = svc.purge_detached_note_attachments(apply=False)
        self.assertEqual(report.removable["personal"], 1)
        self.assertEqual(report.files_removed, 0)
        self.assertTrue(self.rows("personal").filter(pk=row.pk).exists())
        self.assertTrue(default_storage.exists(row.note_attachment_url.name))

    def test_the_second_run_finds_nothing(self):
        self.detached("task")
        svc.purge_detached_note_attachments(apply=True)
        again = svc.purge_detached_note_attachments(apply=True)
        self.assertEqual((again.total, again.files_removed), (0, 0))

    def test_an_attachment_of_a_live_note_is_not_touched(self):
        note = self.make_note("personal")
        live = self.attach("personal", note)
        self.detached("personal")
        svc.purge_detached_note_attachments(apply=True)
        self.assertTrue(self.rows("personal").filter(pk=live.pk).exists())
        self.assertTrue(default_storage.exists(live.note_attachment_url.name))


class WhatIsKept(DetachedBase):
    def kept(self, row, **counts):
        report = svc.purge_detached_note_attachments(apply=True)
        self.assertEqual(report.total, 0, report)
        for field, n in counts.items():
            self.assertEqual(getattr(report, field), n, field)
        self.assertTrue(self.rows("personal").filter(pk=row.pk).exists())
        self.assertTrue(default_storage.exists(row.note_attachment_url.name))

    def test_a_path_that_is_not_the_models_own_shape_is_never_deleted(self):
        row = self.detached("personal")
        for odd in (
            "elsewhere/1/file.txt",  # not under notes/
            "notes/personal/None/file.txt",  # no note id
            "notes/personal/12/sub/file.txt",  # a directory too deep
            "notes/task/12/file.txt",  # another family's directory on a personal row
            "../notes/personal/12/file.txt",
            "",
        ):
            with self.subTest(path=odd):
                self.rows("personal").filter(pk=row.pk).update(note_attachment_url=odd)
                report = svc.purge_detached_note_attachments(apply=True)
                self.assertEqual(report.total, 0)
                self.assertEqual(report.kept_odd_path, 1)
                self.assertTrue(self.rows("personal").filter(pk=row.pk).exists())

    def test_a_row_whose_note_exists_is_kept_even_with_note_null(self):
        """Cannot happen with the FK in place; the check is for the one mistake that cannot be
        undone."""
        row = self.detached("personal")
        # a note that is alive and has the number the file's path names
        PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="alive",
            body=BODY,
            note_id=int(row.note_attachment_url.name.split("/")[2]),
        )
        self.kept(row, kept_note_alive=1)

    def test_the_same_number_in_another_family_does_not_keep_the_file(self):
        """The three tables number notes separately: a live TASK note 7 does not make personal
        note 7's file live."""
        row = self.detached("personal")
        number = int(row.note_attachment_url.name.split("/")[2])
        TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=self.project,
            task=self.task,
            title="other family",
            body=BODY,
            note_id=number,
        )
        report = svc.purge_detached_note_attachments(apply=True)
        self.assertEqual(report.removable["personal"], 1)

    def test_a_file_a_live_row_also_names_is_kept(self):
        row = self.detached("personal")
        live_note = self.make_note("personal")
        shared = PersonalNoteAttachmentFact(note=live_note, uploader=self.user)
        shared.note_attachment_url.name = row.note_attachment_url.name
        shared.save()
        self.kept(row, kept_file_shared=1)

    def test_a_row_whose_file_cannot_be_deleted_is_kept_for_the_next_run(self):
        row = self.detached("personal")
        with mock.patch.object(default_storage, "delete", side_effect=OSError("down")):
            with self.assertLogs(svc.logger, "ERROR") as logs:
                report = svc.purge_detached_note_attachments(apply=True)
        self.assertEqual((report.file_errors, report.total), (1, 0))
        self.assertTrue(self.rows("personal").filter(pk=row.pk).exists())
        self.assertNotIn("file.txt", logs.output[0].split("Traceback")[0])
        again = svc.purge_detached_note_attachments(apply=True)
        self.assertEqual(again.files_removed, 1)
        self.assertFalse(self.rows("personal").filter(pk=row.pk).exists())

    def test_a_row_attached_since_the_read_is_not_deleted(self):
        row = self.detached("personal")
        note = self.make_note("personal")
        original = default_storage.delete

        def reattach(name):
            original(name)
            self.rows("personal").filter(pk=row.pk).update(note=note)

        with mock.patch.object(default_storage, "delete", side_effect=reattach):
            svc.purge_detached_note_attachments(apply=True)
        self.assertTrue(self.rows("personal").filter(pk=row.pk).exists())


class WhyDetachedMeansUnreachable(DetachedBase):
    """The premise the command rests on: nothing can reach a detached row's file."""

    def test_the_media_route_refuses_the_files_of_a_deleted_note_to_everyone(self):
        note = self.make_note("personal")
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_OWNER,
        )
        row = self.attach("personal", note)
        url = f"/media/{row.note_attachment_url.name}"
        self.client.cookies["refresh"] = str(RefreshToken.for_user(self.user))
        self.assertEqual(self.client.get(url).status_code, 200, "the owner reads it while it lives")
        # the old delete: the note and its permission rows go, the file stays
        note.delete()
        NotePermissionMaster.objects.filter(
            note_type=NOTE_TYPE_PERSONAL, note_id=note.note_id
        ).delete()
        self.assertTrue(default_storage.exists(row.note_attachment_url.name))
        for user in (self.user, self.user2, self.team.owner):
            self.client.cookies["refresh"] = str(RefreshToken.for_user(user))
            self.assertEqual(self.client.get(url).status_code, 404, user)

    def test_no_url_lists_or_reads_an_attachment(self):
        """The three attachment routes accept POST only, and refuse a request with no note, so a
        row never starts out detached."""
        routes = {
            "personal": "/api/v2/note/personal/attachment/",
            "task": "/api/v2/note/task/attachment/",
            "chat": "/api/v2/note/chat/attachment/",
        }
        self.authenticate(self.user)
        for family, path in routes.items():
            with self.subTest(family=family):
                self.assertEqual(self.client.get(path).status_code, 405)
                res = self.client.post(path, {"uploader": str(self.user.id)}, format="multipart")
                self.assertEqual(res.status_code, 400)
                self.assertFalse(self.rows(family).exists())


class TheCommand(DetachedBase):
    def run_command(self, *args):
        out = StringIO()
        call_command("purge_detached_note_attachments", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_is_the_default_and_prints_counts_only(self):
        row = self.detached("personal", name="secret-plan.txt")
        out = self.run_command()
        self.assertIn("1 personal note attachment(s) would be deleted", out)
        self.assertIn("re-run with --apply --yes", out)
        self.assertNotIn("secret-plan", out)
        self.assertTrue(self.rows("personal").filter(pk=row.pk).exists())

    def test_apply_needs_yes(self):
        row = self.detached("personal")
        with self.assertRaises(CommandError):
            self.run_command("--apply")
        self.assertTrue(self.rows("personal").filter(pk=row.pk).exists())

    def test_apply_with_yes_deletes_and_reports(self):
        row = self.detached("task")
        out = self.run_command("--apply", "--yes")
        self.assertIn("1 task note attachment(s) deleted", out)
        self.assertIn("1 file(s) removed", out)
        self.assertFalse(self.rows("task").filter(pk=row.pk).exists())
        self.assertIn("0 task note attachment(s) deleted", self.run_command("--apply", "--yes"))
