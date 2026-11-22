"""MIP-1693 (Ken, 2026-11-22): in an archived project, note COMMENTS are refused (the project is
read-only) while note SHARING stays allowed (it is access management).

`comment_on_note`, `reply_to_note_comment` and `resolve_note_comment` (MCP, and the in-app
`comment_on_note`, which runs the MCP tool) write into the note's Yjs doc through collab, so the REST
guard cannot reach them. They refuse before anything is sent to collab.
"""

from __future__ import annotations

from unittest import mock

from origin.search_engine.agent.tools.base import ToolError
from origin.services.archive import ARCHIVED_MESSAGE
from origin.tests.test_archived_note_tools import ArchivedNoteTools as Base
from origin.tests.test_archived_note_tools import _tool
from origin.views.utils.note_role import NOTE_TYPE_TASK, ROLE_VIEWER


class ArchivedNoteComments(Base):
    ARGS = {
        "comment_on_note": {"text": "hi", "anchor_text": "secret"},
        "reply_to_note_comment": {"thread_id": "t1", "text": "hi"},
        "resolve_note_comment": {"thread_id": "t1"},
    }

    def _args(self, name, note_type, note_id):
        return {"note_type": note_type, "note_id": note_id, **self.ARGS[name]}

    def test_comment_tools_refused_for_task_and_chat_notes(self):
        self.archive()
        with mock.patch("origin.services.collab_push.requests") as net:
            for name in self.ARGS:
                for kind, nid in (("task", self.note.note_id), ("chat", self.chat_note.note_id)):
                    with self.subTest(tool=name, kind=kind):
                        with self.assertRaises(ToolError) as caught:
                            _tool(name).run(self._args(name, kind, nid), self.ctx)
                        self.assertIn(ARCHIVED_MESSAGE, str(caught.exception))
            net.post.assert_not_called()

    def test_live_project_comment_reaches_collab(self):
        self.archive()
        self.unarchive()
        with mock.patch(
            "origin.services.collab_push.create_note_thread",
            return_value={"threadId": "t9", "commentId": "c9"},
        ) as create:
            _tool("comment_on_note").run(
                self._args("comment_on_note", "task", self.note.note_id), self.ctx
            )
        create.assert_called_once()

    def test_sharing_still_allowed_in_archived_project(self):
        self.archive()
        args = {"note_type": "task", "note_id": self.note.note_id, "user_id": str(self.stranger.id)}
        out = _tool("manage_note_sharing").run(
            {**args, "action": "share", "role": "viewer"}, self.ctx
        )
        self.assertEqual(out["action"], "share")
        self.assertEqual(self.explicit(self.stranger, NOTE_TYPE_TASK, self.note), ROLE_VIEWER)
        _tool("manage_note_sharing").run({**args, "action": "unshare"}, self.ctx)
        self.assertIsNone(self.explicit(self.stranger, NOTE_TYPE_TASK, self.note))

    def test_rest_note_role_still_allowed_in_archived_project(self):
        self.archive()
        self.authenticate(self.author)
        res = self.client.post(
            "/api/v2/note/role/",
            {
                "team_id": str(self.team.team_id),
                "note_type": NOTE_TYPE_TASK,
                "note_id": self.note.note_id,
                "target_user_id": str(self.stranger.id),
                "role_id": ROLE_VIEWER,
            },
            format="json",
        )
        self.assertIn(res.status_code, (200, 201), res.content)
        self.assertEqual(self.explicit(self.stranger, NOTE_TYPE_TASK, self.note), ROLE_VIEWER)
