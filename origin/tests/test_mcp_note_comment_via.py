"""`get_note` over MCP shows which comments an AI app wrote, to MCP readers only (MIP-1759).

A comment written over MCP carries the reduced source `{kind, app?}` in its Yjs metadata
(`note_threads._common.reduced_via`); genos-collab's `read-threads` hands it back as the comment's
`via`. `get_note` (MCP) now puts it on the comment. What is pinned:

- the shape: a kind, and a connected app's official family, nothing else; a key's name, prefix or id,
  a connection id, an unknown kind or family, and anything that is not a dict never come out, whoever
  reads (the colleague's key and the author's own key see the same);
- a comment with no `via` (a person's) has no `via` key;
- the in-app agent's `fetch_note` is byte-identical with and without the data, flag on: it shares
  `note_comment_fields`, and `include_via` is off by default;
- access is the note's own: a colleague with no access reads nothing and collab is never asked.
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import override_settings

from origin.models.common.api_key_models import SCOPE_READ
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.services import collab_push, note_comments
from origin.tests import test_agent_note_threads_in_get_note as in_app
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp.note_threads import _common

COLLAB = "http://collab.internal:8891"
COLLAB_SECRET = "test-only-collab-internal-secret"
OWNER_ROLE, VIEWER_ROLE = 1, 3
AT = "2026-11-06T09:00:00.000Z"


def _comment(user_id, text, **extra):
    return {
        "id": f"c-{text}",
        "userId": user_id,
        "createdAt": AT,
        "updatedAt": AT,
        "deleted": False,
        "body": [{"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}],
        **extra,
    }


def _thread(comments):
    return {
        "id": "t-1",
        "resolved": False,
        "resolvedBy": None,
        "resolvedAt": None,
        "createdAt": AT,
        "anchorText": "Retention",
        "comments": comments,
    }


@override_settings(COLLAB_INTERNAL_URL=COLLAB, COLLAB_INTERNAL_SECRET=COLLAB_SECRET)
class CommentViaOverMcp(McpBase):
    def setUp(self):
        super().setUp()
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan", body=[]
        )
        for user, role in ((self.user, OWNER_ROLE), (self.user2, VIEWER_ROLE)):
            NotePermissionMaster.objects.create(
                team=self.team, user=user, note_id=self.note.note_id, note_type=1, role_id=role
            )
        self.uid = str(self.user.id)
        patcher = mock.patch.object(collab_push.requests, "post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def collab_says(self, comments):
        body = {"threads": [_thread(comments)]}
        self.post.return_value = mock.Mock(status_code=200, json=mock.Mock(return_value=body))

    def read(self, user=None):
        self._key(scope=SCOPE_READ, user=user or self.user)
        text, is_error = self._tool_text(
            self._call("get_note", {"note_id": str(self.note.note_id), "note_type": "personal"})
        )
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])["comment_threads"][0]["comments"]

    def test_the_writers_marker_comes_back_as_kind_and_official_family(self):
        self.collab_says(
            [
                _comment(self.uid, "key", via={"kind": "api_key"}),
                _comment(self.uid, "claude", via={"kind": "oauth", "app": "claude"}),
                _comment(self.uid, "chatgpt", via={"kind": "oauth", "app": "chatgpt"}),
                _comment(self.uid, "person"),
            ]
        )

        key, claude, chatgpt, person = self.read()

        self.assertEqual(key["via"], {"kind": "api_key"})
        self.assertEqual(claude["via"], {"kind": "oauth", "app": "claude"})
        self.assertEqual(chatgpt["via"], {"kind": "oauth", "app": "chatgpt"})
        self.assertNotIn("via", person, "a person's comment has no via key")

    def test_a_colleagues_key_sees_only_the_kind(self):
        """Collab is fed everything a key or a connection could carry: only the kind (and the
        official family) is ever shown, to the colleague and to the author alike."""
        smuggled = {
            "kind": "api_key",
            "key_id": "k-123",
            "key_name": "my-laptop-claude",
            "key_prefix": "gnos_ab12cd",
            "connection_id": "conn-9",
            "client_name": "Ken's private client",
            "app": "claude",
        }
        self.collab_says([_comment(self.uid, "x", via=smuggled)])

        for user in (self.user2, self.user):
            (comment,) = self.read(user)
            self.assertEqual(comment["via"], {"kind": "api_key"}, user.username)
            blob = json.dumps(comment)
            for secret in ("my-laptop-claude", "gnos_ab12cd", "k-123", "conn-9", "private client"):
                self.assertNotIn(secret, blob)

    def test_an_oauth_comment_shows_the_family_never_the_connection_or_client_name(self):
        smuggled = {"kind": "oauth", "app": "claude", "connection_id": "c-1", "client_name": "Mine"}
        self.collab_says([_comment(self.uid, "x", via=smuggled)])

        (comment,) = self.read(self.user2)

        self.assertEqual(comment["via"], {"kind": "oauth", "app": "claude"})

    def test_a_family_that_is_not_official_is_dropped_and_the_kind_stays(self):
        self.collab_says([_comment(self.uid, "x", via={"kind": "oauth", "app": "evil-corp"})])
        (comment,) = self.read(self.user2)
        self.assertEqual(comment["via"], {"kind": "oauth"})

    def test_what_is_not_a_known_kind_is_not_shown_at_all(self):
        bad = [
            {"kind": "github", "github_user": "someone"},
            {"kind": "pr_merge", "pr_url": "https://example.com"},
            {"kind": "anything-else"},
            {"kind": ["api_key"]},
            {"app": "claude"},
            {},
            "api_key",
            ["api_key"],
            7,
            None,
        ]
        self.collab_says([_comment(self.uid, f"c{i}", via=v) for i, v in enumerate(bad)])

        comments = self.read(self.user2)

        self.assertEqual(len(comments), len(bad))
        for comment in comments:
            self.assertNotIn("via", comment, comment)

    def test_a_deleted_comment_has_no_via(self):
        gone = {
            **_comment(self.uid, "gone", via={"kind": "api_key"}),
            "deleted": True,
            "body": None,
        }
        self.collab_says([gone])
        (comment,) = self.read(self.user2)
        self.assertTrue(comment["deleted"])
        self.assertNotIn("via", comment)

    def test_a_colleague_with_no_access_to_the_note_reads_nothing_and_collab_is_not_asked(self):
        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self.collab_says([_comment(self.uid, "x", via={"kind": "api_key"})])
        self._key(scope=SCOPE_READ, user=self.user2)

        text, is_error = self._tool_text(
            self._call("get_note", {"note_id": str(self.note.note_id), "note_type": "personal"})
        )

        self.assertTrue(is_error)
        self.assertNotIn("api_key", text)
        self.post.assert_not_called()

    def test_the_kinds_read_are_the_kinds_written(self):
        self.assertEqual(note_comments.COMMENT_VIA_KINDS, _common._VIA_KINDS)


class InAppFetchNoteIsUnchanged(in_app.NoteBase):
    """The in-app `fetch_note` (flag on, the only way it reads comments) returns the same bytes
    whether or not the stored comments carry a `via`."""

    def stored(self, **extra):
        thread = self.thread()
        thread["comments"][0].update(extra)
        return [thread]

    def run_tool(self, threads, **kw):
        with in_app.on(), mock.patch(in_app.READER, return_value=threads):
            return self.note("task", **kw)

    def test_a_via_on_a_stored_comment_changes_nothing_the_in_app_model_reads(self):
        without = self.run_tool(self.stored())
        with_via = self.run_tool(self.stored(via={"kind": "oauth", "app": "claude"}))

        self.assertEqual(json.dumps(with_via, sort_keys=True), json.dumps(without, sort_keys=True))
        self.assertNotIn('"via"', json.dumps(with_via))
        self.assertNotIn("oauth", json.dumps(with_via))

    def test_the_shared_reader_adds_nothing_unless_asked(self):
        threads = self.stored(via={"kind": "api_key"})
        with mock.patch(in_app.READER, return_value=threads):
            default = note_comments.note_comment_fields("task", self.task_note.note_id, self.ctx())
            asked = note_comments.note_comment_fields(
                "task", self.task_note.note_id, self.ctx(), include_via=True
            )

        self.assertNotIn("via", default["comment_threads"][0]["comments"][0])
        self.assertEqual(asked["comment_threads"][0]["comments"][0]["via"], {"kind": "api_key"})
