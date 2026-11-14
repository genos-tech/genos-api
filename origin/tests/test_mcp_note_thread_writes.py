"""`reply_to_note_comment` and `resolve_note_comment` over MCP (MIP-1100).

A comment lives in the note's Yjs doc, so each tool asks genos-collab's internal endpoint
(`reply-thread` / `resolve-thread`); collab is stubbed here at `requests.post`. What is pinned:

- access: the caller must be able to read the note (as `get_note` decides; whoever can open a
  note may comment on it in the editor, viewers included), and collab is never asked otherwise;
- resolve: the note's owner is told to collab as `mayResolveAny`, nobody else is; collab, which
  reads the thread's author from the doc, has the last word, and its refusal reads clearly;
- the reply's `via` marker is the reduced one, never a key's name or a connection id;
- an older collab (which answers any unknown path "200 OK") fails safe, with a clear error;
- both tools are MCP-only: never in the in-app registry or its declarations.
"""

from __future__ import annotations

import json
from unittest import mock

import jwt
import requests
from django.contrib.auth import get_user_model
from django.test import override_settings

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.common.team_models import TeamMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.services import collab_push
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import tools as mcp_tools
from origin.views.public.mcp.note_threads import _common

User = get_user_model()

COLLAB = "http://collab.internal:8891"
COLLAB_SECRET = "test-only-collab-internal-secret"
REPLY = "reply_to_note_comment"
RESOLVE = "resolve_note_comment"
THREAD = "6c1c0a52-2a36-4a2f-93cb-0d1a1b6f8c11"

OWNER_ROLE, EDITOR_ROLE, VIEWER_ROLE = 1, 2, 3


