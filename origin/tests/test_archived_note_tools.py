"""MIP-1939: the note tools of the in-app agent and of MCP refuse an archived project's notes.

`create_note`, `update_note`, `delete_note`, `restore_note_version` and `edit_note_block` write note
rows directly, so the REST guard of MIP-1934 cannot reach them. They answer the same refusal text,
change nothing, reads keep working, and a live project is unaffected.
"""

from __future__ import annotations

from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.services.archive import ARCHIVED_MESSAGE
from origin.tests import test_task_personal_note_roles_revoked as base


def _tool(name):
    return REGISTRY.get(name) or MCP_ONLY_REGISTRY[name]


class ArchivedNoteTools(base.TaskNoteBase):
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
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.author.id))

    def archive(self):
        self.project.archived_at = timezone.now()
        self.project.save(update_fields=["archived_at"])

    def unarchive(self):
        self.project.archived_at = None
        self.project.save(update_fields=["archived_at"])

    def refused(self, name, args):
        with self.assertRaises(ToolError) as caught:
            _tool(name).run(args, self.ctx)
        self.assertIn(ARCHIVED_MESSAGE, str(caught.exception))

    def test_create_task_note_refused(self):
        self.archive()
        before = TaskNoteMaster.objects.count()
        self.refused(
            "create_note",
            {
                "note_type": "task",
                "title": "n",
                "content_text": "x",
                "project_id": self.project.project_id,
            },
        )
        self.assertEqual(TaskNoteMaster.objects.count(), before)

    def test_create_chat_note_refused(self):
        self.archive()
        before = ChatNoteMaster.objects.count()
        self.refused(
            "create_note",
            {
                "note_type": "chat",
                "title": "n",
                "content_text": "x",
                "chat_id": str(self.channel.id),
            },
        )
        self.assertEqual(ChatNoteMaster.objects.count(), before)

    def test_update_refused_for_task_and_chat_notes(self):
        self.archive()
        self.refused(
            "update_note",
            {"note_type": "task", "note_id": self.note.note_id, "title": "changed"},
        )
        self.refused(
            "update_note",
            {"note_type": "chat", "note_id": self.chat_note.note_id, "title": "changed"},
        )
        self.note.refresh_from_db()
        self.assertEqual(self.note.title, "Task notes")

    def test_delete_refused_for_task_and_chat_notes(self):
        self.archive()
        self.refused("delete_note", {"note_type": "task", "note_id": self.note.note_id})
        self.refused("delete_note", {"note_type": "chat", "note_id": self.chat_note.note_id})
        self.assertTrue(TaskNoteMaster.objects.filter(note_id=self.note.note_id).exists())
        self.assertTrue(ChatNoteMaster.objects.filter(note_id=self.chat_note.note_id).exists())

    def test_restore_version_refused(self):
        self.archive()
        self.refused(
            "restore_note_version",
            {"note_type": "task", "note_id": self.note.note_id, "version_no": 1},
        )

    def test_edit_block_refused(self):
        self.archive()
        self.refused(
            "edit_note_block",
            {
                "note_type": "task",
                "note_id": self.note.note_id,
                "op": "delete",
                "block_id": "b1",
            },
        )

    def test_reads_still_work(self):
        self.archive()
        out = _tool("fetch_note").run({"note_type": "task", "note_id": self.note.note_id}, self.ctx)
        self.assertIn("secret body", str(out))

    def test_live_project_unaffected_and_unarchive_restores_writes(self):
        self.archive()
        self.unarchive()
        out = _tool("update_note").run(
            {"note_type": "task", "note_id": self.note.note_id, "title": "changed"}, self.ctx
        )
        self.assertEqual(out["note_id"], self.note.note_id)
        self.note.refresh_from_db()
        self.assertEqual(self.note.title, "changed")
