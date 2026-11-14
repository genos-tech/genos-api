"""MIP-1547: `GET /api/v2/note/chat/` (`ChatNoteMasterView.get`) lists a caller's own notes of a
chat through the ONE chat-note rule, not by `owner` alone.

It used to filter `owner=request.user`, so an author who LEFT the chat (the leave ended their
explicit row, api #753) still got HTTP 200 with the note's body from this route, after MIP-1492
(api #766) had closed every other path. The list is now the caller's own notes (unchanged: nobody
sees another person's note here) intersected with `readable_note_ids`, the batch form of
`get_effective_role`:

  * a still-member author, or an author holding an explicit row (the creation fan-out, a
    deliberate re-share): listed;
  * a note of a SOFT-DELETED chat: listed for the author through their own row (Ken's MIP-1505
    decision: the row wins, the channel's liveness is not asked);
  * an author who left, was taken off the project, or never held a row on a deleted chat: not.

The access matrix and builders follow `test_deleted_chat_notes_stay_readable` (api #831): the REAL
create path (`chat_note_roles` fans the rows out) and the REAL leave endpoint.
"""

from __future__ import annotations

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.tests.test_chat_note_one_rule import CHAT, ChatNoteFixtures
from origin.views.utils.note_role import ROLE_VIEWER

BODY = "secret chat body"


class ChatNoteListFixtures(ChatNoteFixtures):
    """`self.reader` is the author under test, `self.owner` the other member of every chat."""

    def kind_chat(self, kind, *members):
        channel = Channel.objects.create(
            team=self.team, kind=kind, title=f"chat {kind}", owner=self.owner
        )
        for m in members:
            ChannelMember.objects.create(
                channel=channel, user=m, role="owner" if m == self.owner else "member"
            )
        return channel

    def create_note(self, author, channel, title="The chat note", **extra):
        """The real create path: the note and the role rows `chat_note_roles` fans out."""
        self.authenticate(author)
        res = self.client.post(
            "/api/v2/note/chat/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(author.id),
                "chat_type": int(channel.kind),
                "chat_id": str(channel.id),
                "title": title,
                "body": [{"type": "paragraph", "content": [{"type": "text", "text": BODY}]}],
                "is_thread": False,
                **extra,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return ChatNoteMaster.objects.get(note_id=res.json()["noteId"])

    def leave(self, channel, user):
        self.authenticate(user)
        res = self.client.delete(f"/api/v3/channels/{channel.id}/members/{user.id}/")
        self.assertEqual(res.status_code, 204, res.content)

    def soft_delete(self, channel):
        Channel.objects.filter(id=channel.id).update(is_deleted=True)

    def share(self, note, user, role=ROLE_VIEWER):
        NotePermissionMaster.objects.update_or_create(
            team=self.team,
            user=user,
            note_type=CHAT,
            note_id=note.note_id,
            defaults={"role_id": role},
        )

    def list_own(self, user, channel):
        """The ids `GET /api/v2/note/chat/` lists for `user` in `channel` (the bodies too)."""
        self.authenticate(user)
        res = self.client.get(
            "/api/v2/note/chat/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(user.id),
                "chat_type": int(channel.kind),
                "chat_id": str(channel.id),
                "is_thread": "False",
                "thread_id": "null",
            },
        )
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def listed(self, user, channel) -> set[int]:
        return {n["noteId"] for n in self.list_own(user, channel)}


class AuthorWhoLeftAGroupChat(ChatNoteListFixtures):
    def setUp(self):
        super().setUp()
        self.chat = self.kind_chat(ChannelKind.GM, self.reader, self.owner)
        self.note = self.create_note(self.reader, self.chat)

    def test_a_current_member_author_lists_the_note_with_its_body(self):
        rows = self.list_own(self.reader, self.chat)
        self.assertEqual([r["noteId"] for r in rows], [self.note.note_id])
        self.assertIn(BODY, str(rows[0]["body"]))

    def test_an_author_who_left_no_longer_gets_the_note(self):
        self.leave(self.chat, self.reader)
        self.assertEqual(self.listed(self.reader, self.chat), set())
        # the same verdict as the single-note route and the sidebar
        self.assertEqual(self.rest_single(self.note).status_code, 403)
        self.assertNotIn(self.note.note_id, self.rest_meta_ids())

    def test_an_author_who_left_and_was_never_given_a_row_is_out_too(self):
        """A legacy note: no explicit row, only the live membership, which the leave ended."""
        NotePermissionMaster.objects.filter(note_type=CHAT, note_id=self.note.note_id).delete()
        self.assertEqual(self.listed(self.reader, self.chat), {self.note.note_id})
        ChannelMember.objects.filter(channel=self.chat, user=self.reader).update(is_deleted=True)
        self.assertEqual(self.listed(self.reader, self.chat), set())

    def test_the_other_member_is_unaffected_by_the_authors_leaving(self):
        other = self.create_note(self.owner, self.chat, title="theirs")
        self.leave(self.chat, self.reader)
        self.assertEqual(self.listed(self.owner, self.chat), {other.note_id})

    def test_an_explicit_row_re_shared_to_the_author_who_left_lists_it_again(self):
        """A grant is not authorship, but it IS a row: the explicit row wins, as everywhere."""
        self.leave(self.chat, self.reader)
        self.share(self.note, self.reader)
        self.assertEqual(self.listed(self.reader, self.chat), {self.note.note_id})

    def test_nobody_gains_the_list_stays_the_callers_own_notes(self):
        """The owner filter stays: another member's note is not listed here, row or not."""
        self.assertEqual(self.listed(self.owner, self.chat), set())
        self.share(self.note, self.user)
        self.assertEqual(self.listed(self.owner, self.chat), set())
        self.assertEqual(self.listed(self.user, self.chat), set())

    def test_a_stranger_lists_nothing(self):
        self.assertEqual(self.listed(self.user, self.chat), set())


