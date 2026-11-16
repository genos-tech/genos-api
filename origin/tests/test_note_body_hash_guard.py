"""A whole-body note write is refused when the note changed since it was read (MIP-1647).

`update_note.content_text` replaces the whole body. A person types while an agent works, so an
agent that read a note and then rewrote it put the old text back over theirs. Tasks have had the
guard since MIP-824 (`body_hash` / `base_body_hash`); notes did not. Over MCP, `get_note` now
returns `body_hash` and `update_note` takes `base_body_hash`.

What is pinned:
- the hash follows the stored body (and its block ids) and ignores the title;
- it is on `get_note` for every note family, and only over MCP;
- a matching hash writes, and the result carries the NEW hash so the next edit can chain;
- a stale hash refuses the WHOLE call: body, title, versions and the live editor are untouched;
- no hash means no check (today's behaviour); a malformed one is a clear error;
- the in-app agent's `fetch_note` / `update_note` declarations and output are unchanged, and the
  argument sent without the MCP surface is ignored.
"""

from __future__ import annotations

import json
import os
from unittest import mock

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.note_body_hash import note_body_hash
from origin.services.collab_push import PushOutcome
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp import result_limits
from origin.views.public.mcp import tools as mcp_tools

PUSH = "origin.search_engine.agent.tools.update_note.push_note_body_now"


def para(text: str, block_id: str = "b1") -> dict:
    return {
        "id": block_id,
        "type": "paragraph",
        "props": {},
        "content": [{"type": "text", "text": text, "styles": {}}],
        "children": [],
    }


class HashTests(McpBase):
    def test_it_follows_the_stored_blocks(self):
        a, b = [para("one")], [para("two")]
        self.assertEqual(note_body_hash(a), note_body_hash([para("one")]))
        self.assertNotEqual(note_body_hash(a), note_body_hash(b))

    def test_a_block_id_is_part_of_it(self):
        self.assertNotEqual(note_body_hash([para("x", "b1")]), note_body_hash([para("x", "b2")]))

    def test_an_empty_body_hashes_the_same_whether_none_or_empty(self):
        self.assertEqual(note_body_hash(None), note_body_hash([]))

    def test_it_is_sixteen_hex_chars(self):
        h = note_body_hash([para("x")])
        self.assertRegex(h, r"^[0-9a-f]{16}$")


class GuardBase(McpBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)
        self.push = mock.patch(PUSH, return_value=PushOutcome.SENT).start()
        self.addCleanup(mock.patch.stopall)
        self.notes = {}
        self.notes["personal"] = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan", body=[para("first")]
        )
        self.notes["task"] = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=self.project,
            task=self.task,
            title="Task note",
            body=[para("first")],
        )

    def note(self, kind="personal"):
        return self.notes[kind]

    def fresh(self, kind="personal"):
        n = self.note(kind)
        return type(n).objects.get(note_id=n.note_id)

    def get(self, kind="personal"):
        text, err = self._tool_text(
            self._call("get_note", {"note_type": kind, "note_id": self.note(kind).note_id})
        )
        self.assertFalse(err, text)
        return json.loads(text[text.index("{") :])

    def update(self, kind="personal", **extra):
        args = {"note_type": kind, "note_id": self.note(kind).note_id, **extra}
        text, err = self._tool_text(self._call("update_note", args))
        return text, err

    def ok(self, kind="personal", **extra):
        text, err = self.update(kind, **extra)
        self.assertFalse(err, text)
        return json.loads(text[text.index("{") :])


