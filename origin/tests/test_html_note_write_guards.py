"""MIP-2218: every write path that would change an HTML note's placeholder text refuses, guiding.

An HTML note's `body` is a placeholder made from its page. Refused with a sentence that says what to
do instead, writing nothing: REST `PUT` of a personal, task and chat note, the collab document-open
probe (read-only for everyone, comments included, as for an archived document: genos-collab already
honours that), and the three MCP note-thread tools.
"""

from __future__ import annotations

from django.test import override_settings

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.services import html_notes
from origin.tests.test_html_notes_api import HtmlNoteApi
from origin.tests.test_mcp_note_thread_writes import (
    REPLY,
    RESOLVE,
    NoteThreadWriteBase,
)
from origin.views.utils.note_role import NOTE_TYPE_CHAT, NOTE_TYPE_PERSONAL, NOTE_TYPE_TASK

NEW_BODY = [{"id": "b1", "type": "paragraph", "content": [{"type": "text", "text": "overwrite"}]}]
PUT_PATH = {
    NOTE_TYPE_PERSONAL: "/api/v2/note/personal/",
    NOTE_TYPE_TASK: "/api/v2/note/task/",
    NOTE_TYPE_CHAT: "/api/v2/note/chat/",
}
MODELS = {
    NOTE_TYPE_PERSONAL: PersonalNoteMaster,
    NOTE_TYPE_TASK: TaskNoteMaster,
    NOTE_TYPE_CHAT: ChatNoteMaster,
}


class RestPutRefuses(HtmlNoteApi):
    def put(self, code, **extra):
        note = self.notes[code]
        payload = {
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.id),
            "note_id": note.note_id,
            "title": "new title",
            "body": NEW_BODY,
            **extra,
        }
        self.authenticate(self.user)
        return self.client.put(PUT_PATH[code], payload, format="json")

    def test_put_refuses_each_family_and_writes_nothing(self):
        for code in (NOTE_TYPE_PERSONAL, NOTE_TYPE_TASK, NOTE_TYPE_CHAT):
            with self.subTest(code=code):
                before = MODELS[code].objects.get(note_id=self.notes[code].note_id)
                title, body = before.title, before.body
                res = self.put(code)
                self.assertEqual(res.status_code, 409, res.content)
                self.assertIn("save_html_note", res.json()["detail"])
                self.assertEqual(res.json()["body_format"], "html")
                after = MODELS[code].objects.get(note_id=self.notes[code].note_id)
                self.assertEqual((after.title, after.body), (title, body))
                self.assertEqual(after.body_format, "html")

    def test_an_ordinary_note_still_saves(self):
        note = self.notes[NOTE_TYPE_PERSONAL]
        PersonalNoteMaster.objects.filter(note_id=note.note_id).update(body_format="blocknote")
        res = self.put(NOTE_TYPE_PERSONAL)
        self.assertNotEqual(res.status_code, 409, res.content)
        self.assertEqual(PersonalNoteMaster.objects.get(note_id=note.note_id).title, "new title")


class CollabProbeHoldsHtmlNotesReadOnly(HtmlNoteApi):
    def check(self, prefix, note):
        self.authenticate(self.user)
        return self.client.post(
            "/api/v2/collab/access/check/",
            {"document_name": f"{prefix}:{note.note_id}"},
            format="json",
        )

    def test_html_notes_answer_viewer_and_archived_so_collab_refuses_writes_and_comments(self):
        for prefix, code in (
            ("my-note", NOTE_TYPE_PERSONAL),
            ("task-note", NOTE_TYPE_TASK),
            ("chat-note", NOTE_TYPE_CHAT),
        ):
            with self.subTest(prefix=prefix):
                res = self.check(prefix, self.notes[code])
                self.assertEqual(res.status_code, 200, res.content)
                self.assertEqual(res.json(), {"role_id": 3, "archived": True, "html_note": True})

    def test_an_ordinary_note_keeps_its_role(self):
        note = self.notes[NOTE_TYPE_PERSONAL]
        PersonalNoteMaster.objects.filter(note_id=note.note_id).update(body_format="blocknote")
        res = self.check("my-note", note)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertNotIn("archived", res.json())
        self.assertIn(res.json()["role_id"], (1, 2))


@override_settings(HTML_NOTES=True)
class McpThreadToolsRefuse(NoteThreadWriteBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)
        html_notes.mark_html(NOTE_TYPE_PERSONAL, self.note.note_id)

    def assertRefused(self, name, **extra):
        if name == "comment_on_note":
            args = {"note_type": "personal", "note_id": self.note.note_id, **extra}
            text, is_error = self._tool_text(self._call(name, args))
        else:
            text, is_error = self.run_tool(name, **extra)
        self.assertTrue(is_error, text)
        self.assertIn("HTML note", text)
        self.assertIn("comment on the task", text)
        self.assertIn("Nothing was changed", text)
        self.post.assert_not_called()

    def test_reply_refused(self):
        self.assertRefused(REPLY, text="hi")

    def test_resolve_refused(self):
        self.assertRefused(RESOLVE)

    def test_comment_on_note_refused(self):
        self.assertRefused("comment_on_note", text="hi", anchor_text="Plan")

    def test_an_ordinary_note_still_reaches_collab(self):
        from origin.models.note.personal_note_models import PersonalNoteMaster as P

        P.objects.filter(note_id=self.note.note_id).update(body_format="blocknote")
        text, is_error = self.run_tool(REPLY, text="hi")
        self.assertFalse(is_error, text)
        self.post.assert_called()