@override_settings(COLLAB_INTERNAL_URL=COLLAB, COLLAB_INTERNAL_SECRET=COLLAB_SECRET)
class NoteThreadWriteBase(McpBase):
    def setUp(self):
        super().setUp()
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan", body=[]
        )
        self._grant(self.user, OWNER_ROLE)
        patcher = mock.patch.object(collab_push.requests, "post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)
        self.answer({"ok": True, "commentId": "c-9", "commentCount": 2, "threadResolved": False})

    def _grant(self, user, role):
        NotePermissionMaster.objects.create(
            team=self.team, user=user, note_id=self.note.note_id, note_type=1, role_id=role
        )

    def answer(self, body, status=200):
        self.post.return_value = mock.Mock(status_code=status, json=mock.Mock(return_value=body))

    def args(self, **extra):
        return {
            "note_type": "personal",
            "note_id": self.note.note_id,
            "thread_id": THREAD,
            **extra,
        }

    def run_tool(self, name, **extra):
        text, is_error = self._tool_text(self._call(name, self.args(**extra)))
        return text, is_error

    def ok(self, name, **extra):
        text, is_error = self.run_tool(name, **extra)
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def refused(self, name, **extra):
        text, is_error = self.run_tool(name, **extra)
        self.assertTrue(is_error, text)
        return text

    @property
    def sent(self):
        return self.post.call_args.kwargs["json"]


class ReplyTests(NoteThreadWriteBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)

    def test_a_reply_asks_collab_for_this_notes_doc_as_the_caller(self):
        out = self.ok(REPLY, text="Because the audit needs six months.")

        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0], f"{COLLAB}/internal/documents/reply-thread")
        self.assertEqual(
            self.sent,
            {
                "documentName": f"my-note:{self.note.note_id}",
                "threadId": THREAD,
                "userId": str(self.user.id),
                "text": "Because the audit needs six months.",
                "metadata": {"via": {"kind": "api_key"}},
            },
        )
        self.assertFalse(self.post.call_args.kwargs["allow_redirects"])
        token = self.post.call_args.kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token, COLLAB_SECRET, algorithms=["HS256"], audience="genos-collab-internal"
        )
        self.assertEqual(claims["aud"], "genos-collab-internal")
        self.assertEqual(out["comment_id"], "c-9")
        self.assertEqual(out["comment_count"], 2)
        self.assertIs(out["thread_resolved"], False)
        self.assertEqual(out["thread_id"], THREAD)
        self.assertEqual(out["note_id"], self.note.note_id)

    def test_a_task_note_and_a_chat_note_name_their_own_doc(self):
        from origin.models.note.task_note_models import TaskNoteMaster

        task_note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            owner=self.user,
            title="T",
            body=[],
        )
        text, is_error = self._tool_text(
            self._call(
                REPLY,
                {
                    "note_type": "task",
                    "note_id": task_note.note_id,
                    "thread_id": THREAD,
                    "text": "x",
                },
            )
        )
        self.assertFalse(is_error, text)
        self.assertEqual(self.sent["documentName"], f"task-note:{task_note.note_id}")

    def test_a_note_url_names_the_note_and_the_family(self):
        text, is_error = self._tool_text(
            self._call(
                REPLY,
                {
                    "note_id": f"https://genosai.dev/workspace/notes/my/{self.note.note_id}",
                    "thread_id": THREAD,
                    "text": "x",
                },
            )
        )
        self.assertFalse(is_error, text)
        self.assertEqual(self.sent["documentName"], f"my-note:{self.note.note_id}")

    def test_the_marker_carries_no_key_name_prefix_or_id(self):
        from origin.models.common.api_key_models import ApiKey

        key = ApiKey.objects.get(user=self.user)
        self.ok(REPLY, text="hi")
        wire = json.dumps(self.sent)
        for private in (key.name, key.prefix, str(key.id)):
            self.assertNotIn(private, wire)
        self.assertEqual(self.sent["metadata"], {"via": {"kind": "api_key"}})

    def test_a_connected_apps_marker_is_its_kind_and_family_only(self):
        oauth = {
            "kind": "oauth",
            "connection_id": "11111111-2222-3333-4444-555555555555",
            "client_name": "my-laptop-claude",
            "app": "claude",
        }
        with mock.patch.object(_common, "get_current_source", return_value=oauth):
            self.ok(REPLY, text="hi")
        self.assertEqual(self.sent["metadata"], {"via": {"kind": "oauth", "app": "claude"}})
        wire = json.dumps(self.sent)
        self.assertNotIn("my-laptop-claude", wire)
        self.assertNotIn("11111111-2222", wire)

    def test_an_unknown_source_or_kind_sends_no_marker(self):
        for source in (None, {}, {"kind": "something_new", "x": 1}, "api_key"):
            with (
                self.subTest(source=source),
                mock.patch.object(_common, "get_current_source", return_value=source),
            ):
                self.ok(REPLY, text="hi")
                self.assertNotIn("metadata", self.sent)

    def test_the_unusual_app_name_is_dropped_not_passed_on(self):
        oauth = {"kind": "oauth", "client_name": "n", "app": "Not A Family!"}
        with mock.patch.object(_common, "get_current_source", return_value=oauth):
            self.ok(REPLY, text="hi")
        self.assertEqual(self.sent["metadata"], {"via": {"kind": "oauth"}})

    def test_a_reply_to_a_resolved_thread_says_it_is_still_resolved(self):
        self.answer({"ok": True, "commentId": "c", "commentCount": 3, "threadResolved": True})
        text, _ = self.run_tool(REPLY, text="late")
        self.assertIn("still resolved", text)

    def test_the_reply_is_checked_before_collab_is_asked(self):
        for bad in (
            {"text": ""},
            {"text": "   \n "},
            {"text": "x" * 4001},
            {"text": "\n".join("line" for _ in range(51))},
            {"text": "ok", "thread_id": ""},
            {"text": "ok", "thread_id": "has space"},
            {"text": "ok", "thread_id": "x" * 65},
            {"text": "ok", "thread_id": "../../etc"},
        ):
            with self.subTest(bad=bad):
                self.refused(REPLY, **bad)
        self.post.assert_not_called()

    def test_no_text_is_refused(self):
        text, is_error = self._tool_text(self._call(REPLY, self.args()))
        self.assertTrue(is_error)
        self.post.assert_not_called()


