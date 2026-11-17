"""`comment_on_note` on a note nobody has opened in the editor (MIP-1670).

A comment lives only in a note's Yjs document, so a note nobody had opened (typically one created over MCP)
answered "no document" and could not be commented on. Collab can now make the document from the note's STORED
body and write the thread in the same transaction (genos-collab `create-thread` with `seedBlocks`). What is
pinned here, collab stubbed at `requests.post`:

- the tool asks normally FIRST, and only when collab answers `no_document` and the note has a stored body does
  it ask ONCE more, with that body (every block carrying the id `get_note` listed for it, MIP-1672), so the ids
  a caller holds are the new document's ids and `block_id` keeps working;
- nothing else is retried (a second POST of anything else is a second thread), and the retry is never itself
  retried: every way it can fail reads as a sentence, with at most two requests made;
- a note with no stored text, or collab saying something other than `no_document`, is not retried;
- the people the comment reaches are told on this path too, and the answer says the document was made.
"""

from __future__ import annotations

import json
from unittest import mock

import requests

from origin.models.chat.unified_models import Activity
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.views.public.mcp.note_blocks import _stored

from .test_mcp_note_comment_start import COMMENT, CREATED, StartBase
from .test_mcp_note_thread_writes import EDITOR_ROLE

MARKDOWN = (
    "# Findings\n\nThe result held in every run.\n\n- first item\n- second item\n\nClosing line."
)
SEEDED = {**CREATED, "seeded": True, "blockId": "ignored-by-the-tool-under-test"}


def reply(status, body):
    return mock.Mock(status_code=status, json=mock.Mock(return_value=body))


NO_DOCUMENT = reply(404, {"error": "document not found", "code": "no_document"})


class ColdNoteBase(StartBase):
    """A note made through `create_note` (so its stored body really has no ids) that nobody has opened."""

    def setUp(self):
        super().setUp()
        text, is_error = self._tool_text(
            self._call(
                "create_note",
                {"note_type": "personal", "title": "Made over MCP", "content_text": MARKDOWN},
            )
        )
        self.assertFalse(is_error, text)
        self.cold = PersonalNoteMaster.objects.get(
            note_id=json.loads(text[text.index("{") :])["note_id"]
        )
        from origin.models.note.common_note_models import NotePermissionMaster

        NotePermissionMaster.objects.get_or_create(
            team=self.team,
            user=self.user,
            note_id=self.cold.note_id,
            note_type=1,
            defaults={"role_id": 1},
        )
        self.post.reset_mock()

    def comment(self, **extra):
        return self._tool_text(
            self._call(
                COMMENT,
                {
                    "note_type": "personal",
                    "note_id": self.cold.note_id,
                    "anchor_text": "held in every run",
                    "text": "Why every run?",
                    **extra,
                },
            )
        )

    def commented(self, **extra):
        text, is_error = self.comment(**extra)
        self.assertFalse(is_error, text)
        return json.loads(text[text.index("{") :])

    def refused(self, **extra):
        text, is_error = self.comment(**extra)
        self.assertTrue(is_error, text)
        return text

    def sent_bodies(self):
        return [c.kwargs["json"] for c in self.post.call_args_list]


