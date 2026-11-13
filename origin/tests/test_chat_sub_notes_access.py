"""MIP-1483: `GET /api/v2/note/chat/subs/` answers per CHILD, not per parent.

The view used to check only the parent note's role and then return every child's title and
body. A child can leave its parent's chat (`ChatNoteMoveView` on the child alone leaves
`parent_note_id` set), so someone who could read the parent was shown a child in a chat they
are not in. Each child is now held to the one chat-note rule (`get_effective_role`, batched by
`readable_note_ids`): an explicit grant, or membership of the child's OWN chat. A child they
may not read is simply absent, the same answer as a child that does not exist.
"""

from __future__ import annotations

from origin.models.chat.unified_models import ChannelKind
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.tests.test_chat_note_one_rule import ChatNoteFixtures
from origin.views.utils.note_role import ROLE_VIEWER

CHAT = 3


class ChatSubNotesAccess(ChatNoteFixtures):
    def setUp(self):
        super().setUp()
        self.shared = self.gm(self.reader, self.owner)  # the reader is in this chat
        self.private = self.gm(self.owner)  # the reader is not
        self.parent = self.note_on(self.shared, title="Parent")

    def child(self, channel, title):
        note = self.note_on(channel, title=title)
        note.parent_note_id = self.parent.note_id
        note.save(update_fields=["parent_note_id"])
        return note

    def subs(self, user=None, note=None):
        user = user or self.reader
        self.authenticate(user)
        return self.client.get(
            "/api/v2/note/chat/subs/",
            {
                "team_id": str(self.team.team_id),
                "user_id": str(user.id),
                "note_id": (note or self.parent).note_id,
            },
        )

    def test_a_child_in_the_readers_chat_is_listed(self):
        kept = self.child(self.shared, "Kept child")
        res = self.subs()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([n["noteId"] for n in res.json()], [kept.note_id])

    def test_a_child_in_a_chat_the_reader_is_not_in_is_not_returned(self):
        kept = self.child(self.shared, "Kept child")
        hidden = self.child(self.private, "SECRET-CHILD-TITLE")
        hidden.body = [{"type": "paragraph", "content": [{"type": "text", "text": "HIDDEN-BODY"}]}]
        hidden.save(update_fields=["body"])
        res = self.subs()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual([n["noteId"] for n in res.json()], [kept.note_id])
        self.assertNotIn(hidden.title, res.content.decode())
        self.assertNotIn("HIDDEN-BODY", res.content.decode())
        # the probe's premise: the reader really has no role on the child
        from origin.views.utils.note_role import get_effective_role

        self.assertIsNone(get_effective_role(self.reader.id, CHAT, hidden.note_id))

    def test_a_child_moved_through_the_move_endpoint_stops_being_listed(self):
        """The reported route: the owner moves ONE child to a chat the reader is not in."""
        child = self.child(self.shared, "Moved child")
        self.assertEqual(len(self.subs().json()), 1)
        self.authenticate(self.owner)
        moved = self.client.put(
            "/api/v2/note/chat/move/",
            {
                "team_id": self.team.team_id,
                "user_id": str(self.owner.id),
                "note_id": child.note_id,
                "chat_type": int(ChannelKind.GM),
                "channel_id": str(self.private.id),
            },
            format="json",
        )
        self.assertEqual(moved.status_code, 200, moved.content)
        child.refresh_from_db()
        self.assertEqual(child.parent_note_id, self.parent.note_id)  # still pointing at it
        res = self.subs()
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json(), [])

    def test_an_explicit_grant_on_a_child_in_another_chat_still_lists_it(self):
        granted = self.child(self.private, "Granted child")
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.reader,
            note_type=CHAT,
            note_id=granted.note_id,
            role_id=ROLE_VIEWER,
        )
        res = self.subs()
        self.assertEqual([n["noteId"] for n in res.json()], [granted.note_id])

    def test_the_owner_of_every_chat_sees_every_child(self):
        a = self.child(self.shared, "A")
        b = self.child(self.private, "B")
        res = self.subs(user=self.owner)
        self.assertEqual(sorted(n["noteId"] for n in res.json()), sorted([a.note_id, b.note_id]))

    def test_no_access_to_the_parent_is_still_refused(self):
        self.child(self.shared, "Kept child")
        res = self.subs(note=self.note_on(self.private, title="Private parent"))
        self.assertEqual(res.status_code, 403)

    def test_a_child_whose_chat_type_differs_from_its_channels_kind_is_not_listed(self):
        child = self.child(self.shared, "Mismatched")
        ChatNoteMaster.objects.filter(note_id=child.note_id).update(chat_type=int(ChannelKind.PM))
        # chat_type differing from the channel's kind: refused by the rule, so not listed
        self.assertEqual(self.subs().json(), [])
