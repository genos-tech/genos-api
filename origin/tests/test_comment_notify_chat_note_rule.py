"""MIP-1484: the surface-activity routes hold a chat note to the ONE chat-note rule.

`POST /api/v3/activities/surface/` (mentions) and `/surface-comment/` (comment fan-out) with
`surface_type=8` (a chat note) used to need only team membership, and the comment fan-out took
its recipients from the raw channel-member rows. So a team member who cannot read the note got
back the owner's and members' user ids (a roster), a note id that does not exist got an empty
list (an existence oracle), and a stray row on a project chat or a member of a deleted channel
was notified about a note they cannot open.

Now: the caller must be able to read the note (`get_effective_role`), and a note the caller
cannot read answers exactly as a note that does not exist (404). The recipients are the people
who can read the note: its chat's readers by `chat_note_member_q`'s set form
(`chat_acl_user_ids`), plus explicit grants.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Activity, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.tests.test_chat_note_one_rule import ChatNoteFixtures
from origin.views.utils.note_role import ROLE_VIEWER

User = get_user_model()
SURFACE_CHAT_NOTE = 8
COMMENT_URL = "/api/v3/activities/surface-comment/"
MENTION_URL = "/api/v3/activities/surface/"


class CommentNotifyChatNoteRule(ChatNoteFixtures):
    def setUp(self):
        super().setUp()
        self.stray = User.objects.create_user(
            username="stray", email="stray@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.stray)
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.outsider)

    def comment(self, user, note_id, mentioned=()):
        self.authenticate(user)
        return self.client.post(
            COMMENT_URL,
            {
                "surface_type": SURFACE_CHAT_NOTE,
                "team_id": str(self.team.team_id),
                "entity_key": f"comment-participant:{note_id}:x",
                "meta": {"noteId": note_id, "noteType": 3},
                "mentioned_user_ids": [str(u) for u in mentioned],
            },
            format="json",
        )

    def mention(self, user, note_id, targets):
        self.authenticate(user)
        return self.client.post(
            MENTION_URL,
            {
                "surface_type": SURFACE_CHAT_NOTE,
                "team_id": str(self.team.team_id),
                "entity_key": f"note:3:{note_id}",
                "newly_mentioned_user_ids": [str(u) for u in targets],
                "meta": {"noteId": note_id, "noteType": 3},
            },
            format="json",
        )

    @staticmethod
    def recipients(res):
        return {a["recipientUserId"] for a in res.json()["activities"]}

    # -- the caller ---------------------------------------------------------------------- #

    def test_a_team_member_who_cannot_read_the_note_is_refused_like_a_missing_note(self):
        note = self.note_on(self.gm(self.owner, self.reader))
        refused = self.comment(self.outsider, note.note_id)
        missing = self.comment(self.outsider, 999_999)
        self.assertEqual(refused.status_code, 404, refused.content)
        self.assertEqual(missing.status_code, 404, missing.content)
        self.assertEqual(refused.json(), missing.json())
        self.assertNotIn(str(self.owner.id), refused.content.decode())
        self.assertEqual(Activity.objects.filter(surface_type=SURFACE_CHAT_NOTE).count(), 0)

    def test_the_mention_route_refuses_a_non_reader_too_and_writes_nothing(self):
        note = self.note_on(self.gm(self.owner, self.reader))
        refused = self.mention(self.outsider, note.note_id, [self.reader.id])
        missing = self.mention(self.outsider, 999_999, [self.reader.id])
        self.assertEqual((refused.status_code, missing.status_code), (404, 404))
        self.assertEqual(refused.json(), missing.json())
        self.assertEqual(Activity.objects.filter(surface_type=SURFACE_CHAT_NOTE).count(), 0)

    def test_a_reader_gets_the_fan_out_and_the_mention(self):
        note = self.note_on(self.gm(self.owner, self.reader, self.stray))
        res = self.comment(self.reader, note.note_id)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), {str(self.owner.id), str(self.stray.id)})
        res = self.mention(self.reader, note.note_id, [self.stray.id])
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), {str(self.stray.id)})

    # -- the recipients ------------------------------------------------------------------ #

    def test_a_stray_row_on_a_project_chat_is_not_notified(self):
        project = self.project_of("CNR", self.reader)
        chat = self.pm_chat(project)
        ChannelMember.objects.update_or_create(
            channel=chat, user=self.stray, defaults={"is_deleted": False}
        )
        note = self.note_on(chat)
        res = self.comment(self.reader, note.note_id)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), {str(self.owner.id)})

    def test_a_member_of_a_deleted_channel_is_not_notified(self):
        chat = self.gm(self.owner, self.reader, self.stray)
        note = self.note_on(chat)
        chat.is_deleted = True
        chat.save(update_fields=["is_deleted"])
        # the owner holds the note's own explicit row; nobody else can open it
        NotePermissionMaster.objects.create(
            team=self.team, user=self.owner, note_type=3, note_id=note.note_id, role_id=1
        )
        res = self.comment(self.owner, note.note_id)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), set())

    def test_a_note_whose_chat_type_differs_from_its_channels_kind_notifies_no_member(self):
        chat = self.gm(self.owner, self.reader, self.stray)
        note = self.note_on(chat, chat_type=int(ChannelKind.PM))
        NotePermissionMaster.objects.create(
            team=self.team, user=self.owner, note_type=3, note_id=note.note_id, role_id=1
        )
        res = self.comment(self.owner, note.note_id)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), set())

    def test_an_explicit_grantee_outside_the_chat_is_notified(self):
        note = self.note_on(self.gm(self.owner, self.reader))
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.outsider,
            note_type=3,
            note_id=note.note_id,
            role_id=ROLE_VIEWER,
        )
        res = self.comment(self.reader, note.note_id)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(self.recipients(res), {str(self.owner.id), str(self.outsider.id)})

    def test_a_task_body_comment_is_unchanged(self):
        """Only chat notes are gated here: the task-body surface keeps its own recipients."""
        self.authenticate(self.reader)
        res = self.client.post(
            COMMENT_URL,
            {
                "surface_type": 5,
                "team_id": str(self.team.team_id),
                "entity_key": "comment-participant:t",
                "meta": {"taskId": self.task.task_id},
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
