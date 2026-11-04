"""A note's autosave PUT changes its title and body, nothing else (MIP-1018).

The whole request reached a partial `fields = "__all__"` serializer, so
anybody who could write a note could also take its `owner`, re-file it
(`folder_id`, `parent_note_id`) or point a task note at another project or a
chat note at another channel, past the move endpoints' checks.

`team` is not in the forged lists below: `AuthenticatedAPIView` rewrites a
`team`/`team_id` in the body to the object's real team before the view runs
(`foreign_team_scope`), so it cannot be forged on these routes. The guard still
lists it, for a caller that bypasses that step.

The app's autosave sends `user_id`, `note_id`, `title` and `body`; that body is
the contract. A note's own value echoed back is accepted and unused; any
other is a 400 and nothing is written.
"""

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

PERSONAL = "/api/v2/note/personal/"
TASK = "/api/v2/note/task/"
CHAT = "/api/v2/note/chat/"
STRANGER_UUID = "11111111-1111-4111-8111-111111111111"


class NotePutForgedFieldTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            owner=self.user,
            project_system_user=self.user,
            code="PP",
        )
        self.other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Q",
            owner=self.user,
            project_system_user=self.user,
            code="QQ",
        )
        for who in (self.user, self.user2):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=who)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )

    def _fields(self, note, names):
        return {name: note.serializable_value(name) for name in names}

    def _check_refused(self, url, note, names, forged):
        """Every forged value 400s and the note's columns stay what they were."""
        before = self._fields(note, names + ["title", "body"])
        for field, value in forged.items():
            with self.subTest(url=url, field=field):
                res = self.client.put(
                    url,
                    {"note_id": note.note_id, "title": "changed", "body": [], field: value},
                    format="json",
                )
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data, {"error": f"{field} cannot be set."})
                note.refresh_from_db()
                self.assertEqual(self._fields(note, names + ["title", "body"]), before)

    # ── personal ──────────────────────────────────────────────────────

    def _personal(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", body=[], folder_id=None
        )
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=note.note_id, note_type=1, role_id=1
        )
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user2, note_id=note.note_id, note_type=1, role_id=2
        )
        return note

    def test_personal_editor_cannot_take_or_refile_the_note(self):
        note = self._personal()
        self.authenticate(self.user2)
        self._check_refused(
            PERSONAL,
            note,
            ["team", "owner", "folder_id", "parent_note_id", "mentioned_user_ids"],
            {
                "owner": str(self.user2.id),
                "folder_id": 12345,
                "parent_note_id": 99,
                "mentioned_user_ids": [str(self.user2.id)],
            },
        )

    def test_personal_apps_exact_autosave_body_works(self):
        note = self._personal()
        self.authenticate(self.user2)
        res = self.client.put(
            PERSONAL,
            {"user_id": str(self.user2.id), "note_id": note.note_id, "title": "new", "body": []},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        note.refresh_from_db()
        self.assertEqual(note.title, "new")
        self.assertEqual(note.owner_id, self.user.id)
        for key in ("newly_mentioned_user_ids", "all_mentioned_user_ids", "removed_user_ids"):
            self.assertIn(key, res.data)

    def test_personal_the_notes_own_values_echoed_back_are_accepted(self):
        note = self._personal()
        self.authenticate(self.user2)
        res = self.client.put(
            PERSONAL,
            {
                "note_id": note.note_id,
                "title": "echo",
                "body": [],
                "owner": str(self.user.id).upper(),
                "team": str(self.team.team_id),
                "folder_id": None,
                "parent_note_id": None,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        note.refresh_from_db()
        self.assertEqual((note.title, note.owner_id), ("echo", self.user.id))

    # ── task ──────────────────────────────────────────────────────────

    def test_task_note_writer_cannot_take_or_move_the_note(self):
        note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            owner=self.user,
            title="n",
            body=[],
        )
        self.authenticate(self.user2)  # a project member: implicit editor
        other_task = TaskMaster.objects.create(
            team=self.team, project=self.other_project, title="O", status="Open"
        )
        self._check_refused(
            TASK,
            note,
            ["team", "project", "owner", "task", "parent_note_id", "mentioned_user_ids"],
            {
                "owner": str(self.user2.id),
                "project": self.other_project.project_id,
                "task": other_task.task_id,
                "parent_note_id": 99,
                "mentioned_user_ids": [str(self.user2.id)],
            },
        )

    def test_task_apps_exact_autosave_body_works(self):
        note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            owner=self.user,
            title="n",
            body=[],
        )
        self.authenticate(self.user2)
        res = self.client.put(
            TASK,
            {"user_id": str(self.user2.id), "note_id": note.note_id, "title": "new", "body": []},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        note.refresh_from_db()
        self.assertEqual(
            (note.title, note.owner_id, note.project_id),
            ("new", self.user.id, self.project.project_id),
        )

    # ── chat ──────────────────────────────────────────────────────────

    def _chat(self):
        gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Board", owner=self.user
        )
        ChannelMember.objects.create(channel=gm, user=self.user, role="owner")
        ChannelMember.objects.create(channel=gm, user=self.user2, role="member")
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=int(ChannelKind.GM),
            channel=gm,
            is_thread=False,
            title="Decisions",
            body=[],
        )
        return gm, note

    def test_chat_note_writer_cannot_take_or_move_the_note(self):
        _gm, note = self._chat()
        elsewhere = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Elsewhere", owner=self.user
        )
        self.authenticate(self.user2)  # a channel member: implicit editor
        self._check_refused(
            CHAT,
            note,
            [
                "team",
                "owner",
                "chat_type",
                "channel",
                "is_thread",
                "thread_root_id",
                "parent_note_id",
                "mentioned_user_ids",
            ],
            {
                "owner": str(self.user2.id),
                "channel": str(elsewhere.pk),
                "chat_type": 99,
                "is_thread": True,
                "thread_root_id": STRANGER_UUID,
                "parent_note_id": 99,
                "mentioned_user_ids": [str(self.user2.id)],
            },
        )

    def test_chat_apps_exact_autosave_body_works(self):
        _gm, note = self._chat()
        self.authenticate(self.user2)
        res = self.client.put(
            CHAT,
            {"user_id": str(self.user2.id), "note_id": note.note_id, "title": "new", "body": []},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        note.refresh_from_db()
        self.assertEqual((note.title, note.owner_id), ("new", self.user.id))
