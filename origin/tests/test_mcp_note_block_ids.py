"""A note created over MCP and never opened has block ids too (MIP-1672).

A body written from markdown (`create_note`, `update_note`) carries no block ids until the editor first
loads it, so a note nobody has opened listed `block_id: null` for every block, and `edit_note_block` (and the
`blocks_matching` of MIP-1666, which finds a block it could not then name) could not touch it. Such a block now
has an id DERIVED from its own content and from how many identical blocks came before it. What is pinned:

- the premise: a note made through `create_note` really has a body with no ids, so these tests cannot pass by
  accident;
- every block is listed with an id of the right shape, the same on every read, unique, and the children name
  their parent by it;
- the id follows the block, not its place: inserting or deleting other blocks does not change it, two
  identical paragraphs still get two ids, and an id the body already holds is never reused for another block;
- a block that HAS a stored id keeps it, and a live document's ids (collab answers) are used as they are;
- editing by a derived id works on the stored body (replace, insert after, delete), the replaced block keeps
  the id from then on, nothing else in the body is rewritten, and a stale `base_block_hash` is refused;
- the flow that failed: find a block of an MCP-made note by its text, then change just that block.
"""

from __future__ import annotations

import copy
import json

from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.views.public.mcp.note_blocks import _stored
from origin.views.public.mcp.resolve import BLOCK_ID

from .test_mcp_note_block_edit import EDIT, BlockBase, para

MARKDOWN = (
    "# Findings\n\n"
    "The result held in every run.\n\n"
    "- first item\n- second item\n\n"
    "The result held in every run.\n\n"
    "Closing line."
)


class IdlessBase(BlockBase):
    def setUp(self):
        super().setUp()
        text, is_error = self._tool_text(
            self._call(
                "create_note",
                {"note_type": "personal", "title": "Made over MCP", "content_text": MARKDOWN},
            )
        )
        self.assertFalse(is_error, text)
        self.made = PersonalNoteMaster.objects.get(
            note_id=json.loads(text[text.index("{") :])["note_id"]
        )
        self._grant_owner_on(self.made)
        self.no_doc()  # nobody ever opened it: no live doc, the stored body is the truth

    def _grant_owner_on(self, note):
        from origin.models.note.common_note_models import NotePermissionMaster

        NotePermissionMaster.objects.get_or_create(
            team=self.team,
            user=self.user,
            note_id=note.note_id,
            note_type=1,
            defaults={"role_id": 1},
        )

    def args_for(self, **extra):
        return {"note_type": "personal", "note_id": self.made.note_id, **extra}

    def blocks(self, **extra):
        self.answer({"exists": False, "blocks": [], "threads": []})
        text, is_error = self._tool_text(
            self._call("get_note", self.args_for(include_blocks=True, **extra))
        )
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])["blocks"]

    def edit(self, **extra):
        self.no_doc()
        return self._tool_text(self._call(EDIT, self.args_for(**extra)))

    def stored(self):
        return PersonalNoteMaster.objects.get(note_id=self.made.note_id).body

    def by_text(self, blocks):
        return {b["text"]: b for b in blocks}


class ThePremiseTests(IdlessBase):
    def test_a_note_made_over_mcp_really_has_a_body_with_no_ids(self):
        flat = []

        def walk(items):
            for b in items:
                flat.append(b)
                walk(b.get("children") or [])

        walk(self.stored())
        self.assertGreaterEqual(len(flat), 6)
        self.assertTrue(all(not b.get("id") for b in flat), "the premise of MIP-1672")


class EveryBlockIsListedWithAnIdTests(IdlessBase):
    def test_every_block_has_an_id_of_the_right_shape_and_they_are_unique(self):
        blocks = self.blocks()
        ids = [b["block_id"] for b in blocks]
        self.assertTrue(all(ids), ids)
        self.assertTrue(all(BLOCK_ID.match(i) for i in ids), ids)
        self.assertEqual(len(ids), len(set(ids)), "no two blocks share an id")

    def test_the_ids_are_the_same_on_every_read(self):
        self.assertEqual(
            [b["block_id"] for b in self.blocks()], [b["block_id"] for b in self.blocks()]
        )

    def test_two_identical_paragraphs_get_two_ids(self):
        same = [b for b in self.blocks() if b["text"] == "The result held in every run."]
        self.assertEqual(len(same), 2)
        self.assertNotEqual(same[0]["block_id"], same[1]["block_id"])

    def test_a_childs_parent_is_named_by_the_parents_id(self):
        blocks = self.blocks()
        ids = {b["block_id"] for b in blocks}
        for b in blocks:
            if b["parent_block_id"] is not None:
                self.assertIn(b["parent_block_id"], ids)

    def test_blocks_matching_finds_a_block_and_hands_back_an_id(self):
        # MIP-1666's `blocks_matching` used to find the block and return `block_id: null`.
        found = self.blocks(blocks_matching="second item")
        self.assertEqual(len(found), 1)
        self.assertTrue(BLOCK_ID.match(found[0]["block_id"]))