class AskedOnceMoreWithTheStoredBodyTests(ColdNoteBase):
    def test_the_first_request_has_no_body_and_the_second_has_it_with_ids(self):
        self.post.side_effect = [NO_DOCUMENT, reply(200, SEEDED)]
        out = self.commented()
        first, second = self.sent_bodies()
        self.assertEqual(self.post.call_count, 2)
        self.assertNotIn("seedBlocks", first)
        self.assertIn("seedBlocks", second)
        # Everything else about the two requests is the same.
        self.assertEqual({k: v for k, v in second.items() if k != "seedBlocks"}, first)
        self.assertIs(out["document_created"], True)

    def test_the_ids_sent_are_the_ids_get_note_listed_so_a_held_block_id_keeps_working(self):
        listed = [b["block_id"] for b in _stored.summarize(self.cold.body)]
        self.post.side_effect = [NO_DOCUMENT, reply(200, SEEDED)]
        self.commented()
        seed = self.sent_bodies()[1]["seedBlocks"]

        def flat(blocks):
            for b in blocks:
                yield b["id"]
                yield from flat(b.get("children") or [])

        self.assertEqual(list(flat(seed)), listed)
        self.assertTrue(all(listed))

    def test_the_stored_body_itself_is_not_changed_by_asking(self):
        before = PersonalNoteMaster.objects.get(note_id=self.cold.note_id).body
        self.post.side_effect = [NO_DOCUMENT, reply(200, SEEDED)]
        self.commented()
        self.assertEqual(PersonalNoteMaster.objects.get(note_id=self.cold.note_id).body, before)

    def test_a_block_id_the_caller_holds_goes_through_unchanged(self):
        held = _stored.summarize(self.cold.body)[1]["block_id"]
        self.post.side_effect = [NO_DOCUMENT, reply(200, SEEDED)]
        self.commented(block_id=held)
        self.assertEqual({b.get("blockId") for b in self.sent_bodies()}, {held})

    def test_a_note_that_already_has_a_document_makes_one_request_and_says_nothing_was_created(
        self,
    ):
        self.post.side_effect = [reply(200, CREATED)]
        out = self.commented()
        self.assertEqual(self.post.call_count, 1)
        self.assertIs(out["document_created"], False)


class NothingElseIsRetriedTests(ColdNoteBase):
    def assert_one_request(self):
        self.assertEqual(self.post.call_count, 1)
        self.assertNotIn("seedBlocks", self.sent_bodies()[0])

    def test_a_missing_passage_on_an_existing_document_is_not_retried(self):
        self.post.side_effect = [reply(404, {"error": "x", "code": "anchor_not_found"})]
        self.assertIn("not found in the note", self.refused())
        self.assert_one_request()

    def test_an_ambiguous_passage_is_not_retried(self):
        self.post.side_effect = [
            reply(
                409,
                {
                    "error": "x",
                    "code": "anchor_ambiguous",
                    "matches": 2,
                    "blockIds": ["a"],
                    "blockCount": 1,
                },
            )
        ]
        self.refused()
        self.assert_one_request()

    def test_a_timeout_is_not_retried_a_second_post_could_be_a_second_thread(self):
        self.post.side_effect = requests.ReadTimeout("slow")
        self.assertIn("may or may not have been added", self.refused())
        self.assert_one_request()

    def test_collab_down_is_not_retried(self):
        self.post.side_effect = requests.ConnectTimeout("down")
        self.refused()
        self.assert_one_request()

    def test_a_note_with_no_stored_text_is_not_retried_and_says_how_to_fix_it(self):
        PersonalNoteMaster.objects.filter(note_id=self.cold.note_id).update(body=[])
        self.post.side_effect = [NO_DOCUMENT]
        text = self.refused()
        self.assertIn("Nobody has opened this note", text)
        self.assertIn("no text to make one from", text)
        self.assert_one_request()

    def test_a_body_that_is_not_a_list_is_not_retried(self):
        PersonalNoteMaster.objects.filter(note_id=self.cold.note_id).update(body=None)
        self.post.side_effect = [NO_DOCUMENT]
        self.refused()
        self.assert_one_request()


