"""MIP-1497: the two note permission probes answer a note you cannot read like a note that does
not exist.

`POST /api/v2/note/role/check/` and `POST /api/v2/collab/access/check/` answered 404 for a note id
that is not there and 403 for one that is, so any signed-in user could walk the id space and
learn which notes (and, for `task-body:`, which tasks) exist in other teams. Both now answer the
same 404, with the same body, for "missing" and "exists but you may not read it".

Left alone on purpose, and pinned below: the REST note readers behind `require_read_role` keep
their 403. The frontend's access-request panel (`NoteOpenFailure`) is the shared-URL flow and
reads that 403 as "the note exists and you have no role on it".
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
ROLE_CHECK = "/api/v2/note/role/check/"
COLLAB_CHECK = "/api/v2/collab/access/check/"
MISSING = 99999999


class Probes(BaseAPITestCase):
    """`self.user` owns every note; `self.outsider` is a stranger on another team."""

    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="probe", email="probe@example.com", password="x"
        )
        other = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=other, attendee=self.outsider)
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open"
        )
        self.personal = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="mine", body=[]
        )
        # a personal note opens through its Owner row (the real writer adds it)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.personal.note_id,
            note_type=1,
            role_id=1,
        )
        self.task_note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=self.project, task=self.task, title="tn"
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

    def role_check(self, user, note_type, note_id):
        self.authenticate(user)
        return self.client.post(
            ROLE_CHECK, {"note_type": note_type, "note_id": note_id}, format="json"
        )

    def collab_check(self, user, document_name):
        self.authenticate(user)
        return self.client.post(COLLAB_CHECK, {"document_name": document_name}, format="json")

    def assertSameAnswer(self, existing, missing):
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(existing.status_code, 404, existing.content)
        self.assertEqual(existing.json(), missing.json())


class RoleCheck(Probes):
    def test_an_unreadable_note_answers_like_a_missing_one(self):
        for note_type, note in ((1, self.personal), (2, self.task_note), (3, self.chat_note)):
            with self.subTest(note_type=note_type):
                self.assertSameAnswer(
                    self.role_check(self.outsider, note_type, note.note_id),
                    self.role_check(self.outsider, note_type, MISSING),
                )

    def test_a_reader_still_gets_their_role(self):
        for note_type, note in ((1, self.personal), (2, self.task_note), (3, self.chat_note)):
            with self.subTest(note_type=note_type):
                res = self.role_check(self.user, note_type, note.note_id)
                self.assertEqual(res.status_code, 200, res.content)
                self.assertIn("role_id", res.json())


class CollabCheck(Probes):
    def test_an_unreadable_note_answers_like_a_missing_one(self):
        for prefix, note in (
            ("my-note", self.personal),
            ("task-note", self.task_note),
            ("chat-note", self.chat_note),
        ):
            with self.subTest(prefix=prefix):
                self.assertSameAnswer(
                    self.collab_check(self.outsider, f"{prefix}:{note.note_id}"),
                    self.collab_check(self.outsider, f"{prefix}:{MISSING}"),
                )

    def test_a_task_body_you_cannot_open_answers_like_a_missing_task(self):
        self.assertSameAnswer(
            self.collab_check(self.outsider, f"task-body:{self.task.task_id}"),
            self.collab_check(self.outsider, f"task-body:{MISSING}"),
        )

    def test_a_reader_still_gets_in(self):
        for name in (
            f"my-note:{self.personal.note_id}",
            f"task-note:{self.task_note.note_id}",
            f"chat-note:{self.chat_note.note_id}",
            f"task-body:{self.task.task_id}",
        ):
            with self.subTest(name=name):
                res = self.collab_check(self.user, name)
                self.assertEqual(res.status_code, 200, res.content)

    def test_an_unknown_surface_is_still_refused(self):
        self.assertEqual(self.collab_check(self.user, "some-future-surface:1").status_code, 403)


class RestReadersKeepTheir403(Probes):
    """The access-request panel reads this 403 (`NoteOpenFailure`): not part of this change."""

    def test_the_single_note_reader_still_says_403_for_an_existing_unreadable_note(self):
        self.authenticate(self.outsider)
        res = self.client.get(
            "/api/v2/note/personal/single/",
            {"team_id": str(self.team.team_id), "note_id": self.personal.note_id},
        )
        self.assertEqual(res.status_code, 403)
