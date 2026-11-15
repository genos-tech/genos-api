"""`save_conversation_summary` (MIP-1622): a short summary of an AI-app conversation, kept as
a private note in the member's own My Notes folder "AI chat summaries".

What is pinned here, because each one is a promise made to the member (and to counsel):

  * the note is private to its author and lands in a folder of THEIR My Notes, never in a
    Team Notes folder, never in someone else's folder;
  * the folder is found or made on the server, once per author, and a deleted or renamed
    folder does not break the next save;
  * a second save of the same conversation by the same author within 12 hours REPLACES the
    first (the old text stays in the version history), and nothing else is replaced: not
    another author's note, not an older one, not one that was moved out of the folder;
  * it is a SHORT summary: the caps refuse a transcript and nothing is written when they do;
  * only its author can search it, open it or see its folder;
  * it leaves with the member's other notes: exported, and deleted with the account.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine import search as search_module
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY
from origin.search_engine.chunkers.note_chunker import iter_personal_note_chunks
from origin.services import account_deletion, account_export
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.public.mcp.secretary import save_conversation_summary as tool
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_OWNER

NAME = "save_conversation_summary"


class SummaryBase(McpBase):
    def setUp(self):
        super().setUp()
        self._key(scope=SCOPE_WRITE)

    def save(self, ref="chat-1", topic="Launch plan", summary="We settled the launch order.", **kw):
        args = {"topic": topic, "summary": summary, "conversation_ref": ref, **kw}
        text, is_error = self._tool_text(self._call(NAME, args))
        return text, is_error

    def ok(self, **kw):
        text, is_error = self.save(**kw)
        self.assertFalse(is_error, text)
        return text

    def folders(self, user=None):
        return PersonalNoteFolder.objects.filter(
            owner=user or self.user, scope=PersonalNoteFolder.SCOPE_PERSONAL, name=tool.FOLDER_NAME
        )

    def notes(self, user=None):
        folder = self.folders(user).first()
        if folder is None:
            return PersonalNoteMaster.objects.none()
        return PersonalNoteMaster.objects.filter(
            owner=user or self.user, folder_id=folder.folder_id
        )

    def text_of(self, note):
        return "\n".join(tool._block_text(b) for b in note.body)


class TestWhereItGoes(SummaryBase):
    def test_a_summary_is_a_private_note_in_a_folder_made_for_it(self):
        self.ok(
            summary="We chose Friday for the launch.",
            items=[
                {"kind": "done", "text": "Picked the date"},
                {"kind": "next", "text": "Tell the team"},
                {"kind": "waiting", "text": "Legal sign-off"},
                {"kind": "decision", "text": "Launch on Friday"},
            ],
        )

        folder = self.folders().get()
        self.assertEqual(folder.team_id, self.team.team_id)
        self.assertIsNone(folder.parent_folder_id)
        note = self.notes().get()
        self.assertEqual(note.title, "AI chat summary: Launch plan")
        self.assertEqual(note.team_id, self.team.team_id)
        text = self.text_of(note)
        for part in (
            "We chose Friday for the launch.",
            "Done",
            "Picked the date",
            "Next",
            "Waiting",
            "Decision",
            "Conversation ref: chat-1",
        ):
            self.assertIn(part, text)

    def test_only_the_author_holds_a_role_on_it(self):
        self.ok()
        note = self.notes().get()
        roles = list(
            NotePermissionMaster.objects.filter(note_id=note.note_id, note_type=NOTE_TYPE_PERSONAL)
        )
        self.assertEqual([(r.user_id, r.role_id) for r in roles], [(self.user.id, ROLE_OWNER)])

    def test_the_folder_is_made_once_per_author_and_reused(self):
        self.ok(ref="a")
        self.ok(ref="b")
        self.assertEqual(self.folders().count(), 1)
        self.assertEqual(self.notes().count(), 2)

    def test_each_author_gets_their_own_folder_and_never_the_others(self):
        self.ok()
        mine = self.folders().get()
        self._key(scope=SCOPE_WRITE, user=self.user2)
        self.ok()
        theirs = self.folders(self.user2).get()
        self.assertNotEqual(mine.folder_id, theirs.folder_id)
        self.assertEqual(self.notes(self.user2).get().folder_id, theirs.folder_id)
        self.assertEqual(self.notes().get().folder_id, mine.folder_id)

    def test_a_team_notes_folder_or_another_members_folder_with_that_name_is_never_used(self):
        team_folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name=tool.FOLDER_NAME,
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )
        others = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user2,
            name=tool.FOLDER_NAME,
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
        )
        self.ok()
        note = self.notes().get()
        self.assertNotIn(note.folder_id, (team_folder.folder_id, others.folder_id))
        self.assertEqual(self.folders().get().folder_id, note.folder_id)

    def test_a_deleted_folder_is_made_again_by_the_next_save(self):
        self.ok(ref="a")
        first = self.folders().get()
        PersonalNoteMaster.objects.filter(folder_id=first.folder_id).delete()
        first.delete()

        self.ok(ref="b")

        second = self.folders().get()
        self.assertNotEqual(second.folder_id, first.folder_id)
        self.assertEqual(self.notes().count(), 1)

    def test_a_renamed_folder_is_left_alone_and_the_next_save_makes_a_new_one(self):
        self.ok(ref="a")
        old = self.folders().get()
        old.name = "Mine now"
        old.save(update_fields=["name"])

        self.ok(ref="b")

        self.assertEqual(PersonalNoteMaster.objects.filter(folder_id=old.folder_id).count(), 1)
        self.assertEqual(self.folders().count(), 1)
        self.assertNotEqual(self.folders().get().folder_id, old.folder_id)

    def test_a_folder_the_member_nested_is_still_found(self):
        self.ok(ref="a")
        folder = self.folders().get()
        parent = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="Archive", scope="personal"
        )
        folder.parent_folder_id = parent.folder_id
        folder.save(update_fields=["parent_folder_id"])

        self.ok(ref="b")

        self.assertEqual(self.folders().count(), 1)
        self.assertEqual(self.notes().count(), 2)

    def test_it_is_published_on_mcp_and_unknown_to_the_in_app_agent(self):
        self.assertIn(NAME, MCP_ONLY_REGISTRY)
        self.assertNotIn(NAME, REGISTRY)

    def test_a_read_only_key_cannot_save(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self.save()
        self.assertTrue(is_error)
        self.assertEqual(PersonalNoteMaster.objects.filter(owner=self.user).count(), 0)
        self.assertEqual(self.folders().count(), 0)


class TestNotEveryTurn(SummaryBase):
    """The server cannot see the conversation, so it cannot tell when to save; what it can do is
    make a model that offers too often leave ONE note per conversation."""

    def test_the_same_ref_within_twelve_hours_replaces_the_first_note(self):
        self.ok(summary="First thoughts.")
        before = self.notes().get()

        text = self.ok(summary="Final decision: ship Friday.", topic="Launch plan v2")

        after = self.notes().get()
        self.assertEqual(after.note_id, before.note_id, "updated in place, no second note")
        self.assertEqual(after.title, "AI chat summary: Launch plan v2")
        self.assertIn("Final decision", self.text_of(after))
        self.assertNotIn("First thoughts", self.text_of(after))
        self.assertIn("replaced_earlier_summary", text)
        self.assertEqual(self.folders().count(), 1)

    def test_the_earlier_text_stays_in_the_version_history(self):
        self.ok(summary="First thoughts.")
        note = self.notes().get()
        self.ok(summary="Final decision.")
        versions = NoteVersionMaster.objects.filter(
            note_type=NOTE_TYPE_PERSONAL, note_id=note.note_id
        )
        self.assertGreaterEqual(versions.count(), 2)
        self.assertTrue(
            any("First thoughts" in str(v.body) for v in versions), "the replaced text is kept"
        )

    def test_a_different_ref_is_a_different_conversation(self):
        self.ok(ref="one")
        self.ok(ref="two")
        self.assertEqual(self.notes().count(), 2)

    def test_the_ref_is_compared_after_it_is_cleaned(self):
        self.ok(ref="Chat #42 / Launch")
        self.ok(ref="chat-42-launch")
        self.assertEqual(self.notes().count(), 1)

    def test_after_twelve_hours_it_is_a_new_conversation(self):
        self.ok(summary="Yesterday's.")
        PersonalNoteMaster.objects.filter(owner=self.user).update(
            ts_created_at=timezone.now() - timedelta(hours=13)
        )
        self.ok(summary="Today's.")
        self.assertEqual(self.notes().count(), 2)

    def test_just_inside_twelve_hours_still_replaces(self):
        self.ok(summary="Earlier.")
        PersonalNoteMaster.objects.filter(owner=self.user).update(
            ts_created_at=timezone.now() - timedelta(hours=11, minutes=50)
        )
        self.ok(summary="Later.")
        self.assertEqual(self.notes().count(), 1)

    def test_another_author_with_the_same_ref_never_replaces_my_note(self):
        self.ok(ref="shared-ref", summary="Mine.")
        mine = self.notes().get()
        self._key(scope=SCOPE_WRITE, user=self.user2)

        self.ok(ref="shared-ref", summary="Theirs.")

        mine.refresh_from_db()
        self.assertIn("Mine.", self.text_of(mine))
        self.assertEqual(self.notes().count(), 1)
        self.assertEqual(self.notes(self.user2).count(), 1)
        self.assertIn("Theirs.", self.text_of(self.notes(self.user2).get()))

    def test_a_note_the_member_moved_out_of_the_folder_is_not_replaced(self):
        self.ok(ref="x", summary="Moved out.")
        note = self.notes().get()
        note.folder_id = None
        note.save(update_fields=["folder_id"])

        self.ok(ref="x", summary="New one.")

        note.refresh_from_db()
        self.assertIn("Moved out.", self.text_of(note))
        self.assertEqual(self.notes().count(), 1)

    def test_a_note_of_the_same_title_without_the_ref_line_is_not_replaced(self):
        self.ok(ref="x")
        note = self.notes().get()
        note.body = []  # the member emptied the note, so the ref line is gone
        note.save(update_fields=["body"])
        self.ok(ref="x")
        self.assertEqual(self.notes().count(), 2)


class TestItIsShort(SummaryBase):
    def assertRefused(self, text, is_error, *words):
        self.assertTrue(is_error, text)
        for word in words:
            self.assertIn(word, text)
        self.assertIn("Nothing was saved", text)
        self.assertEqual(PersonalNoteMaster.objects.filter(owner=self.user).count(), 0)
        self.assertEqual(self.folders().count(), 0, "a refusal does not even make the folder")

    def test_a_summary_over_the_cap_is_refused_and_nothing_is_written(self):
        text, is_error = self.save(summary="word " * 400)
        self.assertRefused(text, is_error, "SHORT summary", str(tool.SUMMARY_MAX))

    def test_a_long_topic_is_refused(self):
        text, is_error = self.save(topic="t" * (tool.TOPIC_MAX + 1))
        self.assertRefused(text, is_error, "topic")

    def test_too_many_items_are_refused(self):
        items = [{"kind": "done", "text": f"thing {i}"} for i in range(tool.ITEMS_MAX + 1)]
        text, is_error = self.save(items=items)
        self.assertRefused(text, is_error, "items")

    def test_a_long_item_is_refused(self):
        text, is_error = self.save(items=[{"kind": "next", "text": "x" * (tool.ITEM_MAX + 1)}])
        self.assertRefused(text, is_error, "SHORT summary")

    def test_an_unknown_kind_is_refused(self):
        text, is_error = self.save(items=[{"kind": "transcript", "text": "hello"}])
        self.assertRefused(text, is_error, "kind")

    def test_the_ref_is_required_and_a_summary_is_required(self):
        text, is_error = self._tool_text(self._call(NAME, {"topic": "t", "summary": "s"}))
        self.assertRefused(text, is_error, "conversation_ref")
        text, is_error = self._tool_text(
            self._call(NAME, {"topic": "t", "summary": " ", "conversation_ref": "r"})
        )
        self.assertRefused(text, is_error, "summary")

    def test_the_longest_allowed_summary_is_saved(self):
        self.ok(summary="s" * tool.SUMMARY_MAX, ref="r" * 500)
        note = self.notes().get()
        ref = tool.ref_of(note.body)
        self.assertEqual(len(ref), tool.REF_MAX)

    def test_a_mention_in_it_does_not_notify_anyone(self):
        with mock.patch("origin.search_engine.agent.tools.create_note.notify_note_mentions") as n:
            self.ok(
                summary=f"Asked [@Aki](user:{self.user2.id}) and [@eng](group:3) about it.",
                items=[{"kind": "next", "text": f"[@Aki](user:{self.user2.id}) to reply"}],
            )
        note = self.notes().get()
        self.assertEqual(note.mentioned_user_ids or [], [])
        text = self.text_of(note)
        self.assertIn("@Aki", text)
        self.assertNotIn(str(self.user2.id), str(note.body))
        self.assertTrue(n.called, "the normal path still runs; there is just nothing to notify")


class TestOnlyTheAuthorCanFindIt(SummaryBase):
    """A My Notes note is indexed with its owner as the one reader, and the search filters on
    that (`acl_user_ids`) for the in-app Spotlight and for `search_workspace` alike: both end in
    `search.search`. These tests pin the chunk's reader list, the filter the other member's
    search is built with, and that the other member cannot read the note or see its folder."""

    def setUp(self):
        super().setUp()
        self.ok(summary="Zebra-quartz plan.")
        self.note = self.notes().get()

    def chunk_acl(self):
        entities = {e.entity_id: e for e in iter_personal_note_chunks()}
        entity = entities[f"note:personal:{self.note.note_id}"]
        return {tuple(c.acl_user_ids) for c in entity.chunks}

    def test_the_index_lists_the_author_as_the_only_reader(self):
        self.assertEqual(self.chunk_acl(), {(str(self.user.id),)})

    def test_no_team_wide_sentinel_is_in_the_readers(self):
        for acl in self.chunk_acl():
            self.assertFalse([a for a in acl if str(a).startswith("team:")], acl)

    def test_another_members_search_filter_matches_none_of_them(self):
        (acl,) = self.chunk_acl()
        theirs = set(search_module._viewer_acl_terms(str(self.team.team_id), str(self.user2.id)))
        self.assertTrue(theirs, "a team member's terms include the team sentinel")
        self.assertEqual(theirs & set(acl), set())
        mine = set(search_module._viewer_acl_terms(str(self.team.team_id), str(self.user.id)))
        self.assertTrue(mine & set(acl), "and the author's do match")
        built = search_module._build_filter(
            str(self.team.team_id), str(self.user2.id), None, None, None
        )
        self.assertIn({"terms": {"acl_user_ids": sorted(theirs)}}, [_sorted(f) for f in built])

    def test_another_member_cannot_open_the_note_over_mcp(self):
        self._key(scope=SCOPE_READ, user=self.user2)
        text, is_error = self._tool_text(
            self._call("get_note", {"note_id": self.note.note_id, "note_type": "personal"})
        )
        self.assertTrue(is_error, text)
        self.assertNotIn("Zebra-quartz", text)

    def test_another_member_cannot_see_the_folder(self):
        self._key(scope=SCOPE_READ, user=self.user2)
        text, is_error = self._tool_text(self._call("list_note_folders", {}))
        self.assertFalse(is_error, text)
        self.assertNotIn(tool.FOLDER_NAME, text)

    def test_the_author_sees_the_folder(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._tool_text(self._call("list_note_folders", {}))
        self.assertFalse(is_error, text)
        self.assertIn(tool.FOLDER_NAME, text)

    def test_another_member_cannot_replace_it_with_update_note(self):
        self._key(scope=SCOPE_WRITE, user=self.user2)
        text, is_error = self._tool_text(
            self._call(
                "update_note",
                {"note_id": self.note.note_id, "note_type": "personal", "content_text": "x"},
            )
        )
        self.assertTrue(is_error, text)


def _sorted(f):
    terms = f.get("terms") if isinstance(f, dict) else None
    if terms and "acl_user_ids" in terms:
        return {"terms": {"acl_user_ids": sorted(terms["acl_user_ids"])}}
    return f


class TestItLeavesWithTheMember(SummaryBase):
    def test_it_is_in_the_export_with_the_folder_name(self):
        self.ok()
        exported = account_export.build_export(self.user)
        mine = [n for n in exported["personal_notes"] if n["title"].startswith(tool.TITLE_PREFIX)]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["folder"], tool.FOLDER_NAME)

    def test_it_is_deleted_with_the_account_and_the_other_members_is_not(self):
        self.ok()
        self._key(scope=SCOPE_WRITE, user=self.user2)
        self.ok()
        account_deletion._delete_personal_data(self.user)
        self.assertEqual(PersonalNoteMaster.objects.filter(owner=self.user).count(), 0)
        self.assertEqual(self.folders().count(), 0)
        self.assertEqual(self.notes(self.user2).count(), 1)
