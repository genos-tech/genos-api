"""`edit_note_block` and `get_note(include_blocks)` over MCP (MIP-1646).

`update_note` can only replace a note's whole body. These change ONE block, by id. The edit is made by
genos-collab on the note's LIVE doc (a person may be typing into it); collab is stubbed here at
`requests.post`. A note nobody ever opened has no live doc, so its stored body is edited here instead.
What is pinned:

- reading: `include_blocks` lists the blocks from the live doc, from the stored body when there is no
  doc, `null` with a note when neither can be read, and nothing at all unless asked;
- editing through collab: exactly what is sent, what is saved (collab's answer, a version snapshot),
  what the caller gets back (new ids, the note's new `body_hash`), and that every collab "no" reads
  clearly and saves nothing;
- editing a never-opened note: replace keeps the id, insert and delete work, nested blocks too, the
  hash guard still works, and ids a caller supplies never reach the note;
- access: whoever may not edit the note may not edit a block of it;
- it is MCP-only: never in the in-app registry or its declarations.
"""

from __future__ import annotations

import json
from unittest import mock

import jwt
import requests
from django.test import override_settings

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.search_engine.agent.tools.note_body_hash import note_body_hash
from origin.services import collab_push, mcp_notice
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp.note_blocks import _stored

COLLAB = "http://collab.internal:8891"
COLLAB_SECRET = "test-only-collab-internal-secret"
EDIT = "edit_note_block"
OWNER_ROLE, EDITOR_ROLE, VIEWER_ROLE = 1, 2, 3


def para(text: str, block_id: str | None = None, children=None) -> dict:
    block = {
        "type": "paragraph",
        "props": {},
        "content": [{"type": "text", "text": text, "styles": {}}],
        "children": children or [],
    }
    if block_id:
        block["id"] = block_id
    return block


def bullet(text: str, block_id: str, children=None) -> dict:
    return {**para(text, block_id, children), "type": "bulletListItem"}


def body() -> list[dict]:
    return [
        {**para("Intro", "b-intro"), "type": "heading", "props": {"level": 2}},
        para("first paragraph", "b-para"),
        bullet("parent item", "b-list", [bullet("child item", "b-child")]),
        para("last paragraph", "b-last"),
    ]


@override_settings(COLLAB_INTERNAL_URL=COLLAB, COLLAB_INTERNAL_SECRET=COLLAB_SECRET)
class BlockBase(McpBase):
    def setUp(self):
        super().setUp()
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan", body=body()
        )
        self._grant(self.user, OWNER_ROLE)
        # The opt-in a delete needs (see `DeletingABlockNeedsTheOptInTests`, which uses a key without it).
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        patcher = mock.patch.object(collab_push.requests, "post")
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    # -- collab, stubbed --
    def answer(self, body, status=200):
        self.post.return_value = mock.Mock(status_code=status, json=mock.Mock(return_value=body))

    def no_doc(self):
        self.answer({"error": "document not found"}, 404)

    def collab_ok(self, blocks=None, new_ids=()):
        self.answer(
            {
                "ok": True,
                "op": "x",
                "blockId": "b",
                "newBlockIds": list(new_ids),
                "blocks": blocks if blocks is not None else body(),
            }
        )

    def _grant(self, user, role):
        NotePermissionMaster.objects.create(
            team=self.team, user=user, note_id=self.note.note_id, note_type=1, role_id=role
        )

    # -- calling --
    def args(self, **extra):
        return {"note_type": "personal", "note_id": self.note.note_id, **extra}

    def call(self, name, **extra):
        return self._tool_text(self._call(name, self.args(**extra)))

    def ok(self, name, **extra):
        text, is_error = self.call(name, **extra)
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def refused(self, name, **extra):
        text, is_error = self.call(name, **extra)
        self.assertTrue(is_error, text)
        return text

    def fresh(self):
        return PersonalNoteMaster.objects.get(note_id=self.note.note_id)

    @property
    def sent(self):
        return self.post.call_args.kwargs["json"]


