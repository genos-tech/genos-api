"""MIP-1505 (Ken's decision): a deleted chat's notes STAY readable to whoever holds an explicit role
on them, and their author keeps access through their own row.

`get_effective_role` lets an explicit `NotePermissionMaster` row win and never looks at the
channel's liveness; this file pins that as a guarantee on every chat-note path, for a note whose
channel is soft-deleted (project PM chat, group chat, DM, multi-person DM). After MIP-1492 the
author is no exception any more (no `owner_id` shortcut), so the author reads a deleted chat's note
only through the Owner row `chat_note_roles` wrote when the note was created.

Who reads, for a note on a SOFT-DELETED channel:
  * a holder of an explicit row (the creation fan-out, or a deliberate share to an outsider): yes;
  * the author, through their own row: yes;
  * a member with NO row (added after the note was made, or a project member): no, because
    membership counts only while the channel is live (`chat_note_member_q`), as before;
  * someone whose row was ended when they left (MIP-1482): no;
  * a stranger: no.

Every reader asked below: REST single / sidebar lists / sub-notes / write, the two collab gates,
MCP `get_note` and `update_note`, the agent's `may_read_note` and `has_write_permission`, the chat
note ACL set, the OpenSearch chunk ACL, the timeline's and the briefing's rules.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.search_engine.agent.acl import chat_acl_user_ids
from origin.search_engine.chunkers.note_chunker import iter_chat_note_chunks
from origin.services.project_delete import delete_project
from origin.tests.test_chat_note_author_loses_access import Surfaces
from origin.views.utils.note_role import (
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_VIEWER,
    get_effective_role,
    readable_note_ids,
)

User = get_user_model()
CHAT = 3
BODY = "secret chat body"
CHAT_KIND_DM = int(ChannelKind.DM)


class DeletedChatBase(Surfaces):
    """`self.reader` is the author, `self.owner` the other participant, `self.user` a teammate who
    was never in any chat (the stranger), `self.ex` a person who was in the chat and left."""

    def setUp(self):
        super().setUp()
        self.stranger = self.user
        self.ex = User.objects.create_user(username="ex", email="ex@example.com", password="x")
        TeamMembers.objects.create(team=self.team, attendee=self.ex)
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.outsider)

    # -- builders ----------------------------------------------------------------------------- #

    def kind_chat(self, kind, *members, owner=None):
        channel = Channel.objects.create(
            team=self.team, kind=kind, title=f"chat {kind}", owner=owner or self.owner
        )
        for m in members:
            ChannelMember.objects.create(
                channel=channel, user=m, role="owner" if m == (owner or self.owner) else "member"
            )
        return channel

    def create_note(self, author, channel, **extra):
        """The REAL create path: the note and the role rows `chat_note_roles` fans out."""
        self.authenticate(author)
        res = self.client.post(
            "/api/v2/note/chat/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(author.id),
                "chat_type": int(channel.kind),
                "chat_id": str(channel.id),
                "title": "The chat note",
                "body": [
                    {"type": "paragraph", "content": [{"type": "text", "text": BODY}]},
                ],
                "is_thread": False,
                **extra,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return ChatNoteMaster.objects.get(note_id=res.json()["noteId"])

    def share(self, note, user, role=ROLE_VIEWER):
        NotePermissionMaster.objects.create(
            team=self.team, user=user, note_type=CHAT, note_id=note.note_id, role_id=role
        )

    def soft_delete(self, channel):
        Channel.objects.filter(id=channel.id).update(is_deleted=True)

    def leave_gm(self, channel, user):
        self.authenticate(user)
        res = self.client.delete(f"/api/v3/channels/{channel.id}/members/{user.id}/")
        self.assertEqual(res.status_code, 204, res.content)

    # -- the readers, asked of one user ------------------------------------------------------- #

    def rest(self, user, method, path, **kw):
        self.authenticate(user)
        return getattr(self.client, method)(path, **kw)

    def rest_gates(self, note, user) -> dict[str, bool]:
        team = str(self.team.team_id)
        uid = str(user.id)
        q = {"team_id": team, "user_id": uid}
        single = self.rest(
            user,
            "get",
            "/api/v2/note/chat/single/",
            data={"team_id": team, "note_id": note.note_id},
        )
        meta = self.rest(user, "get", "/api/v2/note/chat/meta/", data=q)
        everything = self.rest(user, "get", "/api/v2/note/chat/all/", data=q)
        collab = self.rest(
            user,
            "post",
            "/api/v2/collab/access/check/",
            data={"document_name": f"chat-note:{note.note_id}"},
            format="json",
        )
        probe = self.rest(
            user,
            "post",
            "/api/v2/note/role/check/",
            data={"note_type": CHAT, "note_id": note.note_id},
            format="json",
        )
        self.assertEqual(meta.status_code, 200, meta.content)
        self.assertEqual(everything.status_code, 200, everything.content)
        return {
            "rest single": single.status_code == 200 and BODY in str(single.json()["body"]),
            "sidebar meta": note.note_id in {n["noteId"] for n in meta.json()},
            "sidebar all": note.note_id in {n["noteId"] for n in everything.json()},
            "collab gate": collab.status_code == 200,
            "role check": probe.status_code == 200,
        }

    def mcp_read(self, note, user) -> bool:
        self._key(scope=SCOPE_WRITE, user=user)
        text, is_error = self._tool_text(
            self._call("get_note", {"note_type": "chat", "note_id": note.note_id})
        )
        return not is_error and BODY in text

    def read_gates(self, note, user) -> dict[str, bool]:
        got = self.rest_gates(note, user)
        got["mcp get_note"] = self.mcp_read(note, user)
        agent = self.verdicts(note, user)
        # "edits to the notes I wrote" only concerns the author; asked of the author below
        agent.pop("briefing _changed_notes")
        agent.pop("has_write_permission")  # a write gate, asked by `write_gates`
        got.update(agent)
        got["readable_note_ids"] = note.note_id in readable_note_ids(user.id, CHAT, [note.note_id])
        return got

    def write_gates(self, note, user) -> dict[str, bool]:
        agent = self.verdicts(note, user)
        self.authenticate(user)
        put = self.client.put(
            "/api/v2/note/chat/",
            {
                "note_id": note.note_id,
                "title": f"by {user.username}",
                # the body stays what it was: later readers still look for it
                "body": ChatNoteMaster.objects.get(note_id=note.note_id).body,
            },
            format="json",
        )
        self._key(scope=SCOPE_WRITE, user=user)
        text, is_error = self._tool_text(
            self._call(
                "update_note",
                {"note_type": "chat", "note_id": note.note_id, "title": f"mcp {user.username}"},
            )
        )
        return {
            "rest put": put.status_code == 200,
            "mcp update_note": not is_error,
            "has_write_permission": agent["has_write_permission"],
        }

    def assertReads(self, note, user, expected: bool):
        got = self.read_gates(note, user)
        self.assertEqual(got, {k: expected for k in got}, f"{user.username}: {got}")

    def assertWrites(self, note, user, expected: bool):
        got = self.write_gates(note, user)
        self.assertEqual(got, {k: expected for k in got}, f"{user.username}: {got}")

    def assertAuthorHearsOfEdits(self, note, expected: bool):
        got = self.verdicts(note)["briefing _changed_notes"]
        self.assertEqual(got, expected)

    def chunk_acl(self, note) -> set[str]:
        out: set[str] = set()
        for batch in iter_chat_note_chunks():
            if batch.entity_id == f"note:chat:{note.note_id}":
                for c in batch.chunks:
                    out |= set(c.acl_user_ids)
        return out


# --------------------------------------------------------------------------------------------- #
# The kinds of chat built from `ChannelMember` rows: DM, multi-person DM, group chat            #
# --------------------------------------------------------------------------------------------- #


class DeletedDirectMessage(DeletedChatBase):
    def setUp(self):
        super().setUp()
        self.chat = self.kind_chat(ChannelKind.DM, self.reader, self.owner)
        self.note = self.create_note(self.reader, self.chat)

    def test_creation_wrote_an_owner_row_for_both_participants(self):
        rows = dict(
            NotePermissionMaster.objects.filter(
                note_type=CHAT, note_id=self.note.note_id
            ).values_list("user_id", "role_id")
        )
        self.assertEqual(rows, {self.reader.id: ROLE_OWNER, self.owner.id: ROLE_OWNER})

    def test_before_the_delete_everyone_in_the_chat_reads(self):
        for u in (self.reader, self.owner):
            self.assertReads(self.note, u, True)
        for u in (self.stranger, self.ex):
            self.assertReads(self.note, u, False)

    def test_after_the_delete_the_author_and_the_other_row_holder_still_read_and_write(self):
        self.soft_delete(self.chat)
        for u in (self.reader, self.owner):
            self.assertReads(self.note, u, True)
            self.assertWrites(self.note, u, True)
        self.assertAuthorHearsOfEdits(self.note, True)

    def test_after_the_delete_a_stranger_and_a_former_member_without_a_row_are_refused(self):
        # `ex` was once in the DM (a live member row) but never held a note row
        ChannelMember.objects.create(channel=self.chat, user=self.ex, is_deleted=True)
        self.soft_delete(self.chat)
        for u in (self.stranger, self.ex):
            self.assertReads(self.note, u, False)
            self.assertWrites(self.note, u, False)

    def test_a_deliberate_share_to_an_outsider_survives_the_delete_read_only(self):
        self.share(self.note, self.outsider)
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.outsider, True)
        self.assertWrites(self.note, self.outsider, False)

    def test_the_search_acl_keeps_the_row_holders_and_drops_the_chat(self):
        self.share(self.note, self.outsider)
        self.soft_delete(self.chat)
        want = {str(self.reader.id), str(self.owner.id), str(self.outsider.id)}
        self.assertEqual(self.chunk_acl(self.note), want)
        # the chat itself reads as nobody once it is deleted; the explicit rows are the whole ACL
        self.assertEqual(chat_acl_user_ids(CHAT_KIND_DM, self.chat.id), set())

    def test_a_hard_deleted_channel_leaves_the_note_with_no_channel_and_the_rows_still_read(self):
        """`ChatNoteMaster.channel` is SET_NULL: a note outlives its channel (the demo teardown
        deletes channels, a DBA can). Only the explicit rows are left to read it by."""
        self.share(self.note, self.outsider)
        Channel.objects.filter(id=self.chat.id).delete()
        self.note.refresh_from_db()
        self.assertIsNone(self.note.channel_id)
        for u in (self.reader, self.owner, self.outsider):
            self.assertReads(self.note, u, True)
        self.assertReads(self.note, self.stranger, False)
        self.assertEqual(
            self.chunk_acl(self.note),
            {str(self.reader.id), str(self.owner.id), str(self.outsider.id)},
        )

    def test_a_dm_reopened_after_a_delete_reads_by_membership_again(self):
        """No code soft-deletes a DM today; a deleted one is reopened by the same pair starting
        it again (`ChannelListView`, the DM branch of its POST), which turns `is_deleted` back off."""
        NotePermissionMaster.objects.filter(
            note_type=CHAT, note_id=self.note.note_id, user=self.owner
        ).delete()  # the other person holds no row: membership alone
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.owner, False)
        Channel.objects.filter(id=self.chat.id).update(is_deleted=False)
        self.assertReads(self.note, self.owner, True)

    def test_the_effective_role_is_the_explicit_row_not_the_implicit_editor(self):
        self.soft_delete(self.chat)
        self.assertEqual(get_effective_role(self.reader.id, CHAT, self.note.note_id), ROLE_OWNER)
        self.assertIsNone(get_effective_role(self.stranger.id, CHAT, self.note.note_id))

    def test_the_sub_notes_of_a_deleted_chats_note_open_for_a_row_holder_only(self):
        child = self.create_note(self.reader, self.chat, parent_note_id=self.note.note_id)
        self.soft_delete(self.chat)
        for user, expected in ((self.reader, True), (self.owner, True), (self.stranger, False)):
            res = self.rest(
                user,
                "get",
                "/api/v2/note/chat/subs/",
                data={
                    "team_id": str(self.team.team_id),
                    "user_id": str(user.id),
                    "note_id": self.note.note_id,
                },
            )
            ids = {n["noteId"] for n in res.json()} if res.status_code == 200 else set()
            self.assertEqual(res.status_code, 200 if expected else 403, res.content)
            self.assertEqual(child.note_id in ids, expected)


class DeletedMultiPersonDm(DeletedChatBase):
    def setUp(self):
        super().setUp()
        self.chat = self.kind_chat(ChannelKind.MDM, self.reader, self.owner, self.ex)
        self.note = self.create_note(self.reader, self.chat)

    def test_every_active_member_got_an_owner_row_and_the_row_survives_the_delete(self):
        self.soft_delete(self.chat)
        for u in (self.reader, self.owner, self.ex):
            self.assertReads(self.note, u, True)
        self.assertWrites(self.note, self.reader, True)
        self.assertReads(self.note, self.stranger, False)

    def test_a_member_who_left_before_the_delete_loses_their_row_and_stays_out(self):
        self.leave_gm(self.chat, self.ex)
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.ex, False)
        for u in (self.reader, self.owner):
            self.assertReads(self.note, u, True)
        self.assertEqual(self.chunk_acl(self.note), {str(self.reader.id), str(self.owner.id)})

    def test_a_member_added_after_the_note_was_made_has_no_row_and_the_delete_closes_it(self):
        late = self.outsider
        ChannelMember.objects.create(channel=self.chat, user=late)
        self.assertReads(self.note, late, True)  # an implicit Editor while the chat is live
        self.soft_delete(self.chat)
        self.assertReads(self.note, late, False)  # membership does not outlive the chat


class DeletedGroupChat(DeletedChatBase):
    def setUp(self):
        super().setUp()
        # `self.owner` owns the group; `self.reader` (the author) is a plain member
        self.chat = self.kind_chat(ChannelKind.GM, self.reader, self.owner, self.ex)
        self.note = self.create_note(self.reader, self.chat)

    def test_creation_wrote_owner_for_the_group_owner_and_editor_for_the_rest(self):
        rows = dict(
            NotePermissionMaster.objects.filter(
                note_type=CHAT, note_id=self.note.note_id
            ).values_list("user_id", "role_id")
        )
        self.assertEqual(
            rows,
            {self.owner.id: ROLE_OWNER, self.reader.id: ROLE_EDITOR, self.ex.id: ROLE_EDITOR},
        )

    def test_after_the_delete_every_row_holder_reads_and_writes_the_author_included(self):
        self.soft_delete(self.chat)
        for u in (self.reader, self.owner, self.ex):
            self.assertReads(self.note, u, True)
            self.assertWrites(self.note, u, True)
        self.assertAuthorHearsOfEdits(self.note, True)
        self.assertEqual(
            self.chunk_acl(self.note),
            {str(self.reader.id), str(self.owner.id), str(self.ex.id)},
        )

    def test_a_viewer_row_reads_but_never_writes_after_the_delete(self):
        NotePermissionMaster.objects.filter(
            note_type=CHAT, note_id=self.note.note_id, user=self.ex
        ).update(role_id=ROLE_VIEWER)
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.ex, True)
        self.assertWrites(self.note, self.ex, False)

    def test_a_member_who_left_lost_the_row_and_the_delete_does_not_give_it_back(self):
        self.leave_gm(self.chat, self.ex)
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.ex, False)
        self.assertWrites(self.note, self.ex, False)
        self.assertNotIn(str(self.ex.id), self.chunk_acl(self.note))

    def test_an_author_who_left_before_the_delete_is_out_and_stays_out(self):
        """MIP-1492 across a delete: the leave ended the author's row, there is no shortcut."""
        self.leave_gm(self.chat, self.reader)
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.reader, False)
        self.assertWrites(self.note, self.reader, False)
        self.assertAuthorHearsOfEdits(self.note, False)
        self.assertNotIn(str(self.reader.id), self.chunk_acl(self.note))
        self.assertReads(self.note, self.owner, True)

    def test_a_teammate_with_no_row_is_refused_as_before_the_delete(self):
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.stranger, False)
        self.assertWrites(self.note, self.stranger, False)
        gone = self.rest(
            self.stranger,
            "get",
            "/api/v2/note/chat/single/",
            data={"team_id": str(self.team.team_id), "note_id": self.note.note_id},
        )
        self.assertEqual(gone.status_code, 403)  # a teammate: "no role", as before the delete

    def test_the_legacy_author_with_no_row_at_all_reads_nothing_after_the_delete(self):
        """Reported, not decided (MIP-1505): an author whose creation wrote no row (a legacy note)
        has only the chat's live membership, which a deleted chat no longer gives."""
        NotePermissionMaster.objects.filter(
            note_type=CHAT, note_id=self.note.note_id, user=self.reader
        ).delete()
        self.assertReads(self.note, self.reader, True)  # live chat: implicit Editor
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.reader, False)


