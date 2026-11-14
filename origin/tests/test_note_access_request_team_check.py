"""`POST /api/v2/inbox/noteAccessRequest/` answers a stranger like it answers a missing note.

The route files "please let me in" for a note the caller has no role on. It checked that the note
lives in the team named in the body, but never that the CALLER belongs to that team. So anyone
signed in who knows a team id (they are in the invite link, the URL, the socket query) could post
note ids and tell the answers apart: 404 "Note not found." for an id that is not there, 400 "You
already have access" or a filed request (201) for one that is. A note id in another team could be
confirmed to exist, and a request addressed to its owner could be forced into their inbox.

The request is for a person who is ON the team but has no role on the note (the shared-URL case:
`NoteAccessRequestPanel` emits the `note_access_request` socket event, genos-sockets forwards it
here with the team of the connection). That person is unchanged; a person off the team gets the
404 a missing note gets, byte for byte, and nothing is filed.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
URL = "/api/v2/inbox/noteAccessRequest/"
MISSING = 99999999
ROLE_OWNER = 1
ROLE_VIEWER = 3


class Base(BaseAPITestCase):
    """`self.user` owns the team and every note; `self.user2` is a teammate with no role on any of
    them; `self.outsider` belongs only to another team."""

    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(team=self.team, project=project, title="t", status="Open")
        self.personal = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="mine", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.personal.note_id,
            note_type=1,
            role_id=ROLE_OWNER,
        )
        self.task_note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=project, task=task, title="tn"
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.task_note.note_id,
            note_type=2,
            role_id=ROLE_OWNER,
        )
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gm", owner=self.user
        )
        ChannelMember.objects.create(channel=channel, user=self.user, role="owner")
        self.chat_note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=channel.kind,
            channel=channel,
            is_thread=False,
            title="cn",
            body=[],
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.chat_note.note_id,
            note_type=3,
            role_id=ROLE_OWNER,
        )
        self.notes = ((1, self.personal), (2, self.task_note), (3, self.chat_note))

    def ask(self, user, team, note_type, note_id):
        self.authenticate(user)
        return self.client.post(
            URL,
            {"team_id": str(team.team_id), "note_type": note_type, "note_id": note_id},
            format="json",
        )


class AStrangerToTheTeamLearnsNothing(Base):
    def test_knowing_the_team_id_does_not_tell_a_note_from_a_missing_one(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                said = self.ask(self.outsider, self.team, note_type, note.note_id)
                missing = self.ask(self.outsider, self.team, note_type, MISSING)
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(said.status_code, 404, said.content)
                self.assertEqual(said.json(), missing.json())

    def test_nothing_is_filed_for_the_owner(self):
        for note_type, note in self.notes:
            self.ask(self.outsider, self.team, note_type, note.note_id)
        self.assertFalse(InboxItems.objects.filter(sender=self.outsider).exists())

    def test_naming_their_own_team_still_answers_the_same(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                said = self.ask(self.outsider, self.other_team, note_type, note.note_id)
                missing = self.ask(self.outsider, self.other_team, note_type, MISSING)
                self.assertEqual(
                    (said.status_code, said.json()), (missing.status_code, missing.json())
                )

    def test_a_member_who_left_is_a_stranger_too(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        said = self.ask(self.user2, self.team, 1, self.personal.note_id)
        missing = self.ask(self.user2, self.team, 1, MISSING)
        self.assertEqual(said.status_code, 404)
        self.assertEqual(said.json(), missing.json())
        self.assertFalse(InboxItems.objects.filter(sender=self.user2).exists())


class ATeammateCanStillRequestAccess(Base):
    def test_a_teammate_with_no_role_files_the_request_for_the_owner(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                res = self.ask(self.user2, self.team, note_type, note.note_id)
                self.assertEqual(res.status_code, 201, res.content)
                self.assertFalse(res.json()["alreadyExist"])
                self.assertEqual(res.json()["receiver"], str(self.user.id))
                item = InboxItems.objects.get(
                    sender=self.user2,
                    item_optionals__note_type=note_type,
                    item_optionals__note_id=note.note_id,
                )
                self.assertEqual(str(item.receiver_id), str(self.user.id))
                self.assertEqual(item.item_type, 4)

    def test_asking_twice_files_one_request(self):
        first = self.ask(self.user2, self.team, 1, self.personal.note_id)
        second = self.ask(self.user2, self.team, 1, self.personal.note_id)
        self.assertEqual((first.status_code, second.status_code), (201, 201))
        self.assertTrue(second.json()["alreadyExist"])
        self.assertEqual(InboxItems.objects.filter(sender=self.user2).count(), 1)

    def test_a_teammate_who_can_already_read_it_is_told_so(self):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=self.personal.note_id,
            note_type=1,
            role_id=ROLE_VIEWER,
        )
        res = self.ask(self.user2, self.team, 1, self.personal.note_id)
        self.assertEqual(res.status_code, 400, res.content)
        self.assertEqual(res.json(), {"error": "You already have access to this note."})

    def test_a_teammate_asking_for_a_missing_note_gets_the_404(self):
        res = self.ask(self.user2, self.team, 1, MISSING)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {"error": "Note not found."})

    def test_a_teammate_naming_another_team_gets_the_404(self):
        res = self.ask(self.user2, self.other_team, 1, self.personal.note_id)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {"error": "Note not found."})