class AccessTests(NoteThreadWriteBase):
    """Who may reply: whoever may read the note, as `get_note` decides. In the editor whoever can
    open a note can comment on it, viewers included (genos-collab README, "Viewers can still
    comment"), so there is no reader who can read but not comment; the empty case is stated here."""

    def test_an_editor_and_a_viewer_may_reply(self):
        for role in (EDITOR_ROLE, VIEWER_ROLE):
            with self.subTest(role=role):
                NotePermissionMaster.objects.filter(user=self.user2).delete()
                self._grant(self.user2, role)
                self._key(user=self.user2)
                self.post.reset_mock()
                self.ok(REPLY, text="a viewer's reply")
                self.assertEqual(self.sent["userId"], str(self.user2.id))

    def test_a_member_with_no_role_on_the_note_is_refused_and_collab_is_never_asked(self):
        self._key(user=self.user2)
        text = self.refused(REPLY, text="let me in")
        self.assertIn("not found", text)
        self.post.assert_not_called()

    def test_a_note_in_another_team_is_refused_and_collab_is_never_asked(self):
        other = TeamMaster.objects.create(team_name="Rivals", owner=self.user2)
        foreign = PersonalNoteMaster.objects.create(
            team=other, owner=self.user2, title="Theirs", body=[]
        )
        NotePermissionMaster.objects.create(
            team=other, user=self.user, note_id=foreign.note_id, note_type=1, role_id=EDITOR_ROLE
        )
        self._key()
        for name, extra in ((REPLY, {"text": "x"}), (RESOLVE, {})):
            text, is_error = self._tool_text(
                self._call(
                    name,
                    {
                        "note_type": "personal",
                        "note_id": foreign.note_id,
                        "thread_id": THREAD,
                        **extra,
                    },
                )
            )
            self.assertTrue(is_error, name)
            self.assertNotIn("Theirs", text)
        self.post.assert_not_called()

    def test_a_missing_note_reads_like_one_the_caller_cannot_open(self):
        self._key(user=self.user2)
        missing, _ = self._tool_text(
            self._call(REPLY, {**self.args(text="x"), "note_id": self.note.note_id + 999})
        )
        hidden = self.refused(REPLY, text="x")
        self.assertEqual(
            missing.replace(str(self.note.note_id + 999), "N"),
            hidden.replace(str(self.note.note_id), "N"),
        )

    def test_a_read_only_key_cannot_call_either_and_does_not_see_them(self):
        self._key(scope=SCOPE_READ)
        for name in (REPLY, RESOLVE):
            text = self.refused(name, text="x")
            self.assertIn("read-only", text)
        listed = {t["name"] for t in self._result(self._rpc("tools/list"))["tools"]}
        self.assertFalse({REPLY, RESOLVE} & listed)
        self.post.assert_not_called()

    def test_a_write_key_sees_them_and_needs_no_destructive_opt_in(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=False)
        tools = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        self.assertIn(REPLY, tools)
        self.assertIn(RESOLVE, tools)
        self.answer({"ok": True, "resolved": True, "alreadyResolved": False, "resolvedBy": "u"})
        self.ok(RESOLVE)  # no opt-in asked for