class GetNoteTests(GuardBase):
    def test_get_note_returns_the_hash_of_the_stored_body_for_each_family(self):
        for kind in ("personal", "task"):
            out = self.get(kind)
            self.assertEqual(out["body_hash"], note_body_hash(self.fresh(kind).body), kind)

    def test_the_hash_moves_when_the_body_moves_and_not_when_the_title_does(self):
        before = self.get()["body_hash"]
        PersonalNoteMaster.objects.filter(pk=self.note().pk).update(title="Renamed")
        self.assertEqual(self.get()["body_hash"], before)
        PersonalNoteMaster.objects.filter(pk=self.note().pk).update(body=[para("typed since")])
        self.assertNotEqual(self.get()["body_hash"], before)

    def test_the_in_app_agent_does_not_get_it(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        out = REGISTRY["fetch_note"].run(
            {"note_type": "personal", "note_id": self.note().note_id}, ctx
        )
        self.assertNotIn("body_hash", out)


class UpdateNoteGuardTests(GuardBase):
    def test_a_matching_hash_writes_and_returns_the_new_hash(self):
        base = self.get()["body_hash"]
        out = self.ok(content_text="second paragraph", base_body_hash=base)
        self.assertEqual(out["changed_fields"], ["body"])
        self.assertEqual(out["body_hash"], note_body_hash(self.fresh().body))
        self.assertNotEqual(out["body_hash"], base)
        self.push.assert_called_once()

    def test_the_new_hash_chains_into_the_next_edit_without_a_re_read(self):
        out = self.ok(content_text="one", base_body_hash=self.get()["body_hash"])
        again = self.ok(content_text="two", base_body_hash=out["body_hash"])
        self.assertEqual(again["changed_fields"], ["body"])

    def test_a_stale_hash_refuses_the_whole_call_and_writes_nothing(self):
        base = self.get()["body_hash"]
        PersonalNoteMaster.objects.filter(pk=self.note().pk).update(body=[para("typed since")])
        versions_before = NoteVersionMaster.objects.count()

        text, err = self.update(
            content_text="agent rewrite", title="New title", base_body_hash=base
        )

        self.assertTrue(err)
        self.assertIn("changed since you read it", text)
        self.assertIn("Nothing was changed", text)
        row = self.fresh()
        self.assertEqual(row.body, [para("typed since")])  # the person's text survives
        self.assertEqual(row.title, "Plan")  # the title in the same call is not applied either
        self.assertEqual(NoteVersionMaster.objects.count(), versions_before)
        self.push.assert_not_called()

    def test_a_stale_hash_also_refuses_a_title_only_edit(self):
        # Same as `update_task`: the check is for the call, not for the fields it names.
        base = self.get()["body_hash"]
        PersonalNoteMaster.objects.filter(pk=self.note().pk).update(body=[para("typed since")])
        text, err = self.update(title="New title", base_body_hash=base)
        self.assertTrue(err, text)
        self.assertEqual(self.fresh().title, "Plan")

    def test_each_note_family_is_guarded(self):
        for kind in ("personal", "task"):
            base = self.get(kind)["body_hash"]
            type(self.note(kind)).objects.filter(pk=self.note(kind).pk).update(
                body=[para("typed since")]
            )
            text, err = self.update(kind, content_text="agent rewrite", base_body_hash=base)
            self.assertTrue(err, f"{kind}: {text}")
            self.assertEqual(self.fresh(kind).body, [para("typed since")], kind)

    def test_without_a_hash_nothing_is_checked(self):
        PersonalNoteMaster.objects.filter(pk=self.note().pk).update(body=[para("typed since")])
        out = self.ok(content_text="agent rewrite")
        self.assertEqual(out["changed_fields"], ["body"])

    def test_a_write_without_a_hash_still_returns_the_new_hash(self):
        out = self.ok(content_text="agent rewrite")
        self.assertEqual(out["body_hash"], note_body_hash(self.fresh().body))

    def test_a_no_op_edit_with_a_hash_returns_it_so_the_caller_can_keep_going(self):
        base = self.get()["body_hash"]
        out = self.ok(title="Plan", base_body_hash=base)
        self.assertEqual(out["changed_fields"], [])
        self.assertEqual(out["body_hash"], base)

    def test_a_malformed_hash_is_a_clear_error_and_writes_nothing(self):
        for bad in ("", "   ", 12345, ["a"]):
            text, err = self.update(content_text="agent rewrite", base_body_hash=bad)
            self.assertTrue(err, bad)
            self.assertEqual(self.fresh().body, [para("first")], bad)


class InAppAgentUnchangedTests(GuardBase):
    def test_the_registry_declaration_does_not_mention_the_argument(self):
        props = REGISTRY["update_note"].parameters_schema["properties"]
        self.assertNotIn("base_body_hash", props)
        # What the model is sent, for `update_note` only (`update_task` declares its own).
        (declaration,) = [d for d in _build_tool_declarations() if d.name == "update_note"]
        self.assertNotIn("base_body_hash", json.dumps(declaration.parameters_schema))
        self.assertNotIn("base_body_hash", declaration.description)

    def test_the_mcp_schema_publishes_it(self):
        tool = next(t for t in mcp_tools.CATALOG if t.name == "update_note")
        self.assertIn("base_body_hash", mcp_tools.input_schema(tool)["properties"])

    def test_off_the_mcp_surface_the_argument_is_ignored_not_enforced(self):
        # The in-app agent never declares it; a value that reaches the registry tool from any
        # other surface must not start refusing edits.
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        out = REGISTRY["update_note"].run(
            {
                "note_type": "personal",
                "note_id": self.note().note_id,
                "content_text": "in-app edit",
                "base_body_hash": "0000000000000000",
            },
            ctx,
        )
        self.assertEqual(out["changed_fields"], ["body"])
        self.assertNotIn("body_hash", out)

    def test_a_malformed_hash_is_not_checked_off_the_mcp_surface_either(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        try:
            REGISTRY["update_note"].run(
                {
                    "note_type": "personal",
                    "note_id": self.note().note_id,
                    "content_text": "in-app edit",
                    "base_body_hash": 12345,
                },
                ctx,
            )
        except ToolError as exc:  # pragma: no cover
            self.fail(f"the in-app surface must ignore base_body_hash, got: {exc}")


class PartialReadTests(GuardBase):
    """With the paging switch on, only a COMPLETE read carries a hash (MIP-1367 already lists
    `get_note` for this): a reader who saw part of a body must not be able to vouch for all of it."""

    def read(self, **extra):
        with mock.patch.dict(os.environ, {result_limits.FLAG_ENV: "1"}):
            text, err = self._tool_text(
                self._call(
                    "get_note",
                    {"note_type": "personal", "note_id": self.note().note_id, **extra},
                )
            )
        self.assertFalse(err, text)
        return json.loads(text[text.index("{") :])

    def test_a_whole_read_carries_the_hash(self):
        out = self.read()
        self.assertEqual(out["body_hash"], note_body_hash(self.fresh().body))

    def test_a_partial_read_does_not(self):
        long_body = [para("word " * 400, f"b{i}") for i in range(60)]
        PersonalNoteMaster.objects.filter(pk=self.note().pk).update(body=long_body)
        out = self.read(max_chars=1000)
        self.assertIn("truncated", out)
        self.assertNotIn("body_hash", out)
