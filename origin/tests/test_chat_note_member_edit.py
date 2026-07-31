"""A chat note must be editable by the people in that chat.

Chat members used to resolve to Viewer while writes require Editor, so a
note started in a DM 403'd for the other participant on every autosave —
systematically, not just for stale rows. These pin the fix and the
boundaries either side of it.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import (
    ROLE_EDITOR,
    ROLE_VIEWER,
    get_effective_role,
    require_write_role,
)

NOTE_TYPE_CHAT = 3


class ChatNoteMemberEditTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="DM")
        ChannelMember.objects.create(channel=self.dm, user=self.user)
        ChannelMember.objects.create(channel=self.dm, user=self.user2)
        # Created by `user`; `user2` is the other DM participant.
        self.note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="From the DM",
            body=[],
            chat_type=1,
            channel=self.dm,
            is_thread=False,
        )

    def test_other_chat_member_may_edit(self):
        self.assertEqual(
            get_effective_role(self.user2.id, NOTE_TYPE_CHAT, self.note.note_id), ROLE_EDITOR
        )

    def test_other_chat_member_passes_the_write_gate(self):
        self.assertIsNone(require_write_role(self.user2.id, NOTE_TYPE_CHAT, self.note.note_id))

    def test_non_member_still_denied(self):
        ChannelMember.objects.filter(channel=self.dm, user=self.user2).delete()
        self.assertIsNone(get_effective_role(self.user2.id, NOTE_TYPE_CHAT, self.note.note_id))
        self.assertIsNotNone(
            require_write_role(self.user2.id, NOTE_TYPE_CHAT, self.note.note_id)
        )

    def test_explicit_viewer_grant_still_wins(self):
        """An explicit row must keep overriding the implicit role, or
        deliberately pinning someone to read-only stops working."""
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=self.note.note_id,
            note_type=NOTE_TYPE_CHAT,
            role_id=ROLE_VIEWER,
        )
        self.assertEqual(
            get_effective_role(self.user2.id, NOTE_TYPE_CHAT, self.note.note_id), ROLE_VIEWER
        )