class ReadBlocksTests(BlockBase):
    def asked_for_blocks(self):
        return [c for c in self.post.call_args_list if c.args[0].endswith("/read-blocks")]

    def test_nothing_is_added_unless_asked_for(self):
        self.answer({"threads": []})  # `get_note` always asks collab for the comment threads
        out = self.ok("get_note")
        self.assertNotIn("blocks", out)
        self.assertEqual(self.asked_for_blocks(), [], "the block read is opt-in")

    def test_blocks_come_from_the_live_doc_when_there_is_one(self):
        self.answer(
            {
                "exists": True,
                "blocks": [
                    {
                        "id": "b-intro",
                        "type": "heading",
                        "depth": 0,
                        "parentId": None,
                        "text": "Intro typed just now",
                        "hash": "a" * 16,
                        "hasChildren": False,
                    },
                    {
                        "id": "b-child",
                        "type": "bulletListItem",
                        "depth": 1,
                        "parentId": "b-list",
                        "text": "child",
                        "hash": "b" * 16,
                        "hasChildren": False,
                    },
                ],
            }
        )
        out = self.ok("get_note", include_blocks=True)
        self.assertEqual(self.post.call_args.args[0], f"{COLLAB}/internal/documents/read-blocks")
        self.assertEqual(self.sent, {"documentName": f"my-note:{self.note.note_id}"})
        self.assertEqual(
            out["blocks"],
            [
                {
                    "block_id": "b-intro",
                    "type": "heading",
                    "depth": 0,
                    "parent_block_id": None,
                    "text": "Intro typed just now",
                    "block_hash": "a" * 16,
                    "has_children": False,
                },
                {
                    "block_id": "b-child",
                    "type": "bulletListItem",
                    "depth": 1,
                    "parent_block_id": "b-list",
                    "text": "child",
                    "block_hash": "b" * 16,
                    "has_children": False,
                },
            ],
        )

    def test_a_note_nobody_opened_is_read_from_its_stored_body(self):
        self.answer({"exists": False, "blocks": []})
        out = self.ok("get_note", include_blocks=True)
        got = [
            (b["block_id"], b["type"], b["depth"], b["parent_block_id"], b["text"])
            for b in out["blocks"]
        ]
        self.assertEqual(
            got,
            [
                ("b-intro", "heading", 0, None, "Intro"),
                ("b-para", "paragraph", 0, None, "first paragraph"),
                ("b-list", "bulletListItem", 0, None, "parent item"),
                ("b-child", "bulletListItem", 1, "b-list", "child item"),
                ("b-last", "paragraph", 0, None, "last paragraph"),
            ],
        )
        self.assertTrue(out["blocks"][2]["has_children"])
        for b in out["blocks"]:
            self.assertRegex(b["block_hash"], r"^[0-9a-f]{16}$")

    def test_unreadable_is_null_with_a_note_never_an_empty_list(self):
        for failure in (requests.ConnectTimeout("t"), requests.ReadTimeout("slow")):
            self.post.side_effect = failure
            out = self.ok("get_note", include_blocks=True)
            self.assertIsNone(out["blocks"])
            self.assertIn("could not be read", out["blocks_note"])
        self.post.side_effect = None
        # An older collab answers any unknown path "200 OK", plain text.
        self.post.return_value = mock.Mock(
            status_code=200, json=mock.Mock(side_effect=ValueError("not json"))
        )
        self.assertIsNone(self.ok("get_note", include_blocks=True)["blocks"])
        for status in (404, 500):
            self.answer({"error": "x"}, status)
            self.assertIsNone(self.ok("get_note", include_blocks=True)["blocks"], status)

    def test_a_very_long_note_lists_the_first_blocks_and_says_so(self):
        many = [
            {
                "id": f"b{i}",
                "type": "paragraph",
                "depth": 0,
                "parentId": None,
                "text": "x",
                "hash": "c" * 16,
                "hasChildren": False,
            }
            for i in range(520)
        ]
        self.answer({"exists": True, "blocks": many})
        out = self.ok("get_note", include_blocks=True)
        self.assertEqual(len(out["blocks"]), 500)
        self.assertEqual(out["blocks_truncated"], {"shown": 500, "total": 520})

    def test_a_note_the_caller_cannot_read_is_not_found_and_blocks_are_never_asked_for(self):
        # A team member with no access to someone's private note.
        self._key(user=self.user2, allow_destructive=True)
        text, is_error = self.call("get_note", include_blocks=True)
        self.assertTrue(is_error)
        self.assertIn("not found", text)
        self.assertEqual(self.asked_for_blocks(), [])


