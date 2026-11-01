"""MIP-838: creating a chat note requires being in the conversation.

`POST /api/v2/note/chat/` used to accept any channel UUID and stored the
client's `chat_type` as given, so anyone in the team could plant a note in
a DM they weren't part of, and a spoofed kind skewed the role fan-out.
"""

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.team_models import TeamMaster
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER

URL = "/api/v2/note/chat/"


class ChatNoteCreateAclTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.carol = get_user_model().objects.create_user(
            username="carol", email="carol@example.com", password="x"
        )
        # A DM between user and carol; user2 is in the team but not the DM.
        self.dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        ChannelMember.objects.create(channel=self.dm, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.dm, user=self.carol, role="member")
        # A GM owned by carol with user and user2 as members.
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="GM", owner=self.carol
        )
        for u, role in ((self.carol, "owner"), (self.user, "member"), (self.user2, "member")):
            ChannelMember.objects.create(channel=self.gm, user=u, role=role)

    def _post(self, user, channel, **extra):
        self.authenticate(user)
        payload = {
            "team_id": self.team.team_id,
            "user_id": str(user.id),
            "chat_type": int(channel.kind),
            "chat_id": str(channel.id),
            "title": "note",
            "body": [],
            "is_thread": False,
            **extra,
        }
        return self.client.post(URL, payload, format="json")

    def _roles(self, channel):
        note_ids = ChatNoteMaster.objects.filter(channel=channel).values_list("note_id", flat=True)
        return {
            str(uid): role
            for uid, role in NotePermissionMaster.objects.filter(
                note_type=3, note_id__in=list(note_ids)
            ).values_list("user_id", "role_id")
        }

    def test_member_creates_a_dm_note(self):
        res = self._post(self.user, self.dm)
        self.assertEqual(res.status_code, 201)
        self.assertEqual(
            self._roles(self.dm), {str(self.user.id): ROLE_OWNER, str(self.carol.id): ROLE_OWNER}
        )

    def test_non_member_cannot_create_in_someone_elses_dm(self):
        res = self._post(self.user2, self.dm)
        self.assertEqual(res.status_code, 404)
        self.assertFalse(ChatNoteMaster.objects.filter(channel=self.dm).exists())
        self.assertFalse(NotePermissionMaster.objects.filter(note_type=3).exists())

    def test_member_who_left_cannot_create(self):
        ChannelMember.objects.filter(channel=self.gm, user=self.user2).update(is_deleted=True)
        res = self._post(self.user2, self.gm)
        self.assertEqual(res.status_code, 404)
        self.assertFalse(ChatNoteMaster.objects.filter(channel=self.gm).exists())

    def test_deleted_channel_is_refused(self):
        Channel.objects.filter(id=self.dm.id).update(is_deleted=True)
        res = self._post(self.user, self.dm)
        self.assertEqual(res.status_code, 404)

    def test_membership_decides_not_the_channels_team(self):
        # A channel shared from another team: user is a guest member there
        # (allowed), user2 isn't (refused), whatever team the request names.
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.carol
        )
        Channel.objects.filter(id=self.dm.id).update(team=other)
        self.assertEqual(self._post(self.user2, self.dm).status_code, 404)
        self.assertEqual(self._post(self.user, self.dm).status_code, 201)

    def test_malformed_channel_id_is_404(self):
        self.authenticate(self.user)
        res = self.client.post(
            URL,
            {
                "team_id": self.team.team_id,
                "user_id": str(self.user.id),
                "chat_type": 1,
                "chat_id": "not-a-uuid",
                "title": "n",
                "body": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_chat_type_comes_from_the_channel_not_the_request(self):
        # A GM member claims "DM" to make every member an OWNER.
        res = self._post(self.user2, self.gm, chat_type=int(ChannelKind.DM))
        self.assertEqual(res.status_code, 201)
        note = ChatNoteMaster.objects.get(channel=self.gm)
        self.assertEqual(note.chat_type, int(ChannelKind.GM))
        self.assertEqual(
            self._roles(self.gm),
            {
                str(self.carol.id): ROLE_OWNER,
                str(self.user.id): ROLE_EDITOR,
                str(self.user2.id): ROLE_EDITOR,
            },
        )

    def test_request_without_chat_type_still_works(self):
        self.authenticate(self.user)
        res = self.client.post(
            URL,
            {
                "team_id": self.team.team_id,
                "user_id": str(self.user.id),
                "chat_id": str(self.dm.id),
                "title": "n",
                "body": [],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(ChatNoteMaster.objects.get().chat_type, int(ChannelKind.DM))

    def test_thread_note_needs_a_root_in_the_same_channel(self):
        root_elsewhere = Message.objects.create(
            channel=self.gm, sender=self.user, seq=1, body={}, body_text="x"
        )
        res = self._post(self.user, self.dm, is_thread=True, thread_id=str(root_elsewhere.id))
        self.assertEqual(res.status_code, 404)
        self.assertFalse(ChatNoteMaster.objects.exists())

        root = Message.objects.create(
            channel=self.dm, sender=self.user, seq=1, body={}, body_text="root"
        )
        res = self._post(self.user, self.dm, is_thread=True, thread_id=str(root.id))
        self.assertEqual(res.status_code, 201)
        note = ChatNoteMaster.objects.get()
        self.assertTrue(note.is_thread)
        self.assertEqual(str(note.thread_root_id), str(root.id))