class WhenTheRetryFailsTests(ColdNoteBase):
    """Never a third request, and the sentence says why and that nothing was changed."""

    def second_fails_with(self, second):
        self.post.side_effect = [NO_DOCUMENT, second]
        text = self.refused()
        self.assertEqual(self.post.call_count, 2, "the retry is never itself retried")
        self.assertIn("seedBlocks", self.sent_bodies()[1])
        return text

    def test_the_passage_is_not_in_the_body(self):
        text = self.second_fails_with(
            reply(404, {"error": "x", "code": "anchor_not_found", "blockedBlocks": 0})
        )
        self.assertIn("not found in the note", text)

    def test_the_passage_is_ambiguous_in_the_body(self):
        text = self.second_fails_with(
            reply(
                409,
                {
                    "error": "x",
                    "code": "anchor_ambiguous",
                    "matches": 2,
                    "blockIds": ["a", "b"],
                    "blockCount": 2,
                },
            )
        )
        self.assertIn("2 times", text)

    def test_the_body_does_not_fit_the_editors_schema(self):
        text = self.second_fails_with(reply(400, {"error": "x", "code": "seed_invalid"}))
        self.assertIn("could not be turned into a document", text)
        self.assertIn("Open it once", text)
        self.assertIn("Nothing was changed", text)

    def test_the_body_is_too_large_to_send(self):
        text = self.second_fails_with(reply(413, {"error": "body too large"}))
        self.assertIn("too large", text)
        self.assertIn("Open it once", text)

    def test_the_second_request_times_out(self):
        self.post.side_effect = [NO_DOCUMENT, requests.ReadTimeout("slow")]
        text = self.refused()
        self.assertEqual(self.post.call_count, 2)
        self.assertIn("may or may not have been added", text)

    def test_an_older_collab_that_does_not_know_seedblocks_still_says_no_document(self):
        # It answers the unknown field by ignoring it, so the second answer is the same refusal.
        text = self.second_fails_with(NO_DOCUMENT)
        self.assertIn("Nobody has opened this note", text)


class ThePeopleAreToldOnThisPathToo(ColdNoteBase):
    def test_the_other_member_gets_the_commented_activity_after_a_seeded_comment(self):
        self._grant_cold(self.user2)
        self.post.side_effect = [NO_DOCUMENT, reply(200, SEEDED)]
        self.commented()
        rows = list(Activity.objects.filter(recipient=self.user2, surface_type=6))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].meta["noteId"], self.cold.note_id)

    def test_nobody_is_told_when_the_comment_was_not_made(self):
        self._grant_cold(self.user2)
        self.post.side_effect = [
            NO_DOCUMENT,
            reply(404, {"error": "x", "code": "anchor_not_found"}),
        ]
        self.refused()
        self.assertEqual(Activity.objects.filter(recipient=self.user2, surface_type=6).count(), 0)

    def _grant_cold(self, user):
        from origin.models.note.common_note_models import NotePermissionMaster

        NotePermissionMaster.objects.create(
            team=self.team, user=user, note_id=self.cold.note_id, note_type=1, role_id=EDITOR_ROLE
        )


class WithEffectiveIdsTests(ColdNoteBase):
    def test_it_returns_a_copy_with_an_id_on_every_block_and_leaves_the_input_alone(self):
        body = self.cold.body
        snapshot = json.loads(json.dumps(body))
        out = _stored.with_effective_ids(body)
        self.assertEqual(body, snapshot, "the input is not changed")
        self.assertIsNot(out, body)

        def walk(blocks):
            for b in blocks:
                self.assertTrue(b["id"])
                walk(b.get("children") or [])

        walk(out)
        self.assertEqual(
            [b["block_id"] for b in _stored.summarize(out)],
            [b["block_id"] for b in _stored.summarize(body)],
            "writing the ids in changes none of them",
        )

    def test_children_of_children_get_ids_too(self):
        def block(text, kids=()):
            return {
                "type": "bulletListItem",
                "props": {},
                "content": [{"type": "text", "text": text, "styles": {}}],
                "children": list(kids),
            }

        body = [block("parent", [block("child", [block("grandchild")])])]
        out = _stored.with_effective_ids(body)
        self.assertTrue(out[0]["id"])
        self.assertTrue(out[0]["children"][0]["id"])
        self.assertTrue(out[0]["children"][0]["children"][0]["id"])
        self.assertEqual(
            [b["block_id"] for b in _stored.summarize(out)],
            [b["block_id"] for b in _stored.summarize(body)],
        )

    def test_a_stored_id_is_kept(self):
        body = [{"id": "mine", "type": "paragraph", "props": {}, "content": [], "children": []}]
        self.assertEqual(_stored.with_effective_ids(body)[0]["id"], "mine")

    def test_anything_but_a_list_is_an_empty_body(self):
        self.assertEqual(_stored.with_effective_ids(None), [])
        self.assertEqual(_stored.with_effective_ids({"a": 1}), [])
