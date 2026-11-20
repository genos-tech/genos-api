"""MIP-1720: a note's uploaded files go with the note, on EVERY path that deletes it.

An attachment row points at its note with a `SET_NULL` foreign key, so a note delete used to leave
a row with `note = NULL` and the uploaded file in storage, with nothing left that could find
either. `note_delete.forget_note_attachments` takes the rows in the same transaction as the note
and the files after the commit. One test per delete path, for the three families where the path
reaches them, each with a SURVIVOR (another note's file) so "deleted everything" cannot pass.
"""

from __future__ import annotations

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.test import override_settings

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.note.chat_note_models import ChatNoteAttachmentFact, ChatNoteMaster
from origin.models.note.personal_note_models import (
    PersonalNoteAttachmentFact,
    PersonalNoteFolder,
    PersonalNoteMaster,
)
from origin.models.note.task_note_models import TaskNoteAttachmentFact, TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import note_delete
from origin.services.account_deletion import delete_account
from origin.services.demo_seeder import create_demo_environment, delete_demo_team_data
from origin.services.project_delete import delete_project
from origin.tests.test_base import BaseAPITestCase
from origin.tests.transaction_support import CheapFlushTransactionTestCase

User = get_user_model()
BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}]
_MEDIA_ROOT = tempfile.mkdtemp(prefix="test-note-attachments-")


def attach(model, note, uploader, **extra):
    """An uploaded file on `note`, through the model's own `upload_to`, as the route stores it."""
    row = model(note=note, uploader=uploader, **extra)
    row.note_attachment_url.save("file.txt", ContentFile(b"bytes"), save=True)
    return row


def attach_chat(note, uploader, channel=None):
    return attach(ChatNoteAttachmentFact, note, uploader, chat_type=3, is_thread=False)


class Files:
    """Assertions on a row and the stored file it names."""

    def assertPresent(self, row):
        model = type(row)
        self.assertTrue(model.objects.filter(pk=row.pk).exists(), "the row is gone")
        self.assertTrue(default_storage.exists(row.note_attachment_url.name), "the file is gone")

    def assertRemoved(self, row, name):
        model = type(row)
        self.assertFalse(model.objects.filter(pk=row.pk).exists(), "the row survived")
        self.assertFalse(default_storage.exists(name), "the file survived")


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class NoteAttachmentsBase(Files, BaseAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def personal(self, title="p", owner=None, folder=None):
        return PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.user,
            title=title,
            body=BODY,
            folder_id=folder.folder_id if folder else None,
        )

    def project_task_note(self, name="P"):
        project = ProjectMaster.objects.create(
            team=self.team, project_name=name, owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(team=self.team, project=project, title="t", status="Open")
        note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=project, task=task, title="n", body=BODY
        )
        return project, task, note

    def chat_note(self, title="c"):
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM)
        return ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=int(ChannelKind.GM),
            channel=channel,
            is_thread=False,
            title=title,
            body=BODY,
        )


class PageAndAgentDeletes(NoteAttachmentsBase):
    """`note_delete.delete_note`: the page's DELETE and the agent / MCP tool both use it."""

    def check(self, family, make_note, attach_to):
        gone, kept = make_note("gone"), make_note("kept")
        gone_row, kept_row = attach_to(gone), attach_to(kept)
        second = attach_to(gone)  # a note can have several files
        names = [gone_row.note_attachment_url.name, second.note_attachment_url.name]
        self.assertTrue(all(f"/{gone.note_id}/" in n for n in names), names)
        with self.captureOnCommitCallbacks(execute=True):
            note_delete.delete_note(team_id=self.team.team_id, family=family, note_id=gone.note_id)
        self.assertRemoved(gone_row, names[0])
        self.assertRemoved(second, names[1])
        self.assertPresent(kept_row)

    def test_personal_note(self):
        self.check(
            "personal",
            lambda t: self.personal(t),
            lambda n: attach(PersonalNoteAttachmentFact, n, self.user),
        )

    def test_task_note(self):
        project, task, _ = self.project_task_note()

        def make(title):
            return TaskNoteMaster.objects.create(
                team=self.team, owner=self.user, project=project, task=task, title=title, body=BODY
            )

        self.check("task", make, lambda n: attach(TaskNoteAttachmentFact, n, self.user))

    def test_chat_note(self):
        self.check("chat", self.chat_note, lambda n: attach_chat(n, self.user))

    def test_a_note_with_the_same_number_in_another_family_keeps_its_file(self):
        """The three tables number their notes separately, so ids collide."""
        personal = self.personal()
        project, task, _ = self.project_task_note()
        task_note = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=project,
            task=task,
            title="same number",
            body=BODY,
            note_id=personal.note_id,
        )
        kept = attach(TaskNoteAttachmentFact, task_note, self.user)
        with self.captureOnCommitCallbacks(execute=True):
            note_delete.delete_note(
                team_id=self.team.team_id, family="personal", note_id=personal.note_id
            )
        self.assertPresent(kept)

    def test_a_note_with_no_files_deletes_as_before(self):
        note = self.personal()
        with self.captureOnCommitCallbacks(execute=True):
            note_delete.delete_note(
                team_id=self.team.team_id, family="personal", note_id=note.note_id
            )
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=note.note_id).exists())


