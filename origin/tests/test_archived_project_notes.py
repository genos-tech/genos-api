"""MIP-1934: an archived project's notes are read-only on the SERVER, not only behind the UI.

Task notes (create, autosave, delete, move in or out, attachments, version restore) and the notes
of the project's channel answer the archive guard's 409; reads keep working, and unarchiving
brings every write back.
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.services.archive import ARCHIVED_CODE
from origin.tests import test_task_personal_note_roles_revoked as base

NOTE = "/api/v2/note/task/"


class ArchivedTaskNotes(base.TaskNoteBase):
    def setUp(self):
        super().setUp()
        self.project = self.project_of("Alpha", self.author)
        self.other = self.project_of("Beta", self.author)
        self.task = self.task_in(self.project)
        self.other_task = self.task_in(self.other)
        self.note = self.create_task_note(self.author, self.task)
        self.tid = str(self.team.team_id)
        self.authenticate(self.author)

    def archive(self, project=None):
        project = project or self.project
        project.archived_at = timezone.now()
        project.save(update_fields=["archived_at"])

    def assert_archived(self, res):
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.json()["code"], ARCHIVED_CODE)

    def create_body(self, task):
        return {
            "team_id": self.tid,
            "user_id": str(self.author.id),
            "project_id": task.project_id,
            "task_id": task.task_id,
            "title": "n",
            "body": base.BODY,
        }

    def test_create_refused_in_archived_project(self):
        self.archive()
        before = TaskNoteMaster.objects.count()
        self.assert_archived(self.client.post(NOTE, self.create_body(self.task), format="json"))
        self.assertEqual(TaskNoteMaster.objects.count(), before)

    def test_autosave_refused(self):
        self.archive()
        res = self.client.put(
            NOTE,
            {"note_id": self.note.note_id, "title": "x", "body": base.BODY},
            format="json",
        )
        self.assert_archived(res)

    def test_delete_refused(self):
        self.archive()
        res = self.client.delete(f"{NOTE}?team_id={self.tid}&note_id={self.note.note_id}")
        self.assert_archived(res)
        self.assertTrue(TaskNoteMaster.objects.filter(note_id=self.note.note_id).exists())

    def test_move_in_and_out_refused(self):
        move = {
            "team_id": self.tid,
            "user_id": str(self.author.id),
            "note_id": self.note.note_id,
            "task_id": self.other_task.task_id,
        }
        self.archive()
        self.assert_archived(self.client.put(NOTE + "move/", move, format="json"))
        # Into an archived project from a live one.
        self.archive(self.other)
        self.project.archived_at = None
        self.project.save(update_fields=["archived_at"])
        self.assert_archived(self.client.put(NOTE + "move/", move, format="json"))

    def test_attachment_refused(self):
        self.archive()
        res = self.client.post(
            NOTE + "attachment/",
            {
                "note_id": self.note.note_id,
                "uploader": str(self.author.id),
                "note_attachment_file": SimpleUploadedFile("a.txt", b"x"),
            },
            format="multipart",
        )
        self.assert_archived(res)

    def test_reads_still_work(self):
        self.archive()
        res = self.client.get(NOTE + "single/", {"team_id": self.tid, "note_id": self.note.note_id})
        self.assertEqual(res.status_code, 200, res.content)

    def test_unarchive_restores_writes(self):
        self.archive()
        self.project.archived_at = None
        self.project.save(update_fields=["archived_at"])
        res = self.client.put(
            NOTE,
            {"note_id": self.note.note_id, "title": "x", "body": base.BODY},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)

    def test_live_project_unaffected(self):
        self.archive(self.other)
        res = self.client.post(NOTE, self.create_body(self.task), format="json")
        self.assertEqual(res.status_code, 201, res.content)


class ArchivedProjectChatNotes(base.Base):
    def setUp(self):
        super().setUp()
        self.project = self.project_of("Alpha", self.author)
        # The project's own channel is made with the project (one per project).
        self.channel = Channel.objects.get(project=self.project, kind=ChannelKind.PM)
        for u in (self.owner, self.author):
            ChannelMember.objects.get_or_create(channel=self.channel, user=u)
        self.authenticate(self.author)

    def test_chat_note_create_refused(self):
        self.project.archived_at = timezone.now()
        self.project.save(update_fields=["archived_at"])
        before = ChatNoteMaster.objects.count()
        res = self.client.post(
            "/api/v2/note/chat/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.author.id),
                "chat_id": str(self.channel.id),
                "title": "n",
                "body": base.BODY,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(ChatNoteMaster.objects.count(), before)
