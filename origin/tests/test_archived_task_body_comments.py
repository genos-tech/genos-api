"""MIP-1693 (Ken, 2026-11-22): in an archived project, comments on a task BODY are refused like note
comments: `comment_on_task_body`, `reply_to_task_body_comment` and `resolve_task_body_comment` write
into the body's Yjs doc through collab, so they refuse before anything is sent to collab."""

from __future__ import annotations

from unittest import mock

from origin.search_engine.agent.tools.base import ToolError
from origin.services.archive import ARCHIVED_MESSAGE
from origin.tests.test_archived_note_tools import ArchivedNoteTools as Base
from origin.tests.test_archived_note_tools import _tool


class ArchivedTaskBodyComments(Base):
    ARGS = {
        "comment_on_task_body": {"text": "hi", "anchor_text": "secret"},
        "reply_to_task_body_comment": {"thread_id": "t1", "text": "hi"},
        "resolve_task_body_comment": {"thread_id": "t1"},
    }

    def _args(self, name):
        return {"task_id": self.task.task_id, **self.ARGS[name]}

    def test_task_body_comment_tools_refused_when_archived(self):
        self.archive()
        with mock.patch("origin.services.collab_push.requests") as net:
            for name in self.ARGS:
                with self.subTest(tool=name):
                    with self.assertRaises(ToolError) as caught:
                        _tool(name).run(self._args(name), self.ctx)
                    self.assertIn(ARCHIVED_MESSAGE, str(caught.exception))
            net.post.assert_not_called()

    def test_live_project_comment_reaches_collab(self):
        self.archive()
        self.unarchive()
        with mock.patch(
            "origin.services.collab_push.create_thread_in",
            return_value={"threadId": "t9", "commentId": "c9"},
        ) as create:
            _tool("comment_on_task_body").run(self._args("comment_on_task_body"), self.ctx)
        create.assert_called_once()