class FoldersTakeTheirNotesFiles(NoteAttachmentsBase):
    def test_my_notes_folder_delete(self):
        folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="F", scope=PersonalNoteFolder.SCOPE_PERSONAL
        )
        inside, outside = self.personal(folder=folder), self.personal()
        gone, kept = (
            attach(PersonalNoteAttachmentFact, inside, self.user),
            attach(PersonalNoteAttachmentFact, outside, self.user),
        )
        name = gone.note_attachment_url.name
        with self.captureOnCommitCallbacks(execute=True):
            note_delete.delete_personal_folder(
                team_id=self.team.team_id, owner_id=self.user.id, folder_id=folder.folder_id
            )
        self.assertRemoved(gone, name)
        self.assertPresent(kept)

    def test_team_notes_folder_delete(self):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="T",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )
        inside, outside = self.personal(folder=folder), self.personal()
        gone, kept = (
            attach(PersonalNoteAttachmentFact, inside, self.user),
            attach(PersonalNoteAttachmentFact, outside, self.user),
        )
        name = gone.note_attachment_url.name
        with mock.patch.object(note_delete, "require_team_folder_owner"):
            with self.captureOnCommitCallbacks(execute=True):
                note_delete.delete_team_folder(
                    team_id=self.team.team_id, user_id=self.user.id, folder=folder
                )
        self.assertRemoved(gone, name)
        self.assertPresent(kept)


class CascadingDeletes(NoteAttachmentsBase):
    """The notes go with their task or project (a cascade), not through `delete_note`."""

    def test_task_delete(self):
        project, task, note = self.project_task_note()
        sibling_task = TaskMaster.objects.create(
            team=self.team, project=project, title="t2", status="Open"
        )
        sibling = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=project,
            task=sibling_task,
            title="s",
            body=BODY,
        )
        gone, kept = (
            attach(TaskNoteAttachmentFact, note, self.user),
            attach(TaskNoteAttachmentFact, sibling, self.user),
        )
        name = gone.note_attachment_url.name
        self.authenticate(self.user)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.delete(
                f"/api/v2/task/?team_id={self.team.team_id}&task_id={task.task_id}"
                "&is_init_task_boolean=0"
            )
        self.assertEqual(res.status_code, 204, res.content)
        self.assertFalse(TaskNoteMaster.objects.filter(note_id=note.note_id).exists())
        self.assertRemoved(gone, name)
        self.assertPresent(kept)

    def test_project_delete(self):
        project, _task, note = self.project_task_note()
        other, _t, other_note = self.project_task_note("Q")
        # a note filed to the project but not to one of its tasks
        loose = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=project, title="loose", body=BODY
        )
        rows = [attach(TaskNoteAttachmentFact, n, self.user) for n in (note, loose)]
        names = [r.note_attachment_url.name for r in rows]
        kept = attach(TaskNoteAttachmentFact, other_note, self.user)
        with self.captureOnCommitCallbacks(execute=True):
            delete_project(project)
        for row, name in zip(rows, names):
            self.assertRemoved(row, name)
        self.assertPresent(kept)
        self.assertTrue(ProjectMaster.objects.filter(pk=other.pk).exists())

    def test_account_erasure(self):
        mine = self.personal(owner=self.user2)
        theirs = self.personal(owner=self.user)
        gone = attach(PersonalNoteAttachmentFact, mine, self.user2)
        kept = attach(PersonalNoteAttachmentFact, theirs, self.user)
        name = gone.note_attachment_url.name
        with self.captureOnCommitCallbacks(execute=True):
            delete_account(self.user2)
        self.assertFalse(PersonalNoteMaster.objects.filter(note_id=mine.note_id).exists())
        self.assertRemoved(gone, name)
        self.assertPresent(kept)


