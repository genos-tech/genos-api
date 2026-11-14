"""MIP-1492: a chat note's AUTHOR who left the chat, the project or the team loses the note like
everyone else, on every surface (REST has done so since MIP-1482).

`ChatNoteMaster.owner` used to be admitted as "the note's own ACL" by the agent's `may_read_note`,
the chat-note ACL set (`chat_note_acl_user_ids`: `get_note` over MCP and in the app, the source
visibility rule, mentions, citations, note summaries), the OpenSearch chunk ACL, the agent's
`has_write_permission`, the timeline's `readable_notes` and the briefing's "edits to your notes".
All of them now ask the one chat-note rule (`get_effective_role` / `chat_note_member_q` / the chat's
member set) and an explicit grant, exactly like any other reader.

Task notes and personal notes keep their author rule (pinned at the end).
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from origin.models.chat.unified_models import ChannelMember
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.search_engine.agent.acl import chat_note_acl_user_ids, may_read_note
from origin.search_engine.agent.tools.note_access import has_write_permission
from origin.search_engine.chunkers.note_chunker import iter_chat_note_chunks
from origin.services import briefing, timeline
from origin.tests.test_chat_note_one_rule import CHAT, ChatNoteFixtures
from origin.views.utils.note_role import ROLE_VIEWER


class Surfaces(ChatNoteFixtures):
    """`self.reader` is the author under test; `self.owner` is another member of every chat."""

    def verdicts(self, note, user=None) -> dict[str, bool]:
        """What every agent-side gate says about `note` for `user` (the author, by default)."""
        user = user or self.reader
        ctx = self.ctx()
        if str(ctx.user_id) != str(user.id):
            ctx = type(ctx)(team_id=ctx.team_id, user_id=str(user.id))
        in_chunks = any(
            str(user.id) in c.acl_user_ids
            for batch in iter_chat_note_chunks()
            if batch.entity_id == f"note:chat:{note.note_id}"
            for c in batch.chunks
        )
        return {
            "may_read_note": may_read_note(
                user_id=str(user.id),
                note_type_code=CHAT,
                note_id=note.note_id,
                owner_id=note.owner_id,
            ),
            "chat_note_acl_user_ids": str(user.id)
            in chat_note_acl_user_ids(
                owner_id=note.owner_id,
                chat_type_code=note.chat_type,
                channel_id=note.channel_id,
                note_id=note.note_id,
            ),
            "chunk acl": in_chunks,
            "has_write_permission": has_write_permission(
                owner_id=note.owner_id, note_id=note.note_id, note_type_code=CHAT, ctx=ctx
            ),
            "timeline": (CHAT, note.note_id)
            in timeline.readable_notes(ctx, [(CHAT, note.note_id)]),
            "briefing _may_read_note": briefing._may_read_note(ctx, CHAT, note),
            "briefing _changed_notes": self.hears_of_edits(note, ctx),
        }

    def hears_of_edits(self, note, ctx) -> bool:
        """Does the author's briefing list another person's edit of the note?"""
        NoteVersionMaster.objects.filter(note_type=CHAT, note_id=note.note_id).delete()
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=CHAT,
            note_id=note.note_id,
            version_no=1,
            title=note.title,
            body=note.body,
            editor=self.owner if str(ctx.user_id) != str(self.owner.id) else self.user,
        )
        now = timezone.now()
        win = briefing.Window(
            name="t", since=now - timedelta(days=1), until=now + timedelta(days=1)
        )
        found = briefing._changed_notes(ctx, win, briefing._Names(ctx))
        return any(i["note_id"] == note.note_id and i["note_type"] == "chat" for i in found)

    def assertAll(self, note, expected: bool, user=None):
        got = self.verdicts(note, user)
        self.assertEqual(got, {k: expected for k in got}, got)

    def assertMcpGetNote(self, note, expected: bool):
        text, is_error = self.mcp_get_note(note.note_id)
        self.assertEqual(not is_error and "secret chat body" in text, expected, text)


class AuthorWhoLeftAGroupChat(Surfaces):
    def setUp(self):
        super().setUp()
        self.chat = self.gm(self.reader, self.owner)
        self.note = self.note_on(self.chat, owner=self.reader)

    def test_a_current_member_author_passes_everywhere(self):
        self.assertAll(self.note, True)
        self.assertMcpGetNote(self.note, True)

    def test_the_author_who_left_is_refused_everywhere(self):
        ChannelMember.objects.filter(channel=self.chat, user=self.reader).update(is_deleted=True)
        self.assertAll(self.note, False)
        self.assertMcpGetNote(self.note, False)

    def test_the_other_member_still_passes_after_the_author_left(self):
        ChannelMember.objects.filter(channel=self.chat, user=self.reader).update(is_deleted=True)
        got = self.verdicts(self.note, self.owner)
        got.pop("briefing _changed_notes")  # "edits to MY notes": the note is not theirs
        self.assertEqual(got, {k: True for k in got}, got)

    def test_an_explicit_share_to_the_author_who_left_still_opens_it(self):
        """A grant is not authorship: the owner re-sharing the note to someone outside the chat."""
        ChannelMember.objects.filter(channel=self.chat, user=self.reader).update(is_deleted=True)
        NotePermissionMaster.objects.create(
            team=self.team, user=self.reader, note_type=CHAT, note_id=self.note.note_id, role_id=3
        )
        got = self.verdicts(self.note)
        # a Viewer share reads everywhere; it writes nowhere
        self.assertFalse(got.pop("has_write_permission"))
        for gate in ("may_read_note", "chat_note_acl_user_ids", "chunk acl", "timeline"):
            self.assertTrue(got[gate], gate)
        self.assertTrue(got["briefing _may_read_note"], got)
        self.assertMcpGetNote(self.note, True)

    def test_a_share_to_someone_who_never_was_in_the_chat_still_opens_it(self):
        stranger = self.reader  # not the author here
        note = self.note_on(self.gm(self.owner), owner=self.owner)
        NotePermissionMaster.objects.create(
            team=self.team, user=stranger, note_type=CHAT, note_id=note.note_id, role_id=ROLE_VIEWER
        )
        got = self.verdicts(note, stranger)
        self.assertTrue(got["may_read_note"] and got["chat_note_acl_user_ids"], got)
        self.assertTrue(got["timeline"] and got["briefing _may_read_note"], got)
        self.assertMcpGetNote(note, True)


class AuthorTakenOffAProject(Surfaces):
    def setUp(self):
        super().setUp()
        self.project = self.project_of("AUT", self.reader)
        self.chat = self.pm_chat(self.project)
        self.note = self.note_on(self.chat, owner=self.reader)

    def test_a_current_project_member_author_passes_everywhere(self):
        self.assertAll(self.note, True)

    def test_the_author_taken_off_the_project_is_refused_everywhere(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        self.assertAll(self.note, False)
        self.assertMcpGetNote(self.note, False)

    def test_a_stray_channel_row_does_not_keep_the_author_in(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        ChannelMember.objects.filter(channel=self.chat, user=self.reader).update(is_deleted=False)
        self.assertAll(self.note, False)


class AuthorOfADriftedNote(Surfaces):
    def test_a_note_filed_under_another_kind_than_its_channels_opens_for_no_author_either(self):
        from origin.models.chat.unified_models import ChannelKind

        gm = self.gm(self.reader, self.owner)
        note = self.note_on(gm, chat_type=ChannelKind.DM, owner=self.reader)
        got = self.verdicts(note)
        for gate in ("may_read_note", "chat_note_acl_user_ids", "has_write_permission", "timeline"):
            self.assertFalse(got[gate], gate)


class TaskAndPersonalNotesKeepTheirAuthorRule(Surfaces):
    """Not touched: a task or personal note's author is still admitted by `owner_id`."""

    def test_the_author_of_a_personal_note_still_reads_and_writes_it(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.reader, title="mine", body=[]
        )
        self.assertTrue(
            may_read_note(
                user_id=str(self.reader.id),
                note_type_code=1,
                note_id=note.note_id,
                owner_id=note.owner_id,
            )
        )
        self.assertTrue(
            has_write_permission(
                owner_id=note.owner_id, note_id=note.note_id, note_type_code=1, ctx=self.ctx()
            )
        )
        self.assertIn((1, note.note_id), timeline.readable_notes(self.ctx(), [(1, note.note_id)]))