class AuthorTakenOffAProject(ChatNoteListFixtures):
    def setUp(self):
        super().setUp()
        self.project = self.project_of("LST", self.reader)
        self.chat = self.pm_chat(self.project)
        self.note = self.create_note(self.reader, self.chat)

    def test_a_current_project_member_author_lists_the_note(self):
        self.assertEqual(self.listed(self.reader, self.chat), {self.note.note_id})

    def test_the_author_taken_off_the_project_no_longer_gets_it(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        # a stray channel row must not keep them in (MIP-1474)
        ChannelMember.objects.filter(channel=self.chat, user=self.reader).update(is_deleted=False)
        NotePermissionMaster.objects.filter(
            note_type=CHAT, note_id=self.note.note_id, user=self.reader
        ).delete()
        self.assertEqual(self.listed(self.reader, self.chat), set())


class NoteOfADeletedChat(ChatNoteListFixtures):
    """Ken's MIP-1505 decision: the explicit rows keep a deleted chat's notes readable."""

    def test_a_deleted_group_chats_note_stays_listed_for_its_author(self):
        chat = self.kind_chat(ChannelKind.GM, self.reader, self.owner)
        note = self.create_note(self.reader, chat)
        self.soft_delete(chat)
        self.assertEqual(self.listed(self.reader, chat), {note.note_id})

    def test_a_deleted_dm_and_a_deleted_project_chat_stay_listed_for_the_author(self):
        dm = self.kind_chat(ChannelKind.DM, self.reader, self.owner)
        dm_note = self.create_note(self.reader, dm)
        project = self.project_of("DEL", self.reader)
        pm = self.pm_chat(project)
        pm_note = self.create_note(self.reader, pm)
        self.soft_delete(dm)
        self.soft_delete(pm)
        self.assertEqual(self.listed(self.reader, dm), {dm_note.note_id})
        self.assertEqual(self.listed(self.reader, pm), {pm_note.note_id})

    def test_an_explicit_row_holder_author_of_a_deleted_chat_note_lists_it(self):
        """The note was written into a chat the author is not a member of, by a share (a row)."""
        chat = self.kind_chat(ChannelKind.GM, self.owner)
        note = self.create_note(self.owner, chat)
        ChatNoteMaster.objects.filter(note_id=note.note_id).update(owner=self.reader)
        self.share(note, self.reader)
        self.soft_delete(chat)
        self.assertEqual(self.listed(self.reader, chat), {note.note_id})

    def test_an_author_who_left_before_the_delete_stays_out(self):
        chat = self.kind_chat(ChannelKind.GM, self.reader, self.owner)
        note = self.create_note(self.reader, chat)
        self.leave(chat, self.reader)
        self.soft_delete(chat)
        self.assertNotIn(note.note_id, self.listed(self.reader, chat))
        self.assertEqual(self.listed(self.reader, chat), set())

    def test_a_legacy_author_with_no_row_reads_nothing_after_the_delete(self):
        chat = self.kind_chat(ChannelKind.GM, self.reader, self.owner)
        note = self.create_note(self.reader, chat)
        NotePermissionMaster.objects.filter(note_type=CHAT, note_id=note.note_id).delete()
        self.assertEqual(self.listed(self.reader, chat), {note.note_id})  # live: membership
        self.soft_delete(chat)
        self.assertEqual(self.listed(self.reader, chat), set())  # deleted: no row, no read


class ListShape(ChatNoteListFixtures):
    def test_the_query_count_does_not_grow_with_the_number_of_notes(self):
        chat = self.kind_chat(ChannelKind.GM, self.reader, self.owner)
        self.create_note(self.reader, chat, title="n0")

        def count():
            self.list_own(self.reader, chat)  # warm: auth/session caches
            with CaptureQueriesContext(connection) as ctx:
                self.list_own(self.reader, chat)
            return len(ctx)

        one = count()
        for i in range(1, 6):
            self.create_note(self.reader, chat, title=f"n{i}")
        self.assertEqual(len(self.list_own(self.reader, chat)), 6)
        self.assertEqual(count(), one)

    def test_the_list_keeps_its_order_and_shape(self):
        chat = self.kind_chat(ChannelKind.GM, self.reader, self.owner)
        first = self.create_note(self.reader, chat, title="first")
        second = self.create_note(self.reader, chat, title="second")
        rows = self.list_own(self.reader, chat)
        self.assertEqual([r["noteId"] for r in rows], [first.note_id, second.note_id])
        self.assertEqual(
            set(rows[0]),
            {
                "noteType",
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            },
        )