# --------------------------------------------------------------------------------------------- #
# A project's chat                                                                              #
# --------------------------------------------------------------------------------------------- #


class DeletedProjectChat(DeletedChatBase):
    def setUp(self):
        super().setUp()
        self.project = self.project_of("DEL", self.reader, self.ex)
        self.chat = self.pm_chat(self.project)
        self.note = self.create_note(self.reader, self.chat)

    def test_creation_wrote_an_owner_row_for_the_author_alone(self):
        rows = dict(
            NotePermissionMaster.objects.filter(
                note_type=CHAT, note_id=self.note.note_id
            ).values_list("user_id", "role_id")
        )
        self.assertEqual(rows, {self.reader.id: ROLE_OWNER})

    def test_a_deleted_chat_keeps_the_note_for_the_author_through_the_owner_row(self):
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.reader, True)
        self.assertWrites(self.note, self.reader, True)
        self.assertAuthorHearsOfEdits(self.note, True)
        self.assertEqual(self.chunk_acl(self.note), {str(self.reader.id)})

    def test_a_project_member_without_a_row_loses_the_note_with_the_chat(self):
        self.assertReads(self.note, self.ex, True)  # on the project: the implicit Editor
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.ex, False)
        self.assertReads(self.note, self.owner, False)

    def test_a_share_to_an_outsider_survives_and_a_stranger_stays_out(self):
        self.share(self.note, self.outsider, ROLE_EDITOR)
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.outsider, True)
        self.assertWrites(self.note, self.outsider, True)
        self.assertReads(self.note, self.stranger, False)

    def test_an_author_taken_off_the_project_before_the_delete_loses_the_note(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        self.soft_delete(self.chat)
        self.assertReads(self.note, self.reader, False)
        self.assertWrites(self.note, self.reader, False)
        self.assertAuthorHearsOfEdits(self.note, False)
        self.assertNotIn(str(self.reader.id), self.chunk_acl(self.note))

    def test_the_author_taken_off_the_project_after_the_delete_loses_the_note_too(self):
        self.soft_delete(self.chat)
        ProjectMembers.objects.filter(project=self.project, attendee=self.reader).delete()
        self.assertReads(self.note, self.reader, False)

    def test_CONFLICT_deleting_the_project_ends_the_membership_rows_but_keeps_a_share(self):
        """NOT a guarantee of this file: it records a CONFLICT with the decision (MIP-1505).

        MIP-1500 (api #759, closed) made `delete_project` end the roles the project's membership
        wrote (the author's Owner row included) BEFORE it soft-deletes the project chat, and
        pinned that. Here the author of a project chat's note loses it, which is not what a chat
        that is merely soft-deleted does (`test_a_deleted_chat_keeps_the_note_for_the_author_...`).
        The lead asks Ken which of the two stands; this test keeps
        #759's behaviour visible so a change to either side is a deliberate edit of both."""
        self.share(self.note, self.outsider, ROLE_VIEWER)
        delete_project(self.project)
        self.assertTrue(Channel.objects.get(id=self.chat.id).is_deleted)
        self.assertReads(self.note, self.outsider, True)
        self.assertReads(self.note, self.reader, False)
        self.assertReads(self.note, self.stranger, False)
        self.assertEqual(self.chunk_acl(self.note), {str(self.outsider.id)})