class TransactionAndStorageContract(NoteAttachmentsBase):
    def test_a_rolled_back_delete_touches_neither_row_nor_file(self):
        note = self.personal()
        row = attach(PersonalNoteAttachmentFact, note, self.user)
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    note_delete.forget_note_attachments(1, [note.note_id])
                    raise RuntimeError("the delete failed afterwards")
        self.assertPresent(row)

    def test_the_file_is_not_deleted_before_the_commit(self):
        note = self.personal()
        row = attach(PersonalNoteAttachmentFact, note, self.user)
        name = row.note_attachment_url.name
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            note_delete.forget_note_attachments(1, [note.note_id])
            self.assertFalse(PersonalNoteAttachmentFact.objects.filter(pk=row.pk).exists())
            self.assertTrue(default_storage.exists(name), "deleted before the commit")
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertFalse(default_storage.exists(name))

    def test_a_storage_failure_does_not_undo_or_raise(self):
        note = self.personal()
        row = attach(PersonalNoteAttachmentFact, note, self.user)
        with mock.patch.object(default_storage, "delete", side_effect=OSError("down")):
            with self.assertLogs(note_delete.logger, "WARNING") as logs:
                with self.captureOnCommitCallbacks(execute=True):
                    removed = note_delete.forget_note_attachments(1, [note.note_id])
        self.assertEqual(removed, 1)
        self.assertFalse(PersonalNoteAttachmentFact.objects.filter(pk=row.pk).exists())
        # the family, never the file name (the uploader's text)
        self.assertIn("family personal", logs.output[0])
        self.assertNotIn(row.note_attachment_url.name, logs.output[0])

    def test_a_long_list_of_notes_is_handled_in_batches(self):
        notes = [self.personal(f"n{i}") for i in range(3)]
        rows = [attach(PersonalNoteAttachmentFact, n, self.user) for n in notes]
        with mock.patch.object(note_delete, "_FORGET_BATCH", 2):
            with self.captureOnCommitCallbacks(execute=True):
                removed = note_delete.forget_note_attachments(1, [n.note_id for n in notes])
        self.assertEqual(removed, 3)
        self.assertFalse(PersonalNoteAttachmentFact.objects.filter(pk__in=[r.pk for r in rows]))


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class DemoTeardown(Files, CheapFlushTransactionTestCase):
    """`delete_demo_team_data`. Real commits: the teardown's deferred FKs are checked at COMMIT."""

    def test_the_teams_notes_files_go_and_another_teams_stay(self):
        demo = User.objects.create_user(
            username="demo_att", email="demo_att@example.com", password="x", is_demo=True
        )
        other = User.objects.create_user(
            username="demo_att2", email="demo_att2@example.com", password="x", is_demo=True
        )
        team_id = create_demo_environment(demo)["team_id"]
        other_team_id = create_demo_environment(other)["team_id"]

        def first(model, tid):
            return model.objects.filter(team=tid).first()

        mine, theirs = [], []
        for note_model, att_model, extra in (
            (PersonalNoteMaster, PersonalNoteAttachmentFact, {}),
            (TaskNoteMaster, TaskNoteAttachmentFact, {}),
            (ChatNoteMaster, ChatNoteAttachmentFact, {"chat_type": 3, "is_thread": False}),
        ):
            for tid, bucket, owner in ((team_id, mine, demo), (other_team_id, theirs, other)):
                note = first(note_model, tid)
                if note is None:  # the seed has no chat notes: a visitor writes them
                    self.assertIs(note_model, ChatNoteMaster)
                    note = ChatNoteMaster.objects.create(
                        team_id=tid,
                        owner=owner,
                        chat_type=int(ChannelKind.PM),
                        channel=Channel.objects.filter(team=tid, kind=ChannelKind.PM).first(),
                        is_thread=False,
                        title="chat note",
                        body=[],
                    )
                bucket.append(attach(att_model, note, owner, **extra))
        names = [r.note_attachment_url.name for r in mine]

        delete_demo_team_data(team_id)

        for row, name in zip(mine, names):
            self.assertRemoved(row, name)
        for row in theirs:
            self.assertPresent(row)