class TheIdFollowsTheBlockTests(IdlessBase):
    def test_inserting_or_deleting_other_blocks_does_not_change_an_unchanged_blocks_id(self):
        before = self.by_text(self.blocks())
        body = copy.deepcopy(self.stored())
        body.insert(
            0,
            {
                "type": "paragraph",
                "props": {},
                "content": [{"type": "text", "text": "New first", "styles": {}}],
                "children": [],
            },
        )
        del body[2]  # the list's first item
        PersonalNoteMaster.objects.filter(note_id=self.made.note_id).update(body=body)
        after = self.by_text(self.blocks())
        for text in ("Findings", "Closing line."):
            self.assertEqual(after[text]["block_id"], before[text]["block_id"], text)
        self.assertIn("New first", after)

    def test_changing_a_blocks_content_changes_its_id(self):
        before = self.by_text(self.blocks())["Closing line."]["block_id"]
        body = copy.deepcopy(self.stored())
        body[-1]["content"][0]["text"] = "A different closing line."
        PersonalNoteMaster.objects.filter(note_id=self.made.note_id).update(body=body)
        self.assertNotEqual(
            self.by_text(self.blocks())["A different closing line."]["block_id"], before
        )

    def test_a_block_that_has_an_id_keeps_it_and_the_others_are_derived(self):
        body = copy.deepcopy(self.stored())
        body[0]["id"] = "kept-id"
        PersonalNoteMaster.objects.filter(note_id=self.made.note_id).update(body=body)
        blocks = self.blocks()
        self.assertEqual(blocks[0]["block_id"], "kept-id")
        self.assertTrue(all(b["block_id"] for b in blocks))

    def test_an_id_the_body_already_holds_is_never_given_to_another_block(self):
        plain = [para("same", None), para("same", None)]
        for b in plain:
            b.pop("id", None)
        first, second = [s["block_id"] for s in _stored.summarize(plain)]
        # Now the body also holds a block whose STORED id is the one the first would derive.
        clash = copy.deepcopy(plain)
        clash.append({**para("other", first)})
        ids = [s["block_id"] for s in _stored.summarize(clash)]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertIn(first, ids)  # the stored id is still its holder's

    def test_the_derivation_is_pure(self):
        a = _stored.effective_ids(copy.deepcopy(self.stored()))
        b = _stored.effective_ids(copy.deepcopy(self.stored()))
        self.assertEqual(sorted(a.values()), sorted(b.values()))


class TheDerivationIsFixedAndIgnoresChildrenTests(IdlessBase):
    def test_a_known_block_derives_a_known_id_so_nobody_changes_it_by_accident(self):
        # Computed independently of the code under test: uuid5(namespace, canonical identity + "#0").
        # If this changes, EVERY derived id a caller is holding changes with it.
        block = para("pin", None)
        block.pop("id", None)
        self.assertEqual(
            _stored.summarize([block])[0]["block_id"], "84ca22e9-48fa-5cf9-846c-f7dd56288eea"
        )

    def test_identical_blocks_are_told_apart_by_how_many_came_before_them(self):
        # The n-th identical block's id is derived from its content and n (computed independently of the
        # code under test), not from whatever the first one happened to get.
        def pin():
            block = para("pin", None)
            block.pop("id", None)
            return block

        ids = [s["block_id"] for s in _stored.summarize([pin(), pin(), pin()])]
        self.assertEqual(
            ids,
            [
                "84ca22e9-48fa-5cf9-846c-f7dd56288eea",
                "628d6d9b-fb82-5157-bf62-759768670cce",
                "7fce5040-0550-593a-a6b0-af4810a60f37",
            ],
        )

    def test_a_parents_id_ignores_its_children(self):
        def tree(child_text):
            parent = para("parent", None)
            parent.pop("id", None)
            kid = para(child_text, None)
            kid.pop("id", None)
            parent["children"] = [kid]
            return [parent]

        before = _stored.summarize(tree("one"))
        after = _stored.summarize(tree("two"))
        self.assertEqual(
            before[0]["block_id"], after[0]["block_id"], "the parent is the same block"
        )
        self.assertNotEqual(before[1]["block_id"], after[1]["block_id"], "the child changed")
        self.assertEqual(before[1]["parent_block_id"], before[0]["block_id"])