class StoredHelperTests(BlockBase):
    def test_replace_keeps_the_id_and_does_not_touch_the_input(self):
        original = body()
        snapshot = json.dumps(original, sort_keys=True)
        new_body, new_ids = _stored.splice(
            original, "b-para", "replace", [para("one"), para("two")]
        )
        self.assertEqual(json.dumps(original, sort_keys=True), snapshot, "the input is not changed")
        self.assertEqual(
            [b["id"] for b in new_body][:4], ["b-intro", "b-para", new_body[2]["id"], "b-list"]
        )
        self.assertEqual(len(new_ids), 1)
        self.assertEqual(new_ids[0], new_body[2]["id"])

    def test_a_supplied_id_never_reaches_the_note(self):
        new_body, _ = _stored.splice(body(), "b-para", "insert_after", [para("x", "b-intro")])
        ids = [s["block_id"] for s in _stored.summarize(new_body)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_deleting_the_only_block_leaves_an_empty_paragraph(self):
        new_body, _ = _stored.splice([para("alone", "only")], "only", "delete", [])
        self.assertEqual(len(new_body), 1)
        self.assertEqual(new_body[0]["type"], "paragraph")
        self.assertEqual(_stored.summarize(new_body)[0]["text"], "")

    def test_a_nested_block_can_be_replaced_inside_its_parent(self):
        new_body, _ = _stored.splice(body(), "b-child", "replace", [bullet("rewritten", "ignored")])
        self.assertEqual(
            [s["text"] for s in _stored.summarize(new_body) if s["parent_block_id"] == "b-list"],
            ["rewritten"],
        )

    def test_an_unknown_block_is_none(self):
        self.assertIsNone(_stored.splice(body(), "nope", "delete", []))

    def test_the_hash_changes_with_the_blocks_content_and_children_only(self):
        a = _stored.find_block(body(), "b-list")
        changed = body()
        changed[2]["children"][0]["content"][0]["text"] = "child edited"
        b = _stored.find_block(changed, "b-list")
        self.assertNotEqual(_stored.block_hash(a), _stored.block_hash(b))
        self.assertEqual(
            _stored.block_hash(_stored.find_block(body(), "b-intro")),
            _stored.block_hash(_stored.find_block(changed, "b-intro")),
        )


class EditThroughCollabTests(BlockBase):
    def edited_body(self):
        new = body()
        new[1] = para("rewritten", "b-para")
        return new

    def test_collab_is_asked_for_exactly_this_edit_as_the_internal_service(self):
        self.collab_ok(self.edited_body())
        self.ok(
            EDIT,
            block_id="b-para",
            op="replace",
            content_text="rewritten",
            base_block_hash="d" * 16,
        )

        self.post.assert_called_once()
        self.assertEqual(self.post.call_args.args[0], f"{COLLAB}/internal/documents/edit-block")
        sent = self.sent
        self.assertEqual(sent["documentName"], f"my-note:{self.note.note_id}")
        self.assertEqual(
            (sent["blockId"], sent["op"], sent["baseBlockHash"]), ("b-para", "replace", "d" * 16)
        )
        self.assertEqual(sent["blocks"][0]["type"], "paragraph")
        self.assertEqual(sent["blocks"][0]["content"][0]["text"], "rewritten")
        self.assertNotIn("id", sent["blocks"][0], "ids are collab's to assign")
        self.assertFalse(self.post.call_args.kwargs["allow_redirects"])
        token = self.post.call_args.kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(
            token, COLLAB_SECRET, algorithms=["HS256"], audience="genos-collab-internal"
        )
        self.assertEqual(claims["aud"], "genos-collab-internal")

    def test_delete_sends_no_blocks_and_no_hash_when_none_was_given(self):
        self.collab_ok()
        self.ok(EDIT, block_id="b-para", op="delete")
        self.assertNotIn("blocks", self.sent)
        self.assertNotIn("baseBlockHash", self.sent)

    def test_collabs_answer_is_what_gets_saved_with_a_version_and_the_new_hash(self):
        after = self.edited_body()
        self.collab_ok(after, new_ids=["n1"])
        versions_before = NoteVersionMaster.objects.count()

        out = self.ok(EDIT, block_id="b-para", op="replace", content_text="rewritten")

        self.assertEqual(self.fresh().body, after)
        self.assertEqual(out["body_hash"], note_body_hash(after))
        self.assertEqual(out["new_block_ids"], ["n1"])
        self.assertEqual(out["live_editor"], "applied")
        self.assertEqual(
            (out["block_id"], out["op"], out["note_id"]), ("b-para", "replace", self.note.note_id)
        )
        self.assertEqual(out["changed_fields"], ["body"])
        self.assertGreater(NoteVersionMaster.objects.count(), versions_before)
        # Whatever was there before is in the history, so the edit can be walked back.
        self.assertTrue(any(v.body == body() for v in NoteVersionMaster.objects.all()))

    def test_the_title_is_never_touched(self):
        self.collab_ok(self.edited_body())
        self.ok(EDIT, block_id="b-para", op="replace", content_text="x")
        self.assertEqual(self.fresh().title, "Plan")

    def test_it_is_the_same_notice_as_update_note(self):
        notice = mcp_notice.build_notice(
            EDIT,
            self.args(block_id="b-para"),
            {"note_id": self.note.note_id, "note_type": "personal", "changed_fields": ["body"]},
        )
        self.assertIsNotNone(notice)
        self.assertEqual(notice.kind, "note")

    # -- bad arguments: nothing is sent --
    def test_bad_arguments_are_refused_before_collab_is_asked(self):
        cases = [
            {"block_id": "b-para", "op": "explode", "content_text": "x"},
            {"block_id": "b-para", "op": None},
            {"block_id": "no spaces", "op": "delete"},
            {"block_id": "", "op": "delete"},
            {"op": "delete"},
            {"block_id": "b-para", "op": "replace"},
            {"block_id": "b-para", "op": "replace", "content_text": "   "},
            {"block_id": "b-para", "op": "insert_after"},
            {"block_id": "b-para", "op": "delete", "content_text": "x"},
            {
                "block_id": "b-para",
                "op": "replace",
                "content_text": "x",
                "base_block_hash": "not-a-hash",
            },
            {"block_id": "b-para", "op": "replace", "content_text": "x", "base_block_hash": 12345},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                self.refused(EDIT, **extra)
        self.post.assert_not_called()
        self.assertEqual(self.fresh().body, body())

    # -- every collab "no" saves nothing --
    def test_each_collab_no_reads_clearly_and_saves_nothing(self):
        versions_before = NoteVersionMaster.objects.count()
        cases = [
            ({"error": "block not found"}, 404, "was not found"),
            ({"error": "the block changed since you read it"}, 409, "changed since you read it"),
            ({"error": "the blocks do not fit the editor's schema: x"}, 400, "rejected the edit"),
            ({"error": "boom"}, 500, "could not be reached"),
            ({"error": "x"}, 405, "not available on this server yet"),
        ]
        for body_, status, expected in cases:
            with self.subTest(status=status):
                self.answer(body_, status)
                text = self.refused(EDIT, block_id="b-para", op="replace", content_text="x")
                self.assertIn(expected, text)
                self.assertIn("Nothing was changed", text) if status != 400 else None
        self.assertEqual(self.fresh().body, body())
        self.assertEqual(NoteVersionMaster.objects.count(), versions_before)

    def test_a_stale_or_missing_block_says_how_to_read_again(self):
        for status, error in (
            (404, "block not found"),
            (409, "the block changed since you read it"),
        ):
            self.answer({"error": error}, status)
            text = self.refused(EDIT, block_id="b-para", op="delete")
            self.assertIn("include_blocks", text)
            self.assertIn("b-para", text)

    def test_an_older_collab_that_answers_ok_to_any_path_is_not_taken_for_success(self):
        self.post.return_value = mock.Mock(
            status_code=200, json=mock.Mock(side_effect=ValueError("not json"))
        )
        text = self.refused(EDIT, block_id="b-para", op="delete")
        self.assertIn("not available on this server yet", text)
        for answer in ({"ok": False}, {"ok": True}, {"ok": True, "blocks": "x"}, [], "OK", None):
            self.answer(answer)
            self.assertIn(
                "not available", self.refused(EDIT, block_id="b-para", op="delete"), str(answer)
            )
        self.assertEqual(self.fresh().body, body())

    def test_a_lost_answer_is_not_retried_and_says_it_may_have_landed(self):
        self.post.side_effect = requests.ReadTimeout("slow")
        text = self.refused(EDIT, block_id="b-para", op="insert_after", content_text="x")
        self.assertEqual(self.post.call_count, 1, "a second POST would be a second block")
        self.assertIn("may or may not have been made", text)
        self.assertEqual(self.fresh().body, body())

    def test_collab_unreachable_says_nothing_was_changed_and_does_not_edit_the_stored_body(self):
        # Collab down does NOT mean "no live doc": editing the stored copy would be overwritten by
        # (or overwrite) whatever an open editor holds.
        self.post.side_effect = requests.ConnectTimeout("t")
        text = self.refused(EDIT, block_id="b-para", op="delete")
        self.assertIn("could not be reached", text)
        self.assertEqual(self.fresh().body, body())

    @override_settings(COLLAB_INTERNAL_URL="")
    def test_collab_not_configured_here(self):
        self.assertIn("could not be reached", self.refused(EDIT, block_id="b-para", op="delete"))
        self.post.assert_not_called()

    def test_if_saving_fails_after_the_editor_was_changed_it_says_so(self):
        self.collab_ok(self.edited_body())
        with mock.patch.object(PersonalNoteMaster, "save", side_effect=RuntimeError("db")):
            text = self.refused(EDIT, block_id="b-para", op="replace", content_text="x")
        self.assertIn("changed in the editor", text)
        self.assertIn("autosave", text)


class DeletingABlockNeedsTheOptInTests(BlockBase):
    """`op: delete` removes content, so it is held to the same opt-in a delete is
    (`DELETING_ARGUMENTS`), while replace and insert are not."""

    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)  # a key that was NOT opted into deleting

    def test_a_write_key_cannot_delete_a_block(self):
        self.no_doc()
        text = self.refused(EDIT, block_id="b-para", op="delete")
        self.assertIn("op=delete", text)
        self.assertIn("deletes", text)
        self.assertIn("Settings", text, "the refusal must say how to fix it")
        self.assertEqual(self.fresh().body, body())
        self.post.assert_not_called()

    def test_the_same_key_may_replace_and_insert(self):
        self.no_doc()
        self.ok(EDIT, block_id="b-para", op="replace", content_text="x")
        self.ok(EDIT, block_id="b-intro", op="insert_after", content_text="y")

    def test_a_key_opted_into_deleting_may(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        self.no_doc()
        self.ok(EDIT, block_id="b-para", op="delete")
        self.assertEqual(
            [s["block_id"] for s in _stored.summarize(self.fresh().body)][:2], ["b-intro", "b-list"]
        )


class EditStoredBodyTests(BlockBase):
    """No live doc (collab answers 404 `document not found`): the stored body is the whole truth."""

    def setUp(self):
        super().setUp()
        self.no_doc()

    def texts(self):
        return [(s["block_id"], s["text"]) for s in _stored.summarize(self.fresh().body)]

    def test_replace_changes_only_that_block_and_keeps_its_id(self):
        out = self.ok(EDIT, block_id="b-para", op="replace", content_text="rewritten")
        self.assertEqual(out["live_editor"], "not_open")
        self.assertEqual(
            self.texts(),
            [
                ("b-intro", "Intro"),
                ("b-para", "rewritten"),
                ("b-list", "parent item"),
                ("b-child", "child item"),
                ("b-last", "last paragraph"),
            ],
        )
        self.assertEqual(out["new_block_ids"], [])
        self.assertEqual(out["body_hash"], note_body_hash(self.fresh().body))

    def test_untouched_blocks_are_byte_identical(self):
        self.ok(EDIT, block_id="b-para", op="replace", content_text="rewritten")
        after = self.fresh().body
        for i in (0, 2, 3):
            self.assertEqual(after[i], body()[i])

    def test_insert_after_adds_siblings_with_fresh_ids(self):
        out = self.ok(EDIT, block_id="b-intro", op="insert_after", content_text="one\n\ntwo")
        self.assertEqual(len(out["new_block_ids"]), 2)
        self.assertEqual(
            [t for _, t in self.texts()][:4], ["Intro", "one", "two", "first paragraph"]
        )
        ids = [i for i, _ in self.texts()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_delete_removes_the_block_and_its_children(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        self.ok(EDIT, block_id="b-list", op="delete")
        self.assertEqual([i for i, _ in self.texts()], ["b-intro", "b-para", "b-last"])

    def test_a_nested_block_is_edited_inside_its_parent(self):
        self.ok(EDIT, block_id="b-child", op="replace", content_text="- child rewritten")
        kids = [
            s["text"]
            for s in _stored.summarize(self.fresh().body)
            if s["parent_block_id"] == "b-list"
        ]
        self.assertEqual(kids, ["child rewritten"])

    def test_an_unknown_block_is_refused_and_nothing_changes(self):
        text = self.refused(EDIT, block_id="nope", op="delete")
        self.assertIn("was not found", text)
        self.assertEqual(self.fresh().body, body())

    def test_the_hash_guard_works_here_too(self):
        good = _stored.block_hash(_stored.find_block(body(), "b-para"))
        self.refused(
            EDIT, block_id="b-para", op="replace", content_text="x", base_block_hash="0" * 16
        )
        self.assertEqual(self.fresh().body, body())
        self.ok(EDIT, block_id="b-para", op="replace", content_text="x", base_block_hash=good)

    def test_a_block_hash_from_get_note_works_on_this_path(self):
        self.answer({"exists": False, "blocks": []})
        blocks = self.ok("get_note", include_blocks=True)["blocks"]
        h = next(b["block_hash"] for b in blocks if b["block_id"] == "b-para")
        self.no_doc()
        self.ok(EDIT, block_id="b-para", op="replace", content_text="x", base_block_hash=h)

    def test_a_version_is_kept_and_the_old_body_is_in_it(self):
        self.ok(EDIT, block_id="b-para", op="delete")
        self.assertTrue(any(v.body == body() for v in NoteVersionMaster.objects.all()))

    def test_a_note_that_was_emptied_gets_one_empty_paragraph(self):
        PersonalNoteMaster.objects.filter(pk=self.note.pk).update(body=[para("alone", "only")])
        self.ok(EDIT, block_id="only", op="delete")
        self.assertEqual(len(self.fresh().body), 1)


class AccessTests(BlockBase):
    def test_a_viewer_may_read_the_blocks_but_not_edit_one(self):
        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self._grant(self.user2, VIEWER_ROLE)
        self._key(user=self.user2, allow_destructive=True)
        self.answer({"exists": False, "blocks": []})
        self.assertIn("blocks", self.ok("get_note", include_blocks=True))
        self.post.reset_mock()
        text = self.refused(EDIT, block_id="b-para", op="delete")
        self.assertIn("Not authorized", text)
        self.post.assert_not_called()
        self.assertEqual(self.fresh().body, body())

    def test_an_editor_may_edit_a_block(self):
        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self._grant(self.user2, EDITOR_ROLE)
        self._key(user=self.user2, allow_destructive=True)
        self.collab_ok()
        self.ok(EDIT, block_id="b-para", op="delete")
        self.post.assert_called_once()

    def test_a_team_member_with_no_access_is_told_the_note_does_not_exist(self):
        self._key(user=self.user2, allow_destructive=True)
        text = self.refused(EDIT, block_id="b-para", op="delete")
        self.assertIn("not found", text)
        self.post.assert_not_called()

    def test_another_teams_note_is_not_found(self):
        from origin.models.common.team_models import TeamMaster

        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        foreign = PersonalNoteMaster.objects.create(
            team=other, owner=self.user2, title="x", body=body()
        )
        text, is_error = self._tool_text(
            self._call(
                EDIT,
                {
                    "note_type": "personal",
                    "note_id": foreign.note_id,
                    "block_id": "b-para",
                    "op": "delete",
                },
            )
        )
        self.assertTrue(is_error)
        self.assertIn("not found", text)
        self.post.assert_not_called()

    def test_a_read_only_key_cannot_edit(self):
        self._key(scope=SCOPE_READ, allow_destructive=True)
        text, is_error = self.call(EDIT, block_id="b-para", op="delete")
        self.assertTrue(is_error, text)
        self.post.assert_not_called()
        self.assertEqual(self.fresh().body, body())

    def test_task_and_chat_notes_use_their_own_document_names(self):
        from origin.models.note.task_note_models import TaskNoteMaster

        task_note = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=self.project,
            task=self.task,
            title="T",
            body=body(),
        )
        self.collab_ok()
        text, is_error = self._tool_text(
            self._call(
                EDIT,
                {
                    "note_type": "task",
                    "note_id": task_note.note_id,
                    "block_id": "b-para",
                    "op": "delete",
                },
            )
        )
        self.assertFalse(is_error, text)
        self.assertEqual(self.sent["documentName"], f"task-note:{task_note.note_id}")


class McpOnlyTests(BlockBase):
    def test_it_is_in_the_mcp_registry_and_never_declared_to_the_in_app_agent(self):
        self.assertIn(EDIT, MCP_ONLY_REGISTRY)
        self.assertNotIn(EDIT, REGISTRY)
        self.assertNotIn(EDIT, {d.name for d in _build_tool_declarations()})

    def test_get_note_declaration_for_the_in_app_agent_does_not_mention_blocks(self):
        (declaration,) = [d for d in _build_tool_declarations() if d.name == "fetch_note"]
        self.assertNotIn("include_blocks", json.dumps(declaration.parameters_schema))
        self.assertNotIn("edit_note_block", declaration.description)
