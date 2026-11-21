"""MIP-1938: the collab access probe answers read-only for the documents of an archived project.

genos-collab opens a connection read-only when the probe says Viewer, and refuses every update on it.
The probe is the one place that knows the archive, so an archived project's task notes, chat notes and
task bodies answer `role_id` 3 with `archived: true` (collab then also refuses viewer comments).
Personal notes are never archived; reads keep working (200, not 404); restoring brings write back.
"""

from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.tests import test_task_personal_note_roles_revoked as base

from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_OWNER

URL = "/api/v2/collab/access/check/"
VIEWER = 3


class ArchivedCollabAccess(base.TaskNoteBase):
    def setUp(self):
        super().setUp()
        self.project = self.project_of("Alpha", self.author)
        self.task = self.task_in(self.project)
        self.note = self.create_task_note(self.author, self.task)
        self.channel = Channel.objects.get(project=self.project, kind=ChannelKind.PM)
        for u in (self.owner, self.author):
            ChannelMember.objects.get_or_create(channel=self.channel, user=u)
        res = self.client.post(
            "/api/v2/note/chat/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.author.id),
                "chat_id": str(self.channel.id),
                "title": "chat note",
                "body": base.BODY,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.chat_note = ChatNoteMaster.objects.get(note_id=res.json()["noteId"])
        self.personal = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.author, title="mine", body=base.BODY
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.author,
            note_id=self.personal.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_OWNER,
        )
        self.authenticate(self.author)

    def check(self, name):
        return self.client.post(URL, {"document_name": name}, format="json")

    def archive(self, on=True):
        self.project.archived_at = timezone.now() if on else None
        self.project.save(update_fields=["archived_at"])

    def names(self):
        return {
            "task-note": f"task-note:{self.note.note_id}",
            "chat-note": f"chat-note:{self.chat_note.note_id}",
            "task-body": f"task-body:{self.task.task_id}",
        }

    def test_live_project_is_writable(self):
        for name in self.names().values():
            body = self.check(name).json()
            self.assertNotEqual(body.get("role_id"), VIEWER, name)
            self.assertNotIn("archived", body, name)

    def test_archived_project_documents_answer_read_only(self):
        self.archive()
        for name in self.names().values():
            res = self.check(name)
            self.assertEqual(res.status_code, 200, name)
            self.assertEqual(res.json(), {"role_id": VIEWER, "archived": True}, name)

    def test_restore_brings_write_back(self):
        self.archive()
        self.archive(False)
        for name in self.names().values():
            self.assertNotIn("archived", self.check(name).json(), name)

    def test_archived_group_chat_note_is_read_only(self):
        Channel.objects.filter(pk=self.channel.pk).update(archived_at=timezone.now())
        res = self.check(f"chat-note:{self.chat_note.note_id}")
        self.assertEqual(res.json(), {"role_id": VIEWER, "archived": True})

    def test_personal_note_never_archived(self):
        self.archive()
        body = self.check(f"my-note:{self.personal.note_id}").json()
        self.assertNotIn("archived", body)
        self.assertNotEqual(body["role_id"], VIEWER)

    def test_no_access_still_404_in_archived_project(self):
        self.archive()
        self.authenticate(self.stranger)
        for name in self.names().values():
            self.assertEqual(self.check(name).status_code, 404, name)