class ResolveTests(NoteThreadWriteBase):
    def resolved(self, **over):
        return {"ok": True, "resolved": True, "alreadyResolved": False, "resolvedBy": "u", **over}

    def setUp(self):
        super().setUp()
        self.answer(self.resolved())

    def test_the_owner_is_told_to_collab_as_may_resolve_any(self):
        self._key()
        out = self.ok(RESOLVE)
        self.assertEqual(self.post.call_args.args[0], f"{COLLAB}/internal/documents/resolve-thread")
        self.assertEqual(
            self.sent,
            {
                "documentName": f"my-note:{self.note.note_id}",
                "threadId": THREAD,
                "userId": str(self.user.id),
                "mayResolveAny": True,
            },
        )
        self.assertIs(out["resolved"], True)
        self.assertIs(out["already_resolved"], False)

    def test_an_editor_and_a_viewer_are_not_the_owner(self):
        for role in (EDITOR_ROLE, VIEWER_ROLE):
            with self.subTest(role=role):
                NotePermissionMaster.objects.filter(user=self.user2).delete()
                self._grant(self.user2, role)
                self._key(user=self.user2)
                self.ok(RESOLVE)
                self.assertIs(self.sent["mayResolveAny"], False)
                self.assertEqual(self.sent["userId"], str(self.user2.id))

    def test_whoever_holds_the_owner_role_is_the_owner_not_only_the_creator(self):
        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self._grant(self.user2, OWNER_ROLE)
        self._key(user=self.user2)
        self.ok(RESOLVE)
        self.assertIs(self.sent["mayResolveAny"], True)

    def test_a_personal_notes_author_without_a_role_row_is_its_owner(self):
        legacy = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Old", body=[]
        )
        self._key()
        text, is_error = self._tool_text(
            self._call(
                RESOLVE,
                {"note_type": "personal", "note_id": legacy.note_id, "thread_id": THREAD},
            )
        )
        self.assertFalse(is_error, text)
        self.assertIs(self.sent["mayResolveAny"], True)

    def test_collab_refusing_a_non_author_reads_clearly_and_counts_as_a_refusal(self):
        from origin.search_engine.agent.tools.base import ToolForbidden

        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self._grant(self.user2, EDITOR_ROLE)
        self._key(user=self.user2)
        self.answer({"error": "only the thread's author or the note's owner can resolve it"}, 403)
        text = self.refused(RESOLVE)
        self.assertIn("author or the note's owner", text)
        self.assertIn(REPLY, text, "it points to what they can do")
        self.assertTrue(issubclass(ToolForbidden, Exception))

    def test_resolving_twice_is_reported_not_failed(self):
        self._key()
        self.answer(self.resolved(alreadyResolved=True, resolvedBy="someone"))
        out = self.ok(RESOLVE)
        self.assertIs(out["already_resolved"], True)
        self.assertEqual(out["resolved_by"], "someone")

    def test_a_deleted_or_unknown_thread_says_so(self):
        self._key()
        self.answer({"error": "thread not found"}, 404)
        text = self.refused(RESOLVE)
        self.assertIn(THREAD, text)
        self.assertIn("not found", text)
        self.assertIn("get_note", text)

    def test_the_thread_id_is_checked_before_collab_is_asked(self):
        self._key()
        self.refused(RESOLVE, thread_id="no spaces")
        self.post.assert_not_called()

    def test_resolve_takes_no_text(self):
        self._key()
        text, is_error = self._tool_text(self._call(RESOLVE, self.args(text="x")))
        self.assertTrue(is_error)
        self.assertIn("text", text)
        self.post.assert_not_called()


class CollabFailsSafeTests(NoteThreadWriteBase):
    """Merge order is collab first. If the api tools run before collab has the endpoints, or
    collab is down, the caller gets a clear error and nothing is claimed as done."""

    def setUp(self):
        super().setUp()
        self._key()

    def both(self):
        return ((REPLY, {"text": "x"}), (RESOLVE, {}))

    def test_an_older_collab_that_answers_ok_to_any_path_is_not_taken_for_success(self):
        # Hocuspocus answers an unknown path "200 OK", plain text.
        self.post.return_value = mock.Mock(
            status_code=200, json=mock.Mock(side_effect=ValueError("not json"))
        )
        for name, extra in self.both():
            text = self.refused(name, **extra)
            self.assertIn("not available on this server yet", text)
            self.assertIn("Nothing was changed", text)

    def test_a_200_without_ok_true_or_the_expected_key_is_not_success(self):
        for body in ({"ok": False}, {"ok": True}, {"commentId": "c"}, [], "OK", None):
            with self.subTest(body=body):
                self.answer(body)
                self.assertIn("not available on this server yet", self.refused(REPLY, text="x"))
        self.answer({"ok": True, "commentId": "c"})
        self.assertIn("not available", self.refused(RESOLVE))

    def test_a_404_or_405_without_collabs_own_error_is_the_same_message(self):
        for status in (404, 405):
            self.post.return_value = mock.Mock(
                status_code=status, json=mock.Mock(side_effect=ValueError("html"))
            )
            self.assertIn("not available on this server yet", self.refused(REPLY, text="x"))

    def test_collab_down_or_slow_or_erroring_says_nothing_was_changed(self):
        for failure in (requests.ConnectTimeout("t"),):
            self.post.side_effect = failure
            for name, extra in self.both():
                text = self.refused(name, **extra)
                self.assertIn("could not be reached", text)
                self.assertIn("Nothing was changed", text)
        self.post.side_effect = None
        for status in (500, 502, 503, 401):
            self.answer({"error": "x"}, status)
            for name, extra in self.both():
                self.assertIn("could not be reached", self.refused(name, **extra), status)

    def test_a_lost_answer_to_a_reply_is_not_retried_and_says_it_may_have_landed(self):
        self.post.side_effect = requests.ReadTimeout("slow")
        text = self.refused(REPLY, text="x")
        self.assertEqual(self.post.call_count, 1, "a second POST would be a second reply")
        self.assertIn("may or may not have been added", text)
        self.assertIn("get_note", text)

    def test_a_lost_answer_to_a_resolve_says_to_try_again(self):
        self.post.side_effect = requests.ReadTimeout("slow")
        self.assertIn("try again", self.refused(RESOLVE))
        self.assertEqual(self.post.call_count, 1)

    def test_a_rejected_field_is_passed_on(self):
        self.answer({"error": "text is empty"}, 400)
        self.assertIn("text is empty", self.refused(REPLY, text="x"))

    @override_settings(COLLAB_INTERNAL_URL="")
    def test_collab_not_configured_here(self):
        for name, extra in self.both():
            self.assertIn("could not be reached", self.refused(name, **extra))
        self.post.assert_not_called()

    @override_settings(COLLAB_INTERNAL_SECRET="")
    def test_no_secret_means_no_call(self):
        self.refused(REPLY, text="x")
        self.post.assert_not_called()