class ALiveDocumentsIdsAreUsedAsTheyAreTests(IdlessBase):
    def test_collabs_ids_are_listed_not_derived(self):
        self.answer(
            {
                "exists": True,
                "blocks": [
                    {
                        "id": "live-1",
                        "type": "paragraph",
                        "depth": 0,
                        "parentId": None,
                        "text": "x",
                        "hash": "a" * 16,
                        "hasChildren": False,
                    }
                ],
                "threads": [],
            }
        )
        text, is_error = self._tool_text(self._call("get_note", self.args_for(include_blocks=True)))
        self.assertFalse(is_error, text)
        self.assertEqual(json.loads(text[text.index("{") :])["blocks"][0]["block_id"], "live-1")


class EditingByADerivedIdTests(IdlessBase):
    def setUp(self):
        super().setUp()
        self.listing = self.by_text(self.blocks())
        self.target = self.listing["Closing line."]

    def test_replace_changes_that_block_only_and_it_keeps_its_id(self):
        before_stored = copy.deepcopy(self.stored())
        text, is_error = self.edit(
            block_id=self.target["block_id"],
            op="replace",
            content_text="A better closing line.",
            base_block_hash=self.target["block_hash"],
        )
        self.assertFalse(is_error, text)
        after = self.stored()
        self.assertEqual(len(after), len(before_stored))
        # Every other block is stored exactly as it was: no id was written into them.
        self.assertEqual(after[:-1], before_stored[:-1])
        self.assertEqual(after[-1]["content"][0]["text"], "A better closing line.")
        self.assertEqual(after[-1]["id"], self.target["block_id"])
        # ...and the id still names it afterwards.
        again = self.by_text(self.blocks())
        self.assertEqual(again["A better closing line."]["block_id"], self.target["block_id"])
        self.assertEqual(again["Findings"]["block_id"], self.listing["Findings"]["block_id"])

    def test_insert_after_adds_a_block_after_it(self):
        text, is_error = self.edit(
            block_id=self.listing["Findings"]["block_id"],
            op="insert_after",
            content_text="Inserted.",
        )
        self.assertFalse(is_error, text)
        self.assertEqual(self.stored()[1]["content"][0]["text"], "Inserted.")

    def test_delete_removes_it(self):
        text, is_error = self.edit(block_id=self.target["block_id"], op="delete")
        self.assertFalse(is_error, text)
        self.assertNotIn("Closing line.", self.by_text(self.blocks()))

    def test_a_stale_base_block_hash_is_refused_and_nothing_changes(self):
        before = copy.deepcopy(self.stored())
        text, is_error = self.edit(
            block_id=self.target["block_id"],
            op="replace",
            content_text="x",
            base_block_hash="0" * 16,
        )
        self.assertTrue(is_error)
        self.assertIn("changed since you read it", text)
        self.assertEqual(self.stored(), before)

    def test_an_id_that_names_no_block_is_still_not_found(self):
        before = copy.deepcopy(self.stored())
        text, is_error = self.edit(block_id="not-a-block-of-this-note", op="delete")
        self.assertTrue(is_error)
        self.assertIn("not found", text)
        self.assertEqual(self.stored(), before)

    def test_the_flow_that_failed_find_a_block_by_its_text_then_change_only_it(self):
        found = self.blocks(blocks_matching="second item")
        self.assertEqual(len(found), 1)
        text, is_error = self.edit(
            block_id=found[0]["block_id"],
            op="replace",
            content_text="- second item, corrected",
            base_block_hash=found[0]["block_hash"],
        )
        self.assertFalse(is_error, text)
        texts = [s["text"] for s in _stored.summarize(self.stored())]
        self.assertIn("second item, corrected", " ".join(texts))
        self.assertIn("first item", " ".join(texts))
        self.assertIn("Closing line.", texts)
