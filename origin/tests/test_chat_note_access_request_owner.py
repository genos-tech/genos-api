"""MIP-1551 (1): who receives, and who may approve, a request for access to a CHAT note.

`inbox_views._note_and_owner` names the note's owner: the explicit Owner row when there is one,
else the master row's `owner` FK (a note made before the permission table was backfilled). For a
task note that fallback counts only while the author is still on the project (MIP-1511, api #774).
For a chat note it had no check at all, so an author who LEFT the chat (the leave ended their rows,
api #753) was still the receiver of every access request on the note and the only person who could
approve one, after MIP-1492 (api #766) had closed every other path they had.

The fallback now goes through the one chat-note rule (`get_effective_role`, which is
`channel_scope.chat_note_member_q` plus the explicit rows): the author counts as the owner only
while that rule still gives them a role. A note whose author left and nobody holds an Owner row has
nobody to ask: the request answers "Note not found." like a missing note (as #774 did for tasks).

Unchanged and pinned here: a Owner row always wins (a holder is the receiver whatever the chat's
state, Ken's MIP-1505 decision: a SOFT-DELETED chat's explicit-row holders and the author through
their row keep working); a legacy author who is still a member is the receiver.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.inbox_models import InboxItems
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.tests.test_chat_note_one_rule import CHAT, ChatNoteFixtures
from origin.views.common.inbox_views import _note_and_owner
from origin.views.utils.note_role import ROLE_OWNER, ROLE_VIEWER, get_effective_role

User = get_user_model()
REQUEST_URL = "/api/v2/inbox/noteAccessRequest/"
GRANT_URL = "/api/v2/note/role/fromInbox/"


class Fixtures(ChatNoteFixtures):
    """`self.reader` is the author, `self.owner` the other member of the chat, `self.user` a
    teammate who was never in it (the requester)."""

    def kind_chat(self, kind, *members):
        channel = Channel.objects.create(
            team=self.team, kind=kind, title=f"chat {kind}", owner=self.owner
        )
        for m in members:
            ChannelMember.objects.create(
                channel=channel, user=m, role="owner" if m == self.owner else "member"
            )
        return channel

    def create_note(self, author, channel):
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
                "body": [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}],
                "is_thread": False,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return ChatNoteMaster.objects.get(note_id=res.json()["noteId"])

    def leave(self, channel, user):
        """The REAL leave endpoint (ends the member's chat-note rows)."""
        self.authenticate(user)
        res = self.client.delete(f"/api/v3/channels/{channel.id}/members/{user.id}/")
        self.assertEqual(res.status_code, 204, res.content)

    def legacy(self, note):
        """A note made before the permission table existed: no row for anyone."""
        NotePermissionMaster.objects.filter(note_type=CHAT, note_id=note.note_id).delete()

    def receiver(self, note):
        return _note_and_owner(CHAT, note.note_id)[1]

    def request(self, note, user=None):
        self.authenticate(user or self.user)
        return self.client.post(
            REQUEST_URL,
            {"team_id": str(self.team.team_id), "note_type": CHAT, "note_id": note.note_id},
            format="json",
        )

    def grant(self, item, approver):
        self.authenticate(approver)
        return self.client.post(GRANT_URL, {"item_id": item.item_id}, format="json")


class LegacyNoteOfAChat(Fixtures):
    """No Owner row anywhere: the author FK is the only owner there is."""

    def setUp(self):
        super().setUp()
        self.chat = self.kind_chat(ChannelKind.GM, self.reader, self.owner)
        self.note = self.create_note(self.reader, self.chat)
        self.legacy(self.note)

    def test_a_member_author_is_still_the_receiver(self):
        self.assertEqual(str(self.receiver(self.note)), str(self.reader.id))
        res = self.request(self.note)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(str(InboxItems.objects.get(item_type=4).receiver_id), str(self.reader.id))

    def test_a_member_author_can_approve(self):
        item = InboxItems.objects.get(pk=self.request(self.note).data["data"]["itemId"])
        res = self.grant(item, self.reader)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(get_effective_role(self.user.id, CHAT, self.note.note_id), ROLE_VIEWER)

    def test_an_author_who_left_is_nobody_to_ask(self):
        ChannelMember.objects.filter(channel=self.chat, user=self.reader).update(is_deleted=True)
        self.assertIsNone(self.receiver(self.note))

    def test_a_stranger_asking_after_the_author_left_gets_note_not_found(self):
        ChannelMember.objects.filter(channel=self.chat, user=self.reader).update(is_deleted=True)
        res = self.request(self.note)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertEqual(res.json(), {"error": "Note not found."})
        self.assertFalse(InboxItems.objects.filter(item_type=4).exists())

    def test_the_same_answer_as_a_note_that_does_not_exist(self):
        ChannelMember.objects.filter(channel=self.chat, user=self.reader).update(is_deleted=True)
        gone = self.request(self.note)
        ChatNoteMaster.objects.filter(pk=self.note.pk).delete()
        missing = self.request(self.note)
        self.assertEqual((gone.status_code, gone.json()), (missing.status_code, missing.json()))

    def test_a_request_filed_while_a_member_cannot_be_approved_by_the_author_who_left(self):
        item = InboxItems.objects.get(pk=self.request(self.note).data["data"]["itemId"])
        ChannelMember.objects.filter(channel=self.chat, user=self.reader).update(is_deleted=True)
        res = self.grant(item, self.reader)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertIsNone(get_effective_role(self.user.id, CHAT, self.note.note_id))
        item.refresh_from_db()
        self.assertEqual(item.request_status, "pending")

    def test_a_project_chat_author_taken_off_the_project_is_nobody_to_ask(self):
        project = self.project_of("CHN", self.reader)
        pm = self.pm_chat(project)
        note = self.note_on(pm, owner=self.reader)  # no rows, as a legacy note
        self.assertEqual(str(self.receiver(note)), str(self.reader.id))
        ProjectMembers.objects.filter(project=project, attendee=self.reader).delete()
        self.assertIsNone(self.receiver(note))
        self.assertEqual(self.request(note).status_code, 404)


class NoteWithOwnerRows(Fixtures):
    def setUp(self):
        super().setUp()
        self.chat = self.kind_chat(ChannelKind.GM, self.reader, self.owner)
        self.note = self.create_note(self.reader, self.chat)

    def test_the_owner_row_wins_even_with_the_author_gone(self):
        """The GM's owner holds the Owner row (the author an Editor row): the leave changes
        nothing about who is asked."""
        self.leave(self.chat, self.reader)
        self.assertEqual(str(self.receiver(self.note)), str(self.owner.id))
        res = self.request(self.note)
        self.assertEqual(res.status_code, 201, res.content)

    def test_an_author_who_left_and_holds_a_row_by_a_new_share_is_the_receiver(self):
        """A deliberate share is a row, and a row wins as everywhere (MIP-1505)."""
        NotePermissionMaster.objects.filter(note_type=CHAT, note_id=self.note.note_id).delete()
        self.leave(self.chat, self.reader)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.reader,
            note_type=CHAT,
            note_id=self.note.note_id,
            role_id=ROLE_OWNER,
        )
        self.assertEqual(str(self.receiver(self.note)), str(self.reader.id))

    def test_the_author_who_left_with_nobody_holding_an_owner_row_is_nobody_to_ask(self):
        NotePermissionMaster.objects.filter(note_type=CHAT, note_id=self.note.note_id).delete()
        self.leave(self.chat, self.reader)
        self.assertIsNone(self.receiver(self.note))
        self.assertEqual(self.request(self.note).status_code, 404)