class McpOnlyTests(NoteThreadWriteBase):
    def test_neither_tool_is_in_the_in_app_registry_or_its_declarations(self):
        for name in (REPLY, RESOLVE):
            self.assertNotIn(name, REGISTRY)
            self.assertIn(name, MCP_ONLY_REGISTRY)
            self.assertTrue(MCP_ONLY_REGISTRY[name].mcp_only)
        declared = json.dumps(_build_tool_declarations(), default=str)
        for name in (REPLY, RESOLVE):
            self.assertNotIn(name, declared)

    def test_they_are_writes_with_the_right_hints(self):
        for name in (REPLY, RESOLVE):
            tool = mcp_tools.BY_NAME[name]
            self.assertTrue(tool.write)
            self.assertFalse(tool.destructive, "no delete: the destructive opt-in is not asked for")
        self.assertFalse(mcp_tools.annotations(mcp_tools.BY_NAME[REPLY])["destructiveHint"])
        self.assertTrue(mcp_tools.annotations(mcp_tools.BY_NAME[RESOLVE])["destructiveHint"])
        self.assertIn(REPLY, mcp_tools.ADDITIVE_WRITE_TOOLS)
        self.assertIn(RESOLVE, mcp_tools.OVERWRITE_TOOLS)

    def test_the_published_schemas(self):
        self._key()
        tools = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        reply = tools[REPLY]["inputSchema"]
        self.assertEqual(sorted(reply["properties"]), ["note_id", "note_type", "text", "thread_id"])
        self.assertEqual(reply["required"], ["note_type", "note_id", "thread_id", "text"])
        self.assertEqual(reply["properties"]["thread_id"]["type"], "string")
        self.assertEqual(reply["properties"]["text"]["type"], "string")
        self.assertEqual(reply["properties"]["note_id"]["type"], "string")
        resolve = tools[RESOLVE]["inputSchema"]
        self.assertEqual(sorted(resolve["properties"]), ["note_id", "note_type", "thread_id"])
        self.assertEqual(resolve["required"], ["note_type", "note_id", "thread_id"])
        for name in (REPLY, RESOLVE):
            self.assertLess(len(tools[name]["description"]), 700, "short, per the MCP rules")
        self.assertIn("reply", tools[REPLY]["description"].lower())
        self.assertIn("Returns `comment_id`", tools[REPLY]["description"])
        self.assertIn("author or the note's owner", tools[RESOLVE]["description"])
        self.assertIn("Returns `resolved`", tools[RESOLVE]["description"])
        self.assertIn(REPLY, tools["get_note"]["description"])
        self.assertIn(RESOLVE, tools["get_note"]["description"])