class NoteOfASoftDeletedChat(Fixtures):
    """Ken's MIP-1505 decision: the row wins, the channel's liveness is not asked."""

    def setUp(self):
        super().setUp()
        self.chat = self.kind_chat(ChannelKind.DM, self.reader, self.owner)
        self.note = self.create_note(self.reader, self.chat)  # DM: both hold an Owner row
        Channel.objects.filter(id=self.chat.id).update(is_deleted=True)

    def test_the_author_through_their_owner_row_is_still_the_receiver_or_the_other_holder(self):
        owners = {
            str(u)
            for u in NotePermissionMaster.objects.filter(
                note_type=CHAT, note_id=self.note.note_id, role_id=ROLE_OWNER
            ).values_list("user_id", flat=True)
        }
        self.assertEqual(owners, {str(self.reader.id), str(self.owner.id)})
        self.assertIn(str(self.receiver(self.note)), owners)
        self.assertEqual(self.request(self.note).status_code, 201)

    def test_the_author_alone_with_a_row_is_the_receiver(self):
        NotePermissionMaster.objects.filter(
            note_type=CHAT, note_id=self.note.note_id, user=self.owner
        ).delete()
        self.assertEqual(str(self.receiver(self.note)), str(self.reader.id))
        item = InboxItems.objects.get(pk=self.request(self.note).data["data"]["itemId"])
        self.assertEqual(self.grant(item, self.reader).status_code, 201)

    def test_a_legacy_note_with_no_row_is_nobody_to_ask_once_its_chat_is_deleted(self):
        """No row, and membership counts only while the channel is live: no role, no routing."""
        self.legacy(self.note)
        self.assertIsNone(self.receiver(self.note))
